from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.engine.project import Cue

LANGUAGE_NAMES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "hi": "Hindi",
    "ur": "Urdu",
    "ar": "Arabic",
    "pt": "Portuguese",
    "ja": "Japanese",
    "zh": "Chinese",
}

# Terms tutorials usually leave in English. Longer phrases are listed first
# so "api key" is suggested instead of the shorter word inside it.
_LEXICON = (
    "pull request",
    "api key",
    "access token",
    "branch",
    "latitude",
    "longitude",
    "altitude",
    "commit",
    "checkout",
    "merge",
    "rebase",
    "clone",
    "stash",
    "repository",
    "repo",
    "api",
    "url",
    "json",
    "html",
    "css",
    "http",
    "https",
    "sql",
    "gps",
    "uuid",
    "login",
    "username",
    "token",
    "endpoint",
    "schema",
)

_LEXICON_KEYS = frozenset(term.casefold() for term in _LEXICON)

_STOPWORDS = frozenset(
    """
    a an the and or but if then else when while of to for from in on at by with
    without into over under after before this that these those it its is are was
    were be been being as not no nor so just also now next here there you your
    we our they them their click open close press select set see look watch
    please video screen button menu window tab page step line text file folder
    """.split()
)

_URL = re.compile(r"https?://[^\s<>\"']+")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)*")
_FILE_EXTENSIONS = frozenset(
    {
        "png",
        "jpg",
        "jpeg",
        "gif",
        "svg",
        "webp",
        "mp4",
        "mov",
        "json",
        "txt",
        "csv",
        "pdf",
        "py",
        "js",
        "ts",
        "tsx",
        "html",
        "css",
        "xml",
        "yml",
        "yaml",
        "md",
        "zip",
        "wav",
        "mp3",
    }
)
@dataclass(frozen=True)
class KeepCandidate:
    term: str
    quote: str


def estimate_keep_terms(cues: list[Cue]) -> list[KeepCandidate]:
    """Propose words to leave in English. This does not call the model."""
    found: list[KeepCandidate] = []
    seen: set[str] = set()
    for cue in cues:
        text = cue.text or ""
        for start, end, term in _candidate_spans(text):
            base = _base_term(term)
            key = base.casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            found.append(KeepCandidate(term=base, quote=_quote(text, start, end)))
    return found


def mask_terms(text: str, terms: list[str], start_index: int = 0) -> tuple[str, dict[str, str], int]:
    """Replace confirmed terms with ⟦K0⟧ tokens. The map restores original spelling."""
    spans = _term_spans(text, terms)
    mapping: dict[str, str] = {}
    pieces: list[str] = []
    cursor = 0
    index = start_index
    for start, end, original in spans:
        token = f"⟦K{index}⟧"
        mapping[token] = original
        pieces.append(text[cursor:start])
        pieces.append(token)
        cursor = end
        index += 1
    pieces.append(text[cursor:])
    return "".join(pieces), mapping, index


def unmask_terms(text: str, mapping: dict[str, str]) -> str:
    for token, original in mapping.items():
        text = text.replace(token, original)
    return text


