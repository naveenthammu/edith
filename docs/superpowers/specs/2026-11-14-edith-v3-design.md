# EDITH v3 Design

Date: 2026-11-14
Status: Approved in brainstorming, pending spec review

## Goal

Turn the single-pass, keyword-matching `main.py` voice assistant into a Windows voice assistant that:

1. Runs continuously and never crashes on recognition, network, or skill errors.
2. Understands free-form speech through an LLM instead of fixed keyword phrases.
3. Performs actions through a set of pluggable skills.
4. Keeps working offline (offline wake word, speech-to-text, LLM fallback, and text-to-speech).

## Target Machine

- Windows 11, Intel i5-1145G7 (4 cores), 15.7 GB RAM, Intel Iris Xe (no NVIDIA GPU).
- All local models must run on CPU. Local LLM is limited to a ~3B parameter quantized model.
- The existing `venv/` was created on macOS and has no Windows interpreter. It must be rebuilt.

## Interaction Model

- **Idle:** Vosk listens continuously and offline for the wake word "edith".
- **Conversation:** after the wake word, EDITH keeps listening without the wake word. Each utterance is transcribed by faster-whisper, handled by the agent, and answered by speech.
- **Return to idle:** after 10 seconds of silence, or when the user says "bye" or "stop". Conversation history resets on return to idle.

## Architecture

```
edith/
  main.py            loop: wait for wake word, converse, return to idle
  config.py          reads .env (BASE_URL, API_KEY, model paths)
  voice/
    wake.py          Vosk wake word detection
    stt.py           faster-whisper, record until silence, return text
    tts.py           pyttsx3 speech through a single-owner queue
  brain/
    llm.py           FreeLLMAPI client with local llama-cpp fallback
    agent.py         chat history and tool loop
  skills/
    __init__.py      @skill registry, builds tool schemas
    web.py  info.py  media.py  system.py  productivity.py
  data/              notes.txt, edith.log, models/ (gitignored)
  tests/
```

Each unit has one purpose:

- `voice/*` turns audio into text and text into audio. They do not know about the LLM.
- `brain/llm.py` returns an OpenAI-shaped chat completion, regardless of backend.
- `brain/agent.py` takes user text and returns reply text. It knows skills only through the registry.
- `skills/*` are plain Python functions. They know nothing about voice or the LLM.

### Request Flow

1. Vosk hears "edith".
2. `stt.py` records until silence and returns text.
3. `agent.py` sends history plus the user text and the tool schemas to `llm.py`.
4. If the response contains tool calls, the agent runs each skill, appends results, and calls the LLM again.
5. The final text reply goes to `tts.py`.
6. EDITH listens again (no wake word) until the idle timeout or "bye".

## Brain

### `brain/llm.py`

- **Primary backend:** FreeLLMAPI, a self-hosted OpenAI-compatible proxy that pools free provider tiers. It is called with the `openai` SDK, `base_url = BASE_URL + "/v1"`, `api_key = API_KEY`, `model = "auto"`, and an 8 second timeout. Tool calling through `auto` was verified on 2026-11-14.
- **Fallback trigger:** connection refused, timeout, HTTP 429, or HTTP 5xx. On the first fallback, EDITH says "switching to offline mode" once.
- **Fallback backend:** llama-cpp-python with `Qwen2.5-3B-Instruct` Q4_K_M GGUF, downloaded from Hugging Face with `huggingface_hub.hf_hub_download` into `data/models/`. It loads lazily on the first fallback (about 5 seconds, about 2.5 GB RAM).
- **Local tool calling:** the local system prompt instructs the model to reply with `{"tool": "<name>", "args": {...}}` to call a skill. `llm.py` parses this into the same `tool_calls` structure FreeLLMAPI returns. If the JSON is invalid, the text is treated as a normal reply.
- **Recovery:** after 60 seconds on the fallback, the next request tries FreeLLMAPI again.
- **No `.env`:** FreeLLMAPI is skipped and only the local model is used.

### `brain/agent.py`

- History: system prompt plus the last 10 turns. It resets on return to idle.
- System prompt: EDITH persona, short spoken-style answers, the current date and time.
- Tool loop: at most 3 tool calls per user request.
- A skill exception is returned to the LLM as `"error: <message>"`. The agent never raises to `main.py`.
- Confirmation: before running a skill registered with `confirm=True`, the agent asks "Are you sure you want to <action>?" through TTS and STT. The skill runs only if the answer contains "yes". Otherwise the LLM receives `"cancelled by user"`. The LLM cannot bypass this.

