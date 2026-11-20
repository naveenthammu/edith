"""Member-bound local reminders, contacts and drafts. No provider sends or approvals."""
import hashlib
import json
import re
import time
import uuid
from datetime import datetime

from brain.hub import HubError
from skills import Skill, _schema


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _rows(response, key):
    rows = response.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise HubError('The hub returned an invalid household record list.')
    return rows


def _bounded_list(key, rows, **metadata):
    selected = rows[:20]
    value = {key: selected, 'matches': len(rows), 'truncated': len(rows) > len(selected), **metadata}
    while len(_json(value)) > 12_000 and selected:
        selected.pop()
        value['truncated'] = True
    return _json(value)


def _contact_update_confirmation(arguments):
    """Trusted full-replacement readback; empty channels must be spoken as clears."""
    limits = {'contact_id': 128, 'expected_version': 64, 'display_name': 100, 'email': 254, 'phone_e164': 16}
    if (set(arguments) != set(limits)
            or any(not isinstance(value, str) or len(value) > limits[key]
                   or (value and not value.isprintable()) for key, value in arguments.items())
            or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', arguments['contact_id']) is None
            or re.fullmatch(r'[0-9a-f]{64}', arguments['expected_version']) is None
            or not arguments['display_name'].strip()
            or not (arguments['email'] or arguments['phone_e164'])):
        raise ValueError('A complete bounded contact replacement is required.')
    return (f"replace ALL fields of contact {arguments['contact_id'][:8]}. "
            f"Name: {arguments['display_name']}. "
            f"Email: {arguments['email'] or 'CLEAR EMAIL'}. "
            f"Phone: {arguments['phone_e164'] or 'CLEAR PHONE'}.")


def household_tools(hub, identity):
    if identity.get('role') not in {'adult', 'elder'} or not identity.get('principal_id'):
        raise HubError('Private household tools require a paired adult or elder identity.')
    owner = identity['principal_id']
    draft_intents = {}

    def create_reminder(text: str, when: str) -> str:
        """Schedule one reminder after readback confirmation. when must be ISO date/time with explicit UTC offset; clarify an ambiguous time first."""
        if not isinstance(text, str) or not text.strip() or len(text) > 400:
            raise ValueError('Reminder text must contain 1 to 400 characters.')
        try:
            due = datetime.fromisoformat(when)
            if due.tzinfo is None or due.utcoffset() is None:
                raise ValueError()
            due_at = int(due.timestamp())
        except (TypeError, ValueError, OverflowError):
            raise ValueError('Use an ISO date/time with an explicit UTC offset, such as 2026-11-21T08:00:00+05:30.') from None
        # Repeating an identical reminder does not create duplicate announcements,
        # including after a lost create response or client restart.
        key = hashlib.sha256(_json([owner, text, due_at]).encode()).hexdigest()
        row = hub.create_reminder(text, due_at, key)
        return _json({**row, 'requested_time': due.isoformat(),
                      'notice': 'Saved locally. Announcements require EDITH running in this private member session; this is not evidence the task was performed.'})

    def list_reminders(status: str = 'active') -> str:
        """List this member's reminders. active includes scheduled/announced; all also includes acknowledged/cancelled."""
        if status not in {'active', 'all'}:
            raise ValueError('Choose active or all reminder states.')
        rows = _rows(hub.reminders(), 'reminders')
        if status == 'active':
            rows = [row for row in rows if row.get('status') in {'scheduled', 'announced'}]
        return _bounded_list('reminders', rows)

    def cancel_reminder(reminder_id: str) -> str:
        """Cancel one exact owned reminder ID after confirmation; never guess an ambiguous ID."""
        return _json(hub.cancel_reminder(reminder_id))

    def acknowledge_reminder(reminder_id: str) -> str:
        """Record the user's explicit acknowledgement of one reminder; this does not prove the underlying task was performed."""
        return _json(hub.acknowledge_reminder(reminder_id))

    def find_contacts(name: str) -> str:
        """Find owned contacts by name, exact email or exact international phone. Ask the user to choose ambiguous matches; never infer an address or country code."""
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 254:
            raise ValueError('Specify a contact name, exact email or international phone of 1 to 254 characters.')
        query = name.strip().casefold()
        rows = _rows(hub.contacts(), 'contacts')
        exact = [row for row in rows if row['display_name'].casefold() == query
                 or (row.get('email') or '').casefold() == query
                 or row.get('phone_e164') == name.strip()]
        selected = exact or [row for row in rows if query in row['display_name'].casefold()]
        return _bounded_list('contacts', selected, ambiguous=len(selected) != 1, match_kind='exact' if exact else 'partial')

    def add_contact(display_name: str, email: str = '', phone_e164: str = '') -> str:
        """Save a supplied name and email and/or international phone after confirmation. Phone uses +countrycode and digits. Never invent an email, country code, or WhatsApp availability."""
        return _json(hub.add_contact(display_name, email or None, phone_e164=phone_e164 or None))

    def update_contact(contact_id: str, expected_version: str, display_name: str, email: str, phone_e164: str) -> str:
        """Replace ALL fields of an exact contact after confirmation. Use its current version and retain existing email/phone unless the user requested a change; empty string clears that field. At least one address is required."""
        return _json(hub.update_contact(contact_id, expected_version, display_name, email or None,
                                       phone_e164=phone_e164 or None))

    def delete_contact(contact_id: str) -> str:
        """Delete one exact contact after confirmation. Unsent proposals referencing it cannot be approved or claimed."""
        return _json(hub.delete_contact(contact_id))

    def draft_email(contact_id: str, contact_version: str, subject: str, body: str) -> str:
        """Prepare an email draft for an exact user-selected contact/version. This never sends; user must review in the trusted command flow."""
        key = hashlib.sha256(_json([contact_id, contact_version, subject, body]).encode()).hexdigest()
        now = time.monotonic()
        for expired in [k for k, (_, created) in draft_intents.items() if now - created >= 300]:
            del draft_intents[expired]
        if key not in draft_intents:
            if len(draft_intents) >= 128:
                raise HubError('Too many pending draft requests; review existing messages first.')
            draft_intents[key] = (uuid.uuid4().hex, now)
        row = hub.propose_message(contact_id, contact_version, subject, body, draft_intents[key][0])
        return _json({**row, 'sent': False,
                      'next_step': 'Say review email to inspect the recipient and full draft, then send reviewed email. Sending requires a configured provider and a fresh exact-content confirmation.'})

    def list_email_drafts() -> str:
        """List email proposal IDs/statuses. Accepted means Gmail accepted the request, not that the recipient received or read it."""
        rows = _rows(hub.messages(), 'messages')
        summaries = [{'id': row['id'], 'status': row['status'], 'recipient': row['payload']['recipient'],
                      'subject': row['payload']['subject'], 'expires_at': row['expires_at']} for row in rows]
        return _bounded_list('messages', summaries)

    confirmations = {create_reminder, cancel_reminder, acknowledge_reminder, add_contact, update_contact, delete_contact}
    functions = (create_reminder, list_reminders, cancel_reminder, acknowledge_reminder,
                 find_contacts, add_contact, update_contact, delete_contact, draft_email, list_email_drafts)
    return {fn.__name__: Skill(fn, fn in confirmations, _schema(fn), local_only=True,
                              confirmation_formatter=_contact_update_confirmation if fn is update_contact else None)
            for fn in functions}
