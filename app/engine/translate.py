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
            key = " ".join(term.split()).casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            found.append(KeepCandidate(term=" ".join(term.split()), quote=_quote(text, start, end)))
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
        for match in re.finditer(_boundary(phrase), text, flags=re.IGNORECASE):
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
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(cleaned)
    unique.sort(key=len, reverse=True)

    occupied: list[tuple[int, int]] = []
    spans: list[tuple[int, int, str]] = []
    for term in unique:
        for match in re.finditer(_boundary(term), text, flags=re.IGNORECASE):
            if _overlaps(match.start(), match.end(), occupied):
                continue
            occupied.append((match.start(), match.end()))
            spans.append((match.start(), match.end(), text[match.start() : match.end()]))
    spans.sort()
    return spans


def _boundary(term: str) -> str:
    parts = [re.escape(part) for part in term.split()]
    return rf"(?<![A-Za-z0-9_]){r'\s+'.join(parts)}(?![A-Za-z0-9_])"


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
