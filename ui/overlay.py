import ctypes
import json
import logging
import platform
from pathlib import Path

log = logging.getLogger("edith.ui")

HTML = Path(__file__).with_name("orb.html")
WIDTH, HEIGHT, MARGIN = 280, 280, 16  # collapsed card; grows when the history panel opens
OPACITY = 0.9   # whole-window alpha: the desktop shows faintly through the card
CARD = (4, 4, 6)  # must match --card in orb.html


class _Api:
    """Methods the page may call as pywebview.api.<name>()."""

    def __init__(self, overlay):
        self._overlay = overlay  # underscore: pywebview does not expose it to the page

    def close(self) -> None:
        self._overlay.hide()

    def fit(self, height) -> None:
        self._overlay.window.resize(WIDTH, max(HEIGHT, min(int(height), 700)))


class Overlay:
    """Siri-style card: translucent, rounded, always on top, top-right corner, hidden until woken.

    WebView2 can't be per-pixel transparent to the desktop in a normal window (checked: colour-key
    and DWM alpha both leave it opaque), but whole-window opacity works, so the orb sits on a glass card.
    """

    def __init__(self):
        import webview

        self._webview = webview
        screen = webview.screens[0]
        self.window = webview.create_window(
            "EDITH",
            str(HTML),
            js_api=_Api(self),
            width=WIDTH,
            height=HEIGHT,
            x=screen.width - WIDTH - MARGIN,
            y=MARGIN,
            frameless=True,
            easy_drag=False,
            on_top=True,
            resizable=False,
            hidden=True,
            focus=False,
            shadow=False,
            background_color="#%02x%02x%02x" % CARD,
        )
        if platform.system() == "Windows":  # opacity/corner tweaks go through WinForms; elsewhere pywebview's defaults stand
            self.window.events.loaded += self._style_card

    def _style_card(self) -> None:
        from System import Action

        form = self.window.native

        def apply():
            form.Opacity = OPACITY
            # Windows 11 rounded corners: DWMWA_WINDOW_CORNER_PREFERENCE = DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(int(form.Handle.ToInt64())), 33, ctypes.byref(ctypes.c_int(2)), 4)

        form.Invoke(Action(apply))

    def _js(self, script: str) -> None:
        if not self.window.events.loaded.wait(10):
            log.warning("overlay page did not load; skipped %s", script[:40])
            return
        try:
            self.window.evaluate_js(script)
        except Exception:
            log.exception("overlay script failed")

    def show(self) -> None:
        self.window.show()
        self._js("orb.wake()")

    def hide(self) -> None:
        self._js("orb.sleep()")
        self.window.hide()

    def add(self, who: str, text: str) -> None:
        self._js(f"orb.add({json.dumps(who)}, {json.dumps(text)})")

    def run(self, func) -> None:
        """Blocks: the GUI loop must own the main thread, so EDITH runs in func's thread."""
        self._webview.start(func)
