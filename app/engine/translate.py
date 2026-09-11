from __future__ import annotations

import json

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


def translate_cues(cues: list[Cue], target_lang: str, api_key: str) -> list[Cue]:
    if not api_key:
        raise ValueError(
            "Add an OpenAI API key in Settings to auto-translate. "
            "Or edit the script text yourself."
        )
    if not cues:
        raise ValueError("There is no script to translate.")

    from openai import OpenAI

    language = LANGUAGE_NAMES.get(target_lang, target_lang)
    payload = [{"index": i, "text": cue.text} for i, cue in enumerate(cues)]
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "You translate tutorial narration. Keep the same meaning, "
                    "spoken tone, and roughly similar length. Return JSON: "
                    '{"items":[{"index":0,"text":"..."}]}'
                ),
            },
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
        text = by_index.get(index) or cue.text
        translated.append(
            Cue(
                video_time=cue.video_time,
                text=text,
                should_video_stop=cue.should_video_stop,
            )
        )
    return translated
