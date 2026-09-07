# Tutorial Composer

A desktop app for turning screen recordings into narrated tutorial videos.

You drop in clips, write timestamped lines (English, Urdu, or another language), generate voices, and export a YouTube-ready MP4. If a line of speech is longer than the gap on screen, the video holds on a freeze-frame so the voice can finish.

This is **not** a full NLE. It is a small composer for one job: record the UI, then add spoken explanation.

## Features

- Import video and still images onto a **V1** track; split, trim, reorder, and ripple-delete a marked range
- Mute the original recording audio in preview and export
- Narration table: **time + text**, with one tab per language
- Generate voices with **Edge TTS** (no API key), or OpenAI / ElevenLabs if you add keys
- WAV voice clips for reliable preview on Windows, macOS, and Linux
- Voice-over track (**VO**) on the timeline; playhead waits until a line actually starts
- Optional music bed with volume control
- Translate a script into a new language tab (OpenAI key)
- Export H.264 / AAC MP4 with freeze-frame holds and mixed narration

## Requirements

- Python 3.11+ (3.13 works)
- [FFmpeg](https://ffmpeg.org/) on your `PATH`, or `imageio-ffmpeg` (installed with the requirements)
- Windows, macOS, or Linux

## Install

```bash
git clone https://github.com/Muhammadinaam/tutorial-composer.git
cd tutorial-composer
python -m venv venv
```

Windows:

```bat
venv\Scripts\activate
pip install -r requirements.txt
```

macOS / Linux:

```bash
source venv/bin/activate
pip install -r requirements.txt
```

## Run

Windows: double-click `run.bat`, or:

```bat
venv\Scripts\python.exe run.py
```

macOS / Linux:

```bash
venv/bin/python run.py
```

## How to use

1. **Add video** (or images) to V1.
2. Add narration lines: **Add at playhead**, then type the text. Time is when that line starts.
3. Pick a voice and click **Generate voices**. Green blocks appear on the VO track.
4. Press **Play**. Scrub the red playhead or the bar under the preview.
5. Cut unwanted middle: Mark In (`I`), Mark Out (`O`), **Delete In→Out**. Or Split twice and delete the middle clip.
6. Optional: **+ Lang** or **Translate** for another language (same picture, new voice).
7. **Export MP4**.

API keys (optional) live in **Settings**. They are stored only on this machine, not in the project file:

- Windows: `%APPDATA%\tutorial-composer\settings.json`
- macOS: `~/Library/Application Support/tutorial-composer/settings.json`
- Linux: `~/.config/tutorial-composer/settings.json`

## Project files

Save / Open uses a JSON project (clips, cues, language tabs). Generated voice files stay in the local cache and are rebuilt with **Generate voices**.

## License

[MIT](LICENSE)
