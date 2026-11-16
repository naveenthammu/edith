"""Gmail, Google Calendar and Google Tasks through Google's official APIs.

One-time setup (README, "Google"): enable the Gmail, Calendar and Tasks APIs in a Google Cloud project,
create an OAuth client of type "Desktop app", and save its JSON as data/google/credentials.json.
Then ask EDITH to "connect Google": the browser asks the user to approve, and the token is kept in
data/google/token.json. EDITH never sees the Google password.
"""
import base64
import logging
import threading
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from skills import skill

log = logging.getLogger(__name__)

DIR = Path(__file__).resolve().parent.parent / "data" / "google"
CREDENTIALS, TOKEN = DIR / "credentials.json", DIR / "token.json"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/tasks",
]
NOT_SET_UP = ("Google isn't set up yet: save the OAuth client file as data/google/credentials.json "
              "(see the README), then ask me to connect Google.")


def _service(name: str, version: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not TOKEN.exists():
        raise RuntimeError("Google isn't connected yet. Ask me to connect Google.")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise RuntimeError("the Google login has expired. Ask me to connect Google again.")
        creds.refresh(Request())
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return build(name, version, credentials=creds, cache_discovery=False)


def _login() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS), SCOPES)
        creds = flow.run_local_server(port=0, timeout_seconds=300, open_browser=True)
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
        log.info("google connected")
    except Exception:
        log.exception("google login failed")


@skill()
def connect_google() -> str:
    """Sign in to Google once so EDITH can use the user's Gmail, Google Calendar and Google Tasks."""
    if not CREDENTIALS.exists():
        return "error: " + NOT_SET_UP
    threading.Thread(target=_login, daemon=True, name="google-login").start()
    return "I've opened Google sign-in in your browser. Please approve access, sir."


# ---------- Gmail ----------

@skill()
def read_email(query: str = "is:unread in:inbox", count: int = 5) -> str:
    """Read recent emails: sender, subject and a short preview. query is a Gmail search,
    e.g. 'is:unread in:inbox' (default), 'from:amazon', 'subject:interview newer_than:7d'."""
    gmail = _service("gmail", "v1").users()
    found = gmail.messages().list(userId="me", q=query, maxResults=max(1, min(count, 10))).execute()
    lines = []
    for ref in found.get("messages", []):
        msg = gmail.messages().get(userId="me", id=ref["id"], format="metadata",
                                   metadataHeaders=["From", "Subject"]).execute()
        head = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        lines.append(f"From {head.get('From', '?')}: {head.get('Subject', '(no subject)')} - "
                     f"{msg.get('snippet', '')[:150]}")
    return "\n".join(lines) or "No emails match."


@skill(confirm=True)
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email from the user's Gmail. to must be a full email address."""
    if "@" not in to:
        return "error: I need the full email address, sir."
    msg = EmailMessage()
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    _service("gmail", "v1").users().messages().send(userId="me", body={"raw": raw}).execute()
    return f"Email sent to {to}."


# ---------- Calendar ----------

def event_line(event: dict) -> str:
    start = event["start"].get("dateTime") or event["start"].get("date")
    when = datetime.fromisoformat(start)
    label = when.strftime("%a %d %b %I:%M %p") if "T" in start else when.strftime("%a %d %b") + " (all day)"
    return f"{label}: {event.get('summary', '(no title)')}"


@skill()
def calendar_events(days: int = 1) -> str:
    """List the user's Google Calendar events for today and the next days. days=1 means today only."""
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    items = _service("calendar", "v3").events().list(
        calendarId="primary", timeMin=today.isoformat(), timeMax=(today + timedelta(days=max(1, days))).isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=25,
    ).execute().get("items", [])
    return "\n".join(event_line(e) for e in items) or "No events on the calendar."


@skill(confirm=True)
def add_calendar_event(title: str, start: str, minutes: int = 60) -> str:
    """Add an event to the user's Google Calendar. start is local date and time as 'YYYY-MM-DD HH:MM'."""
    try:
        begin = datetime.fromisoformat(start).astimezone()
    except ValueError:
        return "error: start must look like 2026-11-17 15:00"
    end = begin + timedelta(minutes=max(1, minutes))
    _service("calendar", "v3").events().insert(calendarId="primary", body={
        "summary": title, "start": {"dateTime": begin.isoformat()}, "end": {"dateTime": end.isoformat()},
    }).execute()
    return f"Added {title} on {begin:%a %d %b at %I:%M %p}."


# ---------- Tasks (used instead of Google Keep, which has no API for personal accounts) ----------

def _find_list(tasks, name: str) -> dict | None:
    lists = tasks.tasklists().list(maxResults=100).execute().get("items", [])
    return next((l for l in lists if l["title"].strip().lower() == name.strip().lower()), None)


@skill()
def list_tasks(list_name: str = "") -> str:
    """Read the user's open Google Tasks to-dos. Give list_name (e.g. 'bucket list') for one list,
    or leave it empty for every list."""
    tasks = _service("tasks", "v1")
    if list_name:
        found = _find_list(tasks, list_name)
        if found is None:
            return f"There's no task list called {list_name}."
        lists = [found]
    else:
        lists = tasks.tasklists().list(maxResults=100).execute().get("items", [])
    out = []
    for tl in lists:
        items = tasks.tasks().list(tasklist=tl["id"], showCompleted=False, maxResults=50).execute().get("items", [])
        titles = [t["title"] for t in items if t.get("title")]
        out.append(f"{tl['title']}: " + ("; ".join(titles) if titles else "nothing open"))
    return "\n".join(out) or "No task lists."


@skill()
def add_task(title: str, list_name: str = "") -> str:
    """Add a to-do to Google Tasks. list_name picks the list, e.g. 'bucket list', and creates it if missing;
    leave it empty for the default list."""
    tasks = _service("tasks", "v1")
    list_id = "@default"
    if list_name:
        found = _find_list(tasks, list_name) or tasks.tasklists().insert(body={"title": list_name.strip().title()}).execute()
        list_id = found["id"]
    tasks.tasks().insert(tasklist=list_id, body={"title": title}).execute()
    return f"Added {title}" + (f" to {list_name}." if list_name else ".")
