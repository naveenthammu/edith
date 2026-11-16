"""Keep the user's Naukri profile fresh: recruiters see recently updated profiles first.

The user logs in once in a real browser window (naukri_login); the session lives in a browser profile
under data/browser/naukri. EDITH never sees or stores the password.
"""
import logging
import threading
import time
from datetime import date
from pathlib import Path

from skills import skill

log = logging.getLogger(__name__)

DATA = Path(__file__).resolve().parent.parent / "data"
PROFILE_DIR = DATA / "browser" / "naukri"
LAST_FILE = DATA / "naukri_last_refresh.txt"
TRIED_FILE = DATA / "naukri_last_try.txt"  # on disk: each restart used to pop up the browser again
LOGIN_URL = "https://www.naukri.com/nlogin/login"
PROFILE_URL = "https://www.naukri.com/mnjuser/profile"
LOGIN_WAIT_SECONDS = 300
CHECK_EVERY_SECONDS = 3600
_lock = threading.Lock()  # one browser on the profile at a time; Chrome refuses a second one anyway
announce = lambda text: None  # main.py points this at the speaker


def _open_browser(p, headless: bool):
    """Persistent context on the user's own Chrome or Edge; falls back to Playwright's Chromium."""
    last_error = None
    for channel in ("chrome", "msedge", None):
        try:
            return p.chromium.launch_persistent_context(str(PROFILE_DIR), channel=channel, headless=headless)
        except Exception as e:
            last_error = e
    raise RuntimeError(f"no usable browser (install Chrome, or run: playwright install chromium): {last_error}")


def _login_flow() -> None:
    from playwright.sync_api import sync_playwright

    with _lock, sync_playwright() as p:
        ctx = _open_browser(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(LOGIN_URL)
            deadline = time.time() + LOGIN_WAIT_SECONDS
            while time.time() < deadline and "login" in page.url:
                page.wait_for_timeout(2000)
            page.wait_for_timeout(3000)  # let the session cookies settle before closing
        except Exception:
            log.info("naukri login window closed")  # user closed it; whatever session exists is kept
        finally:
            ctx.close()


@skill()
def naukri_login() -> str:
    """Open Naukri in a browser window so the user can log in once. Needed before refresh_naukri works."""
    threading.Thread(target=_login_flow, daemon=True, name="naukri-login").start()
    return "I've opened Naukri. Please log in, sir. I'll keep the session for the daily refresh."


def refresh_profile() -> str:
    """Re-save the resume headline unchanged, which marks the profile as updated today.

    Runs in a visible browser window: Naukri denies headless browsers, and EDITH doesn't disguise itself.
    """
    from playwright.sync_api import sync_playwright

    if not PROFILE_DIR.exists():
        return "error: not logged in to Naukri yet. Ask me to log in to Naukri first."
    with _lock, sync_playwright() as p:
        ctx = _open_browser(p, headless=False)
        try:
            page = ctx.new_page()
            page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(5000)
            if "access denied" in page.title().lower():
                return "error: Naukri blocked the automated visit. Please refresh the profile by hand today."
            if "login" in page.url:
                return "error: the Naukri login has expired. Ask me to log in to Naukri again."
            # shortcut: tied to Naukri's current page layout (text "Resume headline" + an edit icon after it);
            # if Naukri redesigns the page, update these two locators.
            page.locator("xpath=//*[normalize-space()='Resume headline']/following::*[contains(@class,'edit')][1]") \
                .click(timeout=15000)
            page.get_by_role("button", name="Save").click(timeout=15000)
            page.wait_for_timeout(3000)
        except Exception as e:
            if type(e).__name__ == "TargetClosedError":
                return "error: the Naukri window was closed before I finished. Ask me to refresh Naukri again."
            log.exception("naukri refresh failed")
            return f"error: couldn't refresh Naukri ({type(e).__name__})"
        finally:
            ctx.close()
    LAST_FILE.write_text(date.today().isoformat(), encoding="utf-8")
    return "Your Naukri profile is refreshed for today."


@skill()
def refresh_naukri() -> str:
    """Refresh the user's Naukri profile now so it shows as recently updated to recruiters."""
    return refresh_profile()


def _read_day(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def daily_once(today: str) -> None:
    """One attempt per day, so a failure doesn't pop up a browser every hour or on every restart."""
    if not PROFILE_DIR.exists() or today in (_read_day(LAST_FILE), _read_day(TRIED_FILE)):
        return
    TRIED_FILE.write_text(today, encoding="utf-8")
    # a browser window is about to pop up on its own: say why, so it isn't closed as a stray window
    announce("Refreshing your Naukri profile, sir. Please leave the browser window open for a few seconds.")
    result = refresh_naukri()
    log.info("daily naukri refresh: %s", result)
    if result.startswith("error: "):  # otherwise an expired login fails silently every day
        announce(f"Naukri refresh failed, sir: {result.removeprefix('error: ')}")


def start_daily() -> None:
    """Refresh once a day in the background, but only after the user has logged in once."""

    def loop():
        while True:
            daily_once(date.today().isoformat())
            time.sleep(CHECK_EVERY_SECONDS)

    threading.Thread(target=loop, daemon=True, name="naukri-daily").start()