def translate_cues(
    cues: list[Cue],
    target_lang: str,
    api_key: str,
    keep_terms: list[str] | None = None,
) -> list[Cue]:
    if not api_key:
        raise ValueError(
            "Add an OpenAI API key in Settings to auto-translate. "
            "Or edit the script text yourself."
        )
    if not cues:
        raise ValueError("There is no script to translate.")

    from openai import OpenAI

    language = LANGUAGE_NAMES.get(target_lang, target_lang)
    masked_rows: list[tuple[str, dict[str, str]]] = []
    next_index = 0
    for cue in cues:
        masked, mapping, next_index = mask_terms(cue.text, keep_terms or [], next_index)
        masked_rows.append((masked, mapping))
    payload = [{"index": i, "text": masked} for i, (masked, _) in enumerate(masked_rows)]
    used_tokens = any(mapping for _, mapping in masked_rows)
    system = (
        "You translate tutorial narration. Keep the same meaning, "
        "spoken tone, and roughly similar length. "
    )
    if used_tokens:
        system += "Copy tokens like ⟦K0⟧ exactly. Do not translate or remove them. "
    system += 'Return JSON: {"items":[{"index":0,"text":"..."}]}'
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    f"Translate each item to {language}. Do not add timestamps.\n"
                    + json.dumps(payload, ensure_ascii=False)
                ),
            },
        ],
    )
    content = response.choices[0].message.content or "{}"
    data = json.loads(content)
    items = data.get("items") or data.get("translations") or []
    by_index = {}
    for item in items:
        try:
            by_index[int(item["index"])] = str(item["text"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
    translated = []
    for index, cue in enumerate(cues):
        text = by_index.get(index)
        if text:
            text = unmask_terms(text, masked_rows[index][1])
        else:
            text = cue.text
        translated.append(
            Cue(
                video_time=cue.video_time,
                text=text,
                should_video_stop=cue.should_video_stop,
            )
        )
    return translated


def polish_cues(cues: list[Cue], lang: str, api_key: str) -> list[Cue]:
    """Fix wording, and rewrite any line that is not in ``lang`` into that language."""
    if not api_key:
        raise ValueError("Add an OpenAI API key in Settings to fix the narration language.")
    if not cues:
        raise ValueError("There is no narration to fix.")

    from openai import OpenAI

    language = LANGUAGE_NAMES.get(lang, lang)
    payload = [{"index": index, "text": cue.text} for index, cue in enumerate(cues)]
    system = (
        "You clean up spoken tutorial narration so every line is in the target language. "
        "Handle each item on its own. "
        "If a line is already in the target language, fix grammar, spelling, and awkward phrasing. "
        "If a line is in any other language, rewrite it into the target language. "
        "Romanized text counts as that language: Roman Urdu and Roman Hindi are not English. "
        "Keep the same meaning, spoken tutorial tone, product names, and on-screen labels. "
        "Do not merge, split, or add lines. "
        'Return JSON: {"items":[{"index":0,"text":"..."}]}'
    )
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.2,
        max_tokens=4000,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    f"Target language: {language}.\n"
                    + json.dumps(payload, ensure_ascii=False)
                ),
            },
        ],
    )
    content = response.choices[0].message.content or "{}"
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("The model did not return narration that could be read.") from exc
    items = data.get("items") or data.get("cues") or data.get("lines") or []
    by_index: dict[int, str] = {}
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                by_index[int(item["index"])] = str(item.get("text") or "").strip()
            except (KeyError, TypeError, ValueError):
                continue
    polished: list[Cue] = []
    for index, cue in enumerate(cues):
        text = by_index.get(index) or cue.text
        polished.append(
            Cue(
                video_time=cue.video_time,
                text=text,
                should_video_stop=cue.should_video_stop,
            )
        )
    return polished


def _candidate_spans(text: str) -> list[tuple[int, int, str]]:
    occupied: list[tuple[int, int]] = []
    found: list[tuple[int, int, str]] = []

    def take(start: int, end: int, term: str) -> None:
        if _overlaps(start, end, occupied):
            return
        occupied.append((start, end))
        found.append((start, end, term))

    for match in _URL.finditer(text):
        raw = match.group().rstrip(".,;:!?)]")
        if raw:
            take(match.start(), match.start() + len(raw), raw)

    for phrase in sorted(_LEXICON, key=len, reverse=True):
        for match in re.finditer(_inflected_boundary(phrase), text, flags=re.IGNORECASE):
            take(match.start(), match.end(), text[match.start() : match.end()])

    for match in _TOKEN.finditer(text):
        token = match.group()
        folded = token.casefold()
        if folded in _STOPWORDS or folded in _LEXICON_KEYS:
            continue
        if not _is_identifier(token):
            continue
        take(match.start(), match.end(), token)

    found.sort()
    return found


def _term_spans(text: str, terms: list[str]) -> list[tuple[int, int, str]]:
    unique: list[str] = []
    seen: set[str] = set()
    for term in terms:
        cleaned = " ".join(term.split())
        if not cleaned:
            continue
        base = _base_term(cleaned)
        key = base.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(base)
    unique.sort(key=len, reverse=True)

    occupied: list[tuple[int, int]] = []
    spans: list[tuple[int, int, str]] = []
    for term in unique:
        for match in re.finditer(_inflected_boundary(term), text, flags=re.IGNORECASE):
            if _overlaps(match.start(), match.end(), occupied):
                continue
            occupied.append((match.start(), match.end()))
            spans.append((match.start(), match.end(), text[match.start() : match.end()]))
    spans.sort()
    return spans


