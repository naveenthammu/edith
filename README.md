# EDITH

A voice assistant for Windows, macOS and Linux. Say "Edith", then talk normally. EDITH can work with files, apps and windows, type and press keys, use the clipboard, change Wi-Fi, Bluetooth, brightness and volume, write and run scripts, send WhatsApp messages, keep your Naukri profile fresh, and answer questions. She remembers what she learns about you.

**Central household engine (in progress):** the existing interface can now use a Rust hub with scoped retrieval, durable reminders, contacts, an email approval journal and reviewed own-store Shopify checkout. Catalog selections become local proposals; exact voice confirmation controls cart preparation and separate checkout opening. Payment remains in the merchant checkout. Paired clients have separate roles and local-only reasoning. Optional household Gmail onboarding is separate from the older personal-mode integration below; sending requires exact draft review and confirmation. The capabilities in the introduction describe personal desktop mode; paired household mode exposes only explicitly scoped tools. See the [setup/runbook](docs/product-plan/central-engine-runbook.md) and [Rust API](hub/README.md). No new frontend is required. Home Assistant setup has been deferred; no physical devices or personal/merchant accounts were connected by this change.

## How it works

- Wake word: Vosk, offline.
- Speech-to-text: faster-whisper `small.en`, local after model provisioning. Live language/accent and acoustic accuracy must be measured on the installation.
- Brain: configured Anthropic provider, then an optional OpenAI-compatible endpoint, then local inference. Local inference can use llama-cpp-python or an installed Ollama GGUF model. Paired household conversations use local inference from their first utterance; private retrieval cannot fall back to cloud. Configured child mode uses curated local games instead of open-ended inference. Provider/model IDs in configuration must be verified against the account being used. CPU latency depends on context and tool turns; individual synthetic grounded-answer samples took about 18–32 seconds on this development PC, not yet conversational-speed performance.
- Memory: after each conversation, Claude notes lasting facts about you (names, preferences, corrections) in `data/memory.txt`. Every later conversation starts with them. It's a plain text file you can read and edit.
- Voice: Windows SAPI on Windows, pyttsx3 elsewhere (macOS speech, espeak-ng on Linux).
- Overlay: a small galaxy card in the top-right corner, drawn by pywebview.

## Setup

1. Install Python 3.12 and the OS extras:
   - **Windows:** `winget install -e --id Python.Python.3.12`. On Windows 10 also run `winget install -e --id Microsoft.EdgeWebView2Runtime`.
   - **macOS:** `brew install python@3.12 portaudio blueutil brightness`. Allow the terminal under System Settings → Privacy & Security → Accessibility and Microphone.
   - **Linux (Debian/Ubuntu):** `sudo apt install python3.12-venv portaudio19-dev espeak-ng xclip brightnessctl gnome-screenshot python3-gi gir1.2-webkit2-4.1`.
2. Create the environment and install:
   ```bash
   python3.12 -m venv venv                         # Windows: py -3.12 -m venv venv
   venv/bin/python -m pip install -r requirements.txt   # Windows: venv/Scripts/python
   ```
3. Copy `.env.example` to `.env` if configuring personal mode. `ANTHROPIC_API_KEY` optionally enables Anthropic; `BASE_URL`, `API_KEY` and `MODEL` optionally enable a compatible endpoint. With neither remote provider configured, inference stays local. Never commit `.env`. For paired household mode, follow the runbook instead of placing hub credentials in `.env`.
4. Start EDITH from the project folder:
   ```bash
   venv/bin/python main.py      # Windows: venv/Scripts/python main.py
   ```
   Wait for "Edith 3 point o is online". Stop EDITH with Ctrl+C in that terminal.

The first speech run downloads missing Vosk and Whisper assets. In `llama_cpp` mode, EDITH also provisions its configured Qwen GGUF. In `ollama` mode, the selected model must already be installed; EDITH does not pull it or download a duplicate GGUF. Provision assets before expecting offline startup. The Windows development environment has been tested with Python 3.13; setup and acoustic behavior on other platforms remain separate checks.

## Using it

- Say "Edith", wait for "Yes sir?", then speak.
- The galaxy card appears in the top-right corner while you talk. **▾** shows what you asked and what EDITH answered this session; **✕** hides the card.
- Keep talking without the wake word. After 10 seconds of silence, or "bye", EDITH goes back to waiting for "Edith".
- Selected tools ask "Are you sure?" first: deleting files, closing apps, running scripts, sending messages and some system actions. Confirmation requires a short explicit affirmative; a sentence containing a negated "yes" is rejected. Personal-mode scripts and generic desktop inputs still have host-user authority, so these flags are not a household sandbox. Paired mode excludes those unscoped tools.

Things to try:

| Say | What happens |
|---|---|
| "Find my resume" / "Open Downloads" | Searches your home folder / opens a folder |
| "Move report.pdf from Downloads to Documents" | Moves or renames files |
| "Close Chrome" / "Minimise this window" | Closes apps, controls windows |
| "Type hello team" / "Press control S" | Types into the focused app, presses shortcuts |
| "Turn Bluetooth off" / "Set brightness to 40" | System settings |
| "How's my system doing?" | CPU, memory, disk, IP and uptime |
| "Write a Python script that renames my photos by date", then "Run it" | Saves to `Documents/Edith/scripts` and runs it |
| "Save Mom's number, +91 98765 43210", then "WhatsApp Mom I'll be late" | Sends through WhatsApp Desktop, or WhatsApp Web |
| "Remember that I prefer Celsius" / "Forget Celsius" | Edits what EDITH knows about you |
| "Log in to Naukri", then "Refresh my Naukri profile" | See below |