## Skills

Skills are registered with a decorator. The registry builds the OpenAI tool schema from the function name, docstring, and type hints.

```python
@skill(confirm=False)
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
```

| File | Skill | Implementation |
|---|---|---|
| `web.py` | `open_website(url)` | `webbrowser.open` (default browser) |
| | `search_google(query)` | open a Google search URL |
| | `open_app(name)` | known-app map (notepad, calc, code, chrome, ...) via `os.startfile`, fallback Windows Start search |
| `info.py` | `get_datetime()` | stdlib `datetime` |
| | `get_weather(city)` | Open-Meteo geocoding and forecast APIs via `requests`, no key |
| | `wikipedia(topic)` | Wikipedia REST summary API via `requests` |
| `media.py` | `play_youtube(query)` | fetch the YouTube search page, open the first `videoId`; fallback opens the search results page |
| | `volume(action, steps)` | `pyautogui.press("volumeup" / "volumedown" / "volumemute")` |
| `system.py` | `lock_pc()` (confirm) | `ctypes.windll.user32.LockWorkStation()` |
| | `sleep_pc()` (confirm) | `rundll32 powrprof.dll,SetSuspendState 0,1,0` |
| | `shutdown_pc()` (confirm) | `shutdown /s /t 30` |
| | `cancel_shutdown()` | `shutdown /a` |
| | `screenshot()` | `pyautogui.screenshot()` saved to `Pictures/Edith/` |
| | `battery()` | `psutil.sensors_battery()` |
| `productivity.py` | `set_reminder(minutes, message)` | `threading.Timer`; message spoken through the TTS queue |
| | `add_note(text)` | append with timestamp to `data/notes.txt` |
| | `read_notes()` | read `data/notes.txt` |

The old keyword commands (joke, about yourself, even or odd, spell) are removed. The LLM handles them in conversation.

Known limit: reminders are in memory only and are lost on restart.

## Voice

- **Wake word:** Vosk small English model (about 50 MB), restricted grammar `["edith", "[unk]"]` so other speech maps to unknown.
- **Speech-to-text:** faster-whisper `base.en` (about 150 MB), int8 on CPU. Recording stops after 1 second of silence, or after 15 seconds total.
- **Microphone:** `sounddevice` for both Vosk and Whisper. `speech_recognition` and PyAudio are removed.
- **Text-to-speech:** pyttsx3. pyttsx3 is not thread-safe, so one TTS thread owns the engine and all speech goes through a queue. Reminders speak through the same queue.

## Error Handling

- Microphone missing or STT failure: log, say "I didn't catch that", continue.
- Both LLM backends down: say "my brain is offline", continue listening.
- Skill exception: returned to the LLM as `"error: <message>"`, which explains it aloud.
- All errors are logged to `data/edith.log`.

## Configuration and Secrets

- `.env` holds `BASE_URL` and `API_KEY`. It is loaded with `python-dotenv`.
- `.env` is gitignored and must never be committed. `.env.example` holds placeholder values.
- `.gitignore`: `.env`, `venv/`, `data/`, `__pycache__/`.

## Setup

- Rebuild `venv/` on Windows with `py -m venv venv`. Python must be 3.10 to 3.12 for llama-cpp-python CPU wheels.
- `requirements.txt`: `openai`, `python-dotenv`, `vosk`, `faster-whisper`, `sounddevice`, `numpy`, `pyttsx3`, `pyautogui`, `psutil`, `requests`, `huggingface_hub`, `llama-cpp-python`, `pytest`.
- Install llama-cpp-python from the CPU wheel index: `pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu`.
- Models download into `data/models/` on first use.
- The README documents setup and the FreeLLMAPI dependency.

## Testing

All automated tests run without a microphone or network.

- `test_registry.py`: `@skill` produces the correct schema from type hints and docstring; the confirm flag is stored.
- `test_agent.py`: with a fake LLM, a tool call runs the skill and returns the result; a plain reply is returned directly; the 3-call cap holds; a skill exception becomes `"error: ..."`; a confirm skill runs only on "yes".
- `test_llm.py`: FreeLLMAPI failure triggers the local fallback (both mocked); local JSON is parsed into `tool_calls`; invalid JSON becomes plain text.
- `test_skills.py`: app map lookup, YouTube `videoId` extraction from sample HTML, notes written to a temporary file.
- Manual: one live run against FreeLLMAPI with a real voice round trip.

## Out of Scope

GUI, multiple users, persistent reminders, voice cloning, auto-start on Windows boot.
