"""Exact voice commands for reviewing/sending a journalled email outside the LLM.

The authenticated configured client must be an adult/elder in a trusted private
session. This is not voice biometrics. Provider connection is a separate choice.
"""
import logging
import re
import time


def configured_message_review(hub, identity):
    """Load an optional member account without starting OAuth or sending mail."""
    if (identity is None or identity.get('role') not in {'adult', 'elder'}
            or not identity.get('principal_id')
            or identity.get('capabilities', {}).get('email_proposals') is not True):
        return None
    from connectors.gmail_account import GmailAccountError, gmail_from_env
    from connectors.message_coordinator import MessageCoordinator

    try:
        adapter = gmail_from_env(identity)
    except GmailAccountError:
        logging.getLogger(__name__).warning('Gmail sending disabled: member account configuration could not be verified.')
        adapter = None
    coordinator = MessageCoordinator(hub, adapter) if adapter is not None else None
    return MessageReview(hub, expected_principal=identity['principal_id'], coordinator=coordinator,
                         sender_email=adapter.sender_email if adapter is not None else None)


class MessageReview:
    def __init__(self, hub, *, expected_principal, coordinator=None, sender_email=None, clock=time.time):
        if not isinstance(expected_principal, str) or not expected_principal:
            raise ValueError('Configure the expected authenticated message owner.')
        self.hub, self.expected_principal = hub, expected_principal
        self.coordinator, self.clock = coordinator, clock
        if sender_email is not None:
            from connectors.gmail import validate_address
            validate_address(sender_email)
        self.sender_email = sender_email
        self._reviewed = None

    def clear(self):
        self._reviewed = None

    def _authorized(self):
        current = self.hub.capabilities()
        return (current.get('principal_id') == self.expected_principal
                and current.get('role') in {'adult', 'elder'}
                and current.get('capabilities', {}).get('email_proposals') is True)

    def _row(self, message_id):
        from connectors.message_coordinator import validate_message
        row = validate_message(self.hub.message(message_id))
        if row['id'] != message_id:
            raise ValueError('Message identity changed.')
        return row

    def _review(self, prefix):
        rows = self.hub.messages().get('messages')
        if not isinstance(rows, list):
            raise ValueError('Invalid message list.')
        pending = [row for row in rows if isinstance(row, dict)
                   and row.get('status') in {'awaiting_approval', 'approved'}
                   and type(row.get('expires_at')) is int and row['expires_at'] > self.clock()
                   and isinstance(row.get('id'), str) and (not prefix or row['id'].startswith(prefix))]
        self.clear()
        if not pending:
            return 'No matching unexpired email draft is ready for review. Prepare a new draft first.'
        if len(pending) != 1:
            identifiers = ', '.join(row['id'][:8] for row in pending[:10])
            return 'More than one email draft matches. Say review email followed by its eight-character ID: ' + identifiers
        row = self._row(pending[0]['id'])
        if row['expires_at'] <= self.clock() or row['status'] not in {'awaiting_approval', 'approved'}:
            return 'This draft changed or expired. Refresh the draft before reviewing it.'
        self._reviewed = (row['id'], row['argument_hash'])
        payload = row['payload']
        sender = f'From: {self.sender_email}. ' if self.sender_email is not None else ''
        return (f"Draft {row['id']}. {sender}Recipient: {payload['recipient']}. Subject: {payload['subject']}. "
                f"Body: {payload['body']}\nNothing has been sent. Say send reviewed email for a fresh confirmation, "
                'or reject reviewed email to cancel this proposal.')

    def handle(self, text, *, confirm, still_current):
        if not isinstance(text, str) or len(text) > 100:
            return None
        command = ' '.join(text.strip().lower().rstrip('.!?').split())
        review = re.fullmatch(r'review email(?: ([a-f0-9-]{8,36}))?', command)
        if review is None and command not in {'send reviewed email', 'reject reviewed email'}:
            return None
        try:
            if still_current() is not True or not self._authorized():
                self.clear()
                return 'This email workflow requires the current paired adult or elder session.'
            if review is not None:
                result = self._review(review.group(1))
                if still_current() is not True or not self._authorized():
                    self.clear()
                    return 'Email review was cancelled because the session changed.'
                return result
            if self._reviewed is None:
                return 'Review an email draft first by saying review email.'
            message_id, digest = self._reviewed
            row = self._row(message_id)
            if row['argument_hash'] != digest or row['expires_at'] <= self.clock() or row['status'] not in {'awaiting_approval', 'approved'}:
                self.clear()
                return 'The reviewed draft changed or expired. Review a fresh draft before sending.'
            if command == 'reject reviewed email':
                self.clear()
                if still_current() is not True or not self._authorized():
                    return 'Email rejection was cancelled because the session changed.'
                receipt = self.hub.decide_message(message_id, 'reject', digest)
                if receipt.get('status') != 'rejected':
                    return 'The hub did not confirm rejection. Check this draft before taking another action.'
                return 'The email proposal was rejected. It was not sent by this action.'
            if self.coordinator is None:
                return 'Gmail sending is not configured for this member. The draft is saved and remains unapproved.'
            payload = row['payload']
            sender = f'from {self.sender_email} ' if self.sender_email is not None else ''
            prompt = (f"Send this exact email through Gmail {sender}to {payload['recipient']}? "
                      f"Subject: {payload['subject']}. Body: {payload['body']}. Say yes to send this content.")
            if confirm(prompt) is not True or still_current() is not True:
                self.clear()
                return 'Email send cancelled.'
            if not self._authorized():
                self.clear()
                return 'Email send cancelled because member access changed.'
            latest = self._row(message_id)
            if (still_current() is not True or latest['argument_hash'] != digest
                    or latest['expires_at'] <= self.clock() or latest['status'] not in {'awaiting_approval', 'approved'}):
                self.clear()
                return 'The reviewed draft changed or expired. It must be reviewed again.'
            self.clear()  # Even an uncertain execution must not be replayable by repeating the voice command.
            def can_execute():
                return still_current() is True and self._authorized() and still_current() is True

            result = self.coordinator.decide_and_execute(message_id, digest, True, can_execute=can_execute)
            if result.get('status') == 'accepted':
                return 'Gmail accepted this email. Recipient delivery and reading are unverified.'
            if result.get('status') == 'failed':
                return 'The provider did not accept this email. Check the recorded failure before preparing another draft.'
            return 'The email outcome is unknown. Check Gmail and the recorded proposal before attempting another send.'
        except Exception:
            # Errors can occur after an effect but before its receipt reaches us.
            # Do not expose provider/private payloads or assert that nothing happened.
            self.clear()
            return 'The email operation could not be confirmed. Inspect the draft and Gmail before attempting a send again.'
