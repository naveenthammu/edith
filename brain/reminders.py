"""Idle-time reminder delivery through the trusted local hub and speech worker.

The hub owns persistence, due times and 120-second claims. Speech completion
does not mean the owner heard the reminder. A process crash or expired lease
can cause redelivery; this is not an exactly-once delivery service.
"""
import logging
import threading
import time

log = logging.getLogger(__name__)
LEASE_SECONDS = 120


class ReminderDispatcher:
    """Claim at most one reminder while the configured owner is safely idle.

    can_announce must be a trusted local gate (owner mode and microphone state),
    not a model decision. turn_lock must also guard whole user conversations.
    Each iteration rechecks server principal/role/reminder capability, including
    after a claim and after speech. Constructor/import never starts a worker.

    poll_once returns idle, announced, retry, or unknown. Unknown mutation
    responses are never retried immediately; the dispatcher leaves the lease
    for reconciliation and pauses until its deadline (up to 120 seconds).
    """

    def __init__(self, hub, speaker, can_announce, turn_lock, *, expected_principal,
                 clock=time.time, stop_event=None, poll_interval=5.0):
        if not isinstance(expected_principal, str) or not 1 <= len(expected_principal) <= 128:
            raise ValueError("A configured reminder owner principal is required")
        if (isinstance(poll_interval, bool) or not isinstance(poll_interval, (float, int))
                or not 0 < poll_interval <= 3600):
            raise ValueError("Reminder poll interval must be positive and at most one hour")
        self.hub = hub
        self.speaker = speaker
        self.can_announce = can_announce
        self.turn_lock = turn_lock
        self.expected_principal = expected_principal
        self.clock = clock
        self._stop = stop_event if stop_event is not None else threading.Event()
        self._interval = poll_interval
        self._not_before = 0
        self._state = threading.Lock()
        self._speaking = False
        self._thread = None

    def _gate(self):
        if self._stop.is_set():
            return False
        try:
            return self.can_announce() is True
        except Exception:
            log.warning("Reminder announcement gate could not be checked.")
            return False

    def _authorized(self, generation=None):
        if not self._gate():
            return False
        if generation is not None and self.speaker.generation != generation:
            return False
        try:
            identity = self.hub.capabilities()
            allowed = (isinstance(identity, dict)
                       and identity.get("principal_id") == self.expected_principal
                       and identity.get("role") in {"adult", "elder"}
                       and isinstance(identity.get("capabilities"), dict)
                       and identity["capabilities"].get("reminders") is True)
        except Exception:
            log.warning("Reminder owner access could not be verified.")
            return False
        return (allowed and self._gate()
                and (generation is None or self.speaker.generation == generation))

    def _defer_unknown(self, row=None):
        now = self.clock()
        expiry = row.get("lease_expires_at") if isinstance(row, dict) else None
        self._not_before = (min(expiry, now + LEASE_SECONDS)
                            if type(expiry) is int and expiry > now else now + LEASE_SECONDS)

    def _valid_claim(self, row):
        if not isinstance(row, dict):
            return False
        for key in ("id", "delivery_id"):
            if not isinstance(row.get(key), str) or not 1 <= len(row[key]) <= 128:
                return False
        return (isinstance(row.get("text"), str) and 1 <= len(row["text"]) <= 400
                and bool(row["text"].strip()) and row.get("status") == "scheduled"
                and type(row.get("due_at")) is int and row["due_at"] <= self.clock()
                and type(row.get("lease_expires_at")) is int
                and row["lease_expires_at"] > self.clock())

    def _receipt(self, row, status):
        if row["lease_expires_at"] <= self.clock():
            self._defer_unknown(row)
            log.warning("Reminder lease expired before delivery could be recorded.")
            return "unknown"
        try:
            result = self.hub.complete_reminder_delivery(row["id"], row["delivery_id"], status)
            expected_status = "announced" if status == "announced" else "scheduled"
            if (not isinstance(result, dict) or result.get("id") != row["id"]
                    or result.get("status") != expected_status):
                raise ValueError("Unconfirmed receipt")
        except Exception:
            self._defer_unknown(row)
            # Do not print exception/response contents: they may contain a note.
            log.warning("Reminder delivery status is unconfirmed; the lease was left for reconciliation.")
            return "unknown"
        return status

    def poll_once(self):
        if self.clock() < self._not_before or not self._gate():
            return "idle"
        if not self.turn_lock.acquire(blocking=False):
            return "idle"
        try:
            if not self._authorized():
                return "idle"
            generation = self.speaker.generation
            try:
                result = self.hub.claim_due_reminder()
            except Exception:
                self._defer_unknown()
                log.warning("Reminder claim status is unknown; no claim retry will occur during the lease window.")
                return "unknown"
            if isinstance(result, dict) and result.get("reminder", False) is None:
                return "idle"
            row = result.get("reminder") if isinstance(result, dict) else None
            if not self._valid_claim(row):
                self._defer_unknown(row)
                log.warning("Reminder claim was invalid or expired; no speech was started.")
                return "unknown"
            if not self._authorized(generation):
                return self._receipt(row, "retry")
            if row["lease_expires_at"] <= self.clock():
                self._defer_unknown(row)
                log.warning("Reminder lease expired during access verification; no speech was started.")
                return "unknown"
            with self._state:
                if self._stop.is_set():
                    speak = False
                else:
                    self._speaking = True
                    speak = True
            if not speak:
                return self._receipt(row, "retry")
            try:
                outcome = self.speaker.say_tracked(row["text"], expected_generation=generation)
            except Exception:
                outcome = "failed"
                log.warning("Reminder speech did not report completion.")
            finally:
                with self._state:
                    self._speaking = False
            status = "announced" if outcome == "completed" and self._authorized(generation) else "retry"
            return self._receipt(row, status)
        finally:
            self.turn_lock.release()

    def start(self) -> bool:
        """Start once explicitly; a stopped instance stays stopped."""
        with self._state:
            if self._stop.is_set() or (self._thread is not None and self._thread.is_alive()):
                return False
            self._thread = threading.Thread(target=self._run, daemon=True, name="reminders")
            self._thread.start()
            return True

    def _run(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                log.warning("Reminder dispatcher could not finish this check.")
            self._stop.wait(self._interval)

    def stop(self, timeout=1.0) -> bool:
        """Stop polling and cancel only speech this dispatcher is performing.

        A pending HTTP/native operation cannot be forcibly interrupted; False
        means the daemon has not exited yet. Its lease remains server-owned.
        """
        with self._state:
            self._stop.set()
            if self._speaking:
                self.speaker.stop()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, timeout))
        return thread is None or not thread.is_alive()
