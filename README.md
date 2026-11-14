# EDITH

A Windows voice assistant. Say "Edith", then talk normally. EDITH answers with an LLM and can open websites and apps, play YouTube, check the weather, read Wikipedia, control volume, lock or shut down the PC, set reminders and take notes.

## How it works

- Wake word: Vosk, offline.
- Speech-to-text: faster-whisper `base.en`, offline.
- Brain: [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) (a local proxy for free LLM tiers). When it is unreachable, EDITH switches to Qwen2.5-3B running locally with llama-cpp-python.
- Voice: pyttsx3 (Windows SAPI).

## Setup

1. Install Python 3.12: `winget install -e --id Python.Python.3.12`
2. Create the environment and install:
   ```bash
   py -3.12 -m venv venv
   venv/Scripts/python -m pip install -r requirements.txt
   ```
3. Run FreeLLMAPI and create a unified API key in its dashboard.
4. Copy `.env.example` to `.env` and fill in `BASE_URL` and `API_KEY`. Never commit `.env`.
5. Start EDITH: `venv/Scripts/python main.py`

The first run downloads the Vosk model (about 50 MB) and the Whisper model (about 150 MB). The local Qwen model (about 2 GB) downloads in the background after startup, so offline mode works once it finishes.

## Using it

- Say "Edith", wait for "Yes sir?", then speak.
- Keep talking without the wake word. After 10 seconds of silence, or "bye", EDITH goes back to waiting for "Edith".
- Lock, sleep and shutdown always ask "Are you sure?" first. Say "yes" to go ahead.
- Settings in `.env`: `MIC_THRESHOLD` (raise it if background noise keeps EDITH recording, lower it if she misses your voice) and `WAKE_WORD` (default `edith`).

## Tests

```bash
venv/Scripts/python -m pytest
```

Logs are in `data/edith.log`. Notes are in `data/notes.txt`.

## Galactic Voyage visualization

The standalone interactive Sun-and-planets visualization is in
[`edith-sun-galaxy.html`](./edith-sun-galaxy.html). It is separate from the
Python voice assistant and runs directly in a modern browser without installing
packages. See the [visualizer guide](./docs/galaxy-visualizer.md) for controls,
implementation details, run instructions, customization, and reuse in other
projects.