def _boundary(term: str) -> str:
    parts = [re.escape(part) for part in term.split()]
    return rf"(?<![A-Za-z0-9_]){r'\s+'.join(parts)}(?![A-Za-z0-9_])"


def _inflected_boundary(term: str) -> str:
    """Match a term and its simple plural, such as branch and branches."""
    parts = term.split()
    if not parts:
        return _boundary(term)
    last = _base_word(parts[-1])
    head = [re.escape(part) for part in parts[:-1]]
    if "_" in last and re.fullmatch(r"[A-Za-z]+(?:_[A-Za-z]+)+", last):
        head.append(rf"{re.escape(last)}(?:s)?")
    elif _can_inflect(last):
        head.append(_inflection_regex(last))
    else:
        head.append(re.escape(parts[-1]))
    return rf"(?<![A-Za-z0-9_]){r'\s+'.join(head)}(?![A-Za-z0-9_])"


def _base_term(term: str) -> str:
    parts = " ".join(term.split()).split(" ")
    if not parts:
        return term
    return " ".join([*parts[:-1], _base_word(parts[-1])])


def _base_word(word: str) -> str:
    if word.casefold() in _LEXICON_KEYS:
        return word
    if "_" in word and re.fullmatch(r"[A-Za-z]+(?:_[A-Za-z]+)+", word):
        folded = word.casefold()
        if folded.endswith("s") and not folded.endswith("ss") and not word.endswith("_"):
            return word[:-1]
        return word
    return _singular_token(word)


def _can_inflect(word: str) -> bool:
    if not re.fullmatch(r"[A-Za-z]{3,}", word):
        return False
    return word.casefold() not in {"gps", "css", "https"}


def _inflection_regex(base: str) -> str:
    folded = base.casefold()
    if re.search(r"(?:ch|sh|[sxz])$", folded):
        return rf"{re.escape(base)}(?:es)?"
    if re.search(r"[^aeiou]y$", folded):
        return rf"{re.escape(base[:-1])}(?:y|ies)"
    return rf"{re.escape(base)}(?:s)?"


def _singular_token(word: str) -> str:
    if not _can_inflect(word):
        return word
    lower = word.casefold()
    if lower.endswith("ies") and len(lower) > 4 and lower[-4] not in "aeiou":
        return word[:-3] + ("Y" if word[-3].isupper() else "y")
    if re.search(r"(?:ch|sh|[sxz])es$", lower) and not lower.endswith("yses"):
        return word[:-2]
    if lower.endswith("s") and not lower.endswith(("ss", "sis", "us")):
        stem = word[:-1]
        if len(stem) >= 3 and stem.casefold() not in _STOPWORDS:
            return stem
    return word


def _overlaps(start: int, end: int, occupied: list[tuple[int, int]]) -> bool:
    return any(start < other_end and end > other_start for other_start, other_end in occupied)


def _is_identifier(token: str) -> bool:
    if re.fullmatch(r"[A-Z][A-Z0-9]{1,}", token):
        return True
    if re.search(r"[a-z][A-Z]", token) or re.search(r"[A-Z][a-z]+[A-Z]", token):
        return True
    if "_" in token and re.search(r"[A-Za-z]", token):
        return True
    if any(char.isdigit() for char in token) and re.search(r"[A-Za-z]", token):
        return True
    if "." in token:
        suffix = token.rsplit(".", 1)[-1].casefold()
        stem = token.rsplit(".", 1)[0]
        if stem and suffix in _FILE_EXTENSIONS:
            return True
    return False


def _quote(text: str, start: int, end: int) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= 80:
        return compact
    left = max(0, start - 28)
    right = min(len(text), end + 28)
    snippet = re.sub(r"\s+", " ", text[left:right]).strip()
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    return f"{prefix}{snippet}{suffix}"
