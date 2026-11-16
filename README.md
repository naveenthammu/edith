# EDITH

A voice assistant for Windows, macOS and Linux. Say "Edith", then talk normally. EDITH can work with files, apps and windows, type and press keys, use the clipboard, change Wi-Fi, Bluetooth, brightness and volume, write and run scripts, send WhatsApp messages, keep your Naukri profile fresh, and answer questions. She remembers what she learns about you.

## How it works

- Wake word: Vosk, offline.
- Speech-to-text: faster-whisper `small.en`, offline. Tuned for accented English.
- Brain: Claude (`claude-opus-5-5`) when online. Without internet or an API key, EDITH uses Qwen2.5-7B running locally with llama-cpp-python. Replies from the offline model take about 10–20 seconds on a laptop CPU.
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
3. Get a Claude API key at https://console.anthropic.com. Copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`. Never commit `.env`. Without a key, EDITH runs offline only.
4. Start EDITH from the project folder:
   ```bash
   venv/bin/python main.py      # Windows: venv/Scripts/python main.py
   ```
   Wait for "Edith 3 point o is online". Stop EDITH with Ctrl+C in that terminal.

The first run downloads the Vosk model (about 50 MB), the Whisper model (about 460 MB) and the offline Qwen model (about 4.7 GB, in the background). Later starts take about a minute.

## Using it

- Say "Edith", wait for "Yes sir?", then speak.
- The galaxy card appears in the top-right corner while you talk. **▾** shows what you asked and what EDITH answered this session; **✕** hides the card.
- Keep talking without the wake word. After 10 seconds of silence, or "bye", EDITH goes back to waiting for "Edith".
- Anything risky asks "Are you sure?" first: deleting files (they go to the Recycle Bin/Trash, and only inside your home folder), closing apps, running scripts, sending WhatsApp messages, turning Wi-Fi off, lock, sleep and shutdown. Say "yes" to go ahead.

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

Mail, messages and notifications that EDITH reads are sent to the online model to answer you, like everything else you say.

## Settings (`.env`)

- `ANTHROPIC_API_KEY`: the Claude key. Empty means offline only.
- `MODEL`: the Claude model (default `claude-opus-5-5`).
- `MIC_THRESHOLD`: raise it if background noise keeps EDITH recording, lower it if she misses your voice.
- `WAKE_WORD`: default `edith`.

## Tests

```bash
venv/bin/python -m pytest      # Windows: venv/Scripts/python -m pytest
```

Logs are in `data/edith.log`. Notes are in `data/notes.txt`, memory in `data/memory.txt`, WhatsApp contacts in `data/contacts.json`.

## Troubleshooting

- **EDITH hears the wrong words:** each line she heard is in `data/edith.log` as `heard: ...`. Speak a little closer to the mic, or adjust `MIC_THRESHOLD`.
- **"Switching to offline mode":** Claude wasn't reachable (no internet, a wrong key, or a rate limit). The reason is in `data/edith.log`. EDITH tries Claude again after a minute.
- **"Overlay unavailable, running voice-only":** the overlay's web view couldn't start (see Setup step 1). Voice still works; details are in `data/edith.log`.
- **A setting command fails on macOS/Linux:** install the extra tool named in Setup step 1 (`blueutil`, `brightness`, `brightnessctl`, `xclip`).
- **Preview the orb without EDITH:** open `ui/orb.html?t=12` in a browser.
