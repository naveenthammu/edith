"""Read the notifications Windows shows (WhatsApp messages, emails, Teams...): app, sender and preview text.

Needs Settings > Privacy & security > Notifications > "Let apps access your notifications" switched on.
Only notifications still in the Windows notification center can be read.
"""
import asyncio
import platform

from skills import skill

MAX_SHOWN = 10
NO_ACCESS = ("error: notification access is off. Turn on Settings > Privacy & security > Notifications > "
             "Let apps access your notifications.")


def _toasts() -> list[tuple[str, list[str]]]:
    """(app name, text lines) for each toast in the notification center, oldest first."""
    from winrt.windows.ui.notifications import KnownNotificationBindings, NotificationKinds
    from winrt.windows.ui.notifications.management import (UserNotificationListener,
                                                            UserNotificationListenerAccessStatus)

    async def fetch():
        listener = UserNotificationListener.current
        if await listener.request_access_async() != UserNotificationListenerAccessStatus.ALLOWED:
            raise PermissionError
        out = []
        for n in await listener.get_notifications_async(NotificationKinds.TOAST):
            binding = n.notification.visual.get_binding(KnownNotificationBindings.toast_generic)
            texts = [t.text for t in binding.get_text_elements()] if binding else []
            out.append((n.app_info.display_info.display_name, texts))
        return out

    return asyncio.run(fetch())


def format_notifications(toasts: list[tuple[str, list[str]]], app: str = "") -> str:
    want = app.strip().lower()
    picked = [(name, texts) for name, texts in reversed(toasts) if not want or want in name.lower()]
    if not picked:
        return f"No {app} notifications." if want else "No notifications."
    lines = [f"{name}: {' - '.join(t for t in texts if t)}" for name, texts in picked[:MAX_SHOWN]]
    count = len(picked)
    return f"{count} notification{'s' if count != 1 else ''}, newest first:\n" + "\n".join(lines)


@skill()
def read_notifications(app: str = "") -> str:
    """Read recent notifications on this PC, e.g. new WhatsApp messages, emails or Teams chats.
    Give app (e.g. 'whatsapp', 'gmail', 'teams') to read only that app's notifications."""
    if platform.system() != "Windows":
        return "error: reading notifications only works on Windows for now"
    try:
        return format_notifications(_toasts(), app)
    except PermissionError:
        return NO_ACCESS
