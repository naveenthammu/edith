"""Exact WhatsApp Business review commands in the existing voice UI, outside the model."""
from copy import deepcopy
import logging
import math
import re
import time

from connectors.whatsapp_business_coordinator import BusinessMessageCoordinator, validate_business_message


_ID = re.compile(r'[a-f0-9-]{8,36}\Z')


def configured_business_review(hub, identity):
    if (not isinstance(identity, dict) or identity.get('role') not in {'adult', 'elder'}
            or identity.get('capabilities', {}).get('business_message_proposals') is not True):
        return None
    from brain.business_tools import configured_business_connection
    try:
        connection = configured_business_connection(hub, identity)
        return (BusinessMessageReview(hub, connection, coordinator=BusinessMessageCoordinator(hub, connection))
                if connection is not None else None)
    except Exception:
        logging.getLogger(__name__).warning('Business message review disabled: the local connection could not be verified.')
        return None


class BusinessMessageReview:
    def __init__(self, hub, connection, *, coordinator=None, clock=time.time):
        if (not isinstance(connection.principal_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', connection.principal_id)
                or not isinstance(connection.connection_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', connection.connection_id)
                or not callable(clock)):
            raise ValueError('Configure the exact paired business sender.')
        self.hub, self.connection, self.coordinator, self.clock = hub, connection, coordinator, clock
        self.owner, self.connection_id = connection.principal_id, connection.connection_id
        self._account = deepcopy(connection.account_snapshot)
        self.clear()

    def clear(self):
        self._reviewed = None

    def _identity(self, value):
        return (isinstance(value, dict) and value.get('principal_id') == self.owner
            and value.get('role') in {'adult', 'elder'}
            and value.get('capabilities', {}).get('business_message_proposals') is True)

    def _allowed(self, still_current):
        try:
            if still_current() is not True or not self._identity(self.hub.capabilities()):
                return False
            self.connection.revalidate()
            return (self.connection.account_snapshot == self._account
                and self._identity(self.hub.capabilities()) and still_current() is True)
        except Exception:
            return False

    def _require(self, still_current):
        if not self._allowed(still_current):
            raise ValueError('Business session or sender changed.')

    def _bound(self, value, *, statuses=('awaiting_approval', 'approved')):
        row = validate_business_message(value)
        stamp = self.clock()
        account = row['payload']['prepared']['account']
        if (type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0
                or row['expires_at'] <= stamp or row['created_at'] > stamp+30
                or row['status'] not in statuses or not _ID.fullmatch(row['id'])
                or {k:v for k,v in account.items() if k!='verified_at'} != {k:v for k,v in self._account.items() if k!='verified_at'}):
            raise ValueError('The business proposal expired or its exact sender changed.')
        return row

    def _row(self, identifier, still_current):
        self._require(still_current)
        row = self._bound(self.hub.business_message(identifier))
        self._require(still_current)
        if row['id'] != identifier:
            raise ValueError('The business message identity changed.')
        return row

    def _fresh(self, identifier, still_current):
        if self.coordinator is None:
            raise ValueError('Business sending is not configured.')
        self._require(still_current)
        row = self._bound(self.coordinator.review(identifier, can_execute=lambda: self._allowed(still_current)))
        self._require(still_current)
        if row['id'] != identifier:
            raise ValueError('The refreshed business message identity changed.')
        return row

    @staticmethod
    def _summary(row):
        payload, prepared = row['payload'], row['payload']['prepared']
        account, template = prepared['account'], prepared['template']
        return (f"Business sender: {account['verified_name']}, {account['display_phone_number']}. "
            f"Recipient: {payload['recipient_name']}, {payload['recipient_e164']}. "
            f"Template: {template['name']}. Language: {template['language']}. Category: {template['category']}. "
            f"Exact message begins: {prepared['rendered_text']} Message ends. "
            "Provider charges may apply. Acceptance does not verify delivery or reading.")

    def _review(self, prefix, still_current):
        self.clear()
        self._require(still_current)
        value = self.hub.business_messages()
        self._require(still_current)
        if (not isinstance(value, dict) or set(value) != {'messages'} or not isinstance(value['messages'], list)
                or len(value['messages']) > 100):
            raise ValueError('Invalid business message list.')
        candidates = []
        for value in value['messages']:
            row = validate_business_message(value)
            if (row['status'] in {'awaiting_approval', 'approved'} and row['expires_at'] > self.clock()
                    and row['payload']['prepared']['account']['connection_id'] == self.connection_id
                    and (not prefix or row['id'].startswith(prefix))):
                candidates.append(self._bound(row))
        if not candidates:
            return 'No matching unexpired business message is ready. Prepare a local draft first.'
        if len(candidates) != 1:
            return ('More than one business message matches. Say review business message followed by its eight-character ID: '
                    + ', '.join(row['id'][:8] for row in candidates[:10]))
        selected = candidates[0]
        row = self._fresh(selected['id'], still_current)
        if row['argument_hash'] != selected['argument_hash']:
            raise ValueError('The selected business message changed during review.')
        self._reviewed = (row['id'], row['argument_hash'])
        return (f"Business message {row['id'][:8]}. " + self._summary(row)
            + ' Say send reviewed business message for fresh confirmation, or reject reviewed business message.')

    def _effect(self, reject, confirm, still_current):
        if self._reviewed is None:
            return 'Review an exact business message first by saying review business message.'
        identifier, argument_hash = self._reviewed
        row = self._row(identifier, still_current) if reject else self._fresh(identifier, still_current)
        if row['argument_hash'] != argument_hash:
            raise ValueError('The reviewed business message changed.')
        if not reject:
            prompt = self._summary(row) + ' Send this exact WhatsApp Business message once? Say yes to send it.'
            if confirm(prompt) is not True:
                self.clear()
                return 'Business message sending cancelled.'
            self._require(still_current)
            current = self._row(identifier, still_current)
            if current['argument_hash'] != argument_hash:
                raise ValueError('The confirmed business message changed.')
        self._require(still_current)
        self.clear()  # No repeated command or failed receipt can reuse this review.
        if self.coordinator is None:
            raise ValueError('Business sending is not configured.')
        result = validate_business_message(self.coordinator.decide_and_execute(identifier, argument_hash, not reject,
                                            can_execute=lambda: self._allowed(still_current)))
        self._require(still_current)
        if result['id'] != identifier or result['argument_hash'] != argument_hash:
            raise ValueError('The result does not match the reviewed proposal.')
        if reject and result['status'] == 'rejected':
            return 'The business message proposal was rejected.'
        if not reject and result['status'] == 'accepted':
            return 'WhatsApp accepted the business message request. Delivery and reading remain unverified.'
        if not reject and result['status'] == 'failed':
            return 'The business message attempt failed. Inspect the recorded result before preparing another proposal.'
        return 'Business message acceptance is unknown. Reconcile the recorded attempt before sending again.'

    def handle(self, text, *, confirm, still_current):
        if not isinstance(text, str) or len(text) > 150:
            return None
        command = ' '.join(text.strip().lower().rstrip('.!?').split())
        match = re.fullmatch(r'review business message(?: ([a-f0-9-]{8,36}))?', command)
        if match is None and command not in {'send reviewed business message', 'reject reviewed business message'}:
            return None
        try:
            self._require(still_current)
            result = (self._review(match.group(1), still_current) if match else
                      self._effect(command == 'reject reviewed business message', confirm, still_current))
            self._require(still_current)
            return result
        except Exception:
            self.clear()
            return ('The business message operation could not be confirmed because its state, sender, permission or session changed. '
                    'Inspect the recorded attempt before another send.')