### Teach EDITH a task

Say "Edith, watch me", then do the task with your mouse and keyboard. When you finish, say "Edith, done, call it Monday report". EDITH sends what you did to Claude, which turns it into named steps. The steps are saved in `data/taught/monday-report/skill.json`, a plain file you can edit. Later, say "Do Monday report", or "Do Monday report for March" if the task has a value that changes. EDITH asks "Are you sure?" and then repeats the task. If a button has moved, she finds it again by its picture.

- Learning needs internet. Replaying mostly doesn't: Claude vision is used only when a button can't be found by its picture.
- Move the mouse into a screen corner to stop a replay.
- EDITH doesn't record typing in windows whose title mentions a password or login. While she watches, keystrokes in every other window are recorded, so say "done" or "cancel" as soon as you finish.
- "What have I taught you?" lists tasks. "Forget Monday report" deletes one.
- Window tracking works on Windows. On macOS and Linux, replay relies on matching pictures and vision.

### Naukri

Say "log in to Naukri" once: a browser window opens and you log in yourself. EDITH keeps the session in `data/browser/naukri` and never sees your password. After that, EDITH refreshes your profile once a day by re-saving your resume headline, which marks the profile as updated so recruiters see it higher. A Chrome window opens for about 15 seconds while it does this. If Naukri blocks the automated visit, EDITH tells you and you refresh it by hand that day.

### Gmail, Google Calendar and Google Tasks

EDITH reads your unread mail, today's events and your to-do lists, sends email and adds events (both after you say "yes"), and adds tasks, for example to a "Bucket list". Google Keep has no API for personal accounts, so EDITH uses Google Tasks for notes and lists.

One-time setup, about 5 minutes:

1. Go to https://console.cloud.google.com, create a project, and under **APIs & Services > Library** enable **Gmail API**, **Google Calendar API** and **Google Tasks API**.
2. Under **APIs & Services > OAuth consent screen**, choose **External**, fill in the app name, and add your own Gmail address as a **test user**.
3. Under **APIs & Services > Credentials**, create an **OAuth client ID** of type **Desktop app** and download its JSON.
4. Save that file as `data/google/credentials.json`.
5. Say "connect Google". Your browser asks you to approve access. Google warns that the app is unverified, because it's your own app: choose **Continue**.

The sign-in token stays in `data/google/token.json`. Delete that file to disconnect.

### WhatsApp messages and other notifications

Say "read my WhatsApp messages" or "any new notifications?". EDITH reads the notifications Windows shows, so it sees the sender and the preview of each WhatsApp message, email or Teams chat that is still in the notification center. Turn on **Settings > Privacy & security > Notifications > Let apps access your notifications** once. This works on Windows only.

In personal mode, an enabled online model can receive conversation and tool content, including mail/notification previews. Paired household mode uses local reasoning, scoped knowledge and a restricted tool registry. Telegram does not inherit the desktop's memory, taught tasks or private document client.

## Settings (`.env`)

- `ANTHROPIC_API_KEY` and `CLAUDE_MODEL`: Anthropic credentials/model selection. The model string is configuration, not a promise of account access.
- `BASE_URL`, `API_KEY`, `MODEL`: optional OpenAI-compatible endpoint and its model.
- `EDITH_LOCAL_PROVIDER`: `llama_cpp` (default) or `ollama`.
- `EDITH_OLLAMA_URL`, `EDITH_OLLAMA_MODEL`: literal loopback Ollama endpoint and an installed local model. Cloud-backed aliases are rejected.
- `EDITH_EMBEDDING_MODEL`: optional `nomic-embed-text:v1.5` for local hybrid retrieval. Chosen source documents must be explicitly indexed; missing embeddings fall back to keywords.
- `EDITH_WORKSPACE_CONFIG`: protected JSON binding a paired adult/elder principal to one source project and separate staging directory. Enables read/search/patch proposals, without automatic application or execution.
- `EDITH_HUB_CLIENT_CONFIG`: protected paired-client JSON path; enables scoped household mode.
- `EDITH_DATA_DIR`: optional absolute directory for configured runtime state, logs and speech/model assets. Legacy personal-mode notes, contacts and teaching modules still have their original paths until migrated.
- `MIC_THRESHOLD`: raise it if background noise keeps EDITH recording, lower it if she misses your voice.
- `WAKE_WORD`: default `edith`.

## Tests

```bash
venv/bin/python -m pytest      # Windows: venv/Scripts/python -m pytest
```

Logs are in `data/edith.log`. Notes are in `data/notes.txt`, memory in `data/memory.txt`, WhatsApp contacts in `data/contacts.json`.

## Troubleshooting

- **EDITH hears the wrong words:** inspect the existing on-screen transcript. Normal utterance logs now record character counts instead of raw speech text. Adjust the microphone/`MIC_THRESHOLD` after a controlled listening test.
- **Local model fallback in the log:** a configured remote provider was unavailable. EDITH retries later in personal mode. Fallback is log-only so a delayed failure cannot announce itself after a cancelled turn; paired conversations remain local from the start.
- **"Overlay unavailable, running voice-only":** the overlay's web view couldn't start (see Setup step 1). Voice still works; details are in `data/edith.log`.
- **A setting command fails on macOS/Linux:** install the extra tool named in Setup step 1 (`blueutil`, `brightness`, `brightnessctl`, `xclip`).
- **Preview the orb without EDITH:** open `ui/orb.html?t=12` in a browser.
