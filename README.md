# EDITH

A Windows voice assistant. Say "Edith", then talk normally. EDITH answers with an LLM and can open websites and apps, play YouTube, check the weather, read Wikipedia, control volume, lock or shut down the PC, set reminders and take notes.

## How it works

- Wake word: Vosk, offline.
- Speech-to-text: faster-whisper `small.en`, offline. Tuned for accented English.
- Brain: [FreeLLMAPI](https://github.com/tashfeenahmed/freellmapi) (a local proxy for free LLM tiers). When it is unreachable, EDITH switches to Qwen2.5-3B running locally with llama-cpp-python.
- Voice: pyttsx3 (Windows SAPI).
- Overlay: a small galaxy card in the top-right corner, drawn by pywebview with Microsoft Edge WebView2.

## Setup

1. Install Python 3.12: `winget install -e --id Python.Python.3.12`. Windows 11 already includes the WebView2 runtime the overlay needs; on Windows 10, install it with `winget install -e --id Microsoft.EdgeWebView2Runtime`.
2. Create the environment and install:
   ```bash
   py -3.12 -m venv venv
   venv/Scripts/python -m pip install -r requirements.txt
   ```
3. Run FreeLLMAPI and create a unified API key in its dashboard.
4. Copy `.env.example` to `.env` and fill in `BASE_URL` and `API_KEY`. Never commit `.env`.
5. Start EDITH from the project folder:
   ```bash
   venv/Scripts/python main.py
   ```
   Wait for "Edith 3 point o is online". Stop EDITH with Ctrl+C in that terminal.

The first run downloads the Vosk model (about 50 MB) and the Whisper model (about 460 MB), so startup takes a few minutes. Later starts take about a minute. The local Qwen model (about 2 GB) downloads in the background after startup, so offline mode works once it finishes.

## Using it

- Say "Edith", wait for "Yes sir?", then speak.
- The galaxy card appears in the top-right corner while you talk. **▾** shows what you asked and what EDITH answered this session; **✕** hides the card. It hides on its own when the conversation ends.
- Keep talking without the wake word. After 10 seconds of silence, or "bye", EDITH goes back to waiting for "Edith".
- Lock, sleep and shutdown always ask "Are you sure?" first. Say "yes" to go ahead.
- Settings in `.env`: `MIC_THRESHOLD` (raise it if background noise keeps EDITH recording, lower it if she misses your voice) and `WAKE_WORD` (default `edith`).

## Tests

```bash
venv/Scripts/python -m pytest
```

Logs are in `data/edith.log`. Notes are in `data/notes.txt`.

## Troubleshooting

- **EDITH hears the wrong words:** each line she heard is in `data/edith.log` as `heard: ...`. Speak a little closer to the mic, or adjust `MIC_THRESHOLD`.
- **Answers get worse, log shows `429 Too Many Requests`:** FreeLLMAPI hit its rate limit and EDITH fell back to the offline model. Wait for the limit to reset or add more keys in FreeLLMAPI.
- **"Overlay unavailable, running voice-only":** the WebView2 runtime is missing (see Setup step 1). Voice still works; details are in `data/edith.log`.
- **Preview the orb without EDITH:** open `ui/orb.html?t=12` in a browser.
