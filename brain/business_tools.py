"""Local WhatsApp template selection and drafts. No consent writes, approvals or sends."""
from copy import deepcopy
import hashlib
import json
import logging
import math
import os
import re
import time
import uuid

from skills import Skill, _schema
from connectors.whatsapp_templates import _render


class BusinessToolError(ValueError):
    """Safe local drafting error without credentials or permission evidence."""


def configured_business_connection(hub, identity):
    if (not isinstance(identity, dict) or identity.get('role') not in {'adult', 'elder'}
            or identity.get('capabilities', {}).get('business_message_proposals') is not True
            or not os.getenv('EDITH_WHATSAPP_CONNECTION_CONFIG')):
        return None
    from connectors.whatsapp_connection import whatsapp_connection_from_env
    try:
        return whatsapp_connection_from_env(hub, identity)
    except Exception:
        logging.getLogger(__name__).warning('Business messaging disabled: the selected local connection could not be verified.')
        return None


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _validated(kind, value):
    from connectors import whatsapp_business_coordinator as contract
    try:
        return getattr(contract, 'validate_' + kind)(value)
    except Exception:
        raise BusinessToolError('The business messaging record could not be verified.') from None


def _rows(response, key, limit=100):
    if (not isinstance(response, dict) or set(response) != {key}
            or not isinstance(response[key], list) or len(response[key]) > limit):
        raise BusinessToolError('The business messaging record list is invalid or exceeds its bound.')
    return response[key]


def _bounded(key, values):
    result = {key: values[:20], 'matches': len(values), 'truncated': len(values) > 20}
    while len(_json(result)) > 12_000 and result[key]:
        result[key].pop()
        result['truncated'] = True
    return _json(result)


def message_summary(value):
    row = _validated('business_message', value)
    payload, prepared = row['payload'], row['payload']['prepared']
    return {'id': row['id'], 'status': row['status'], 'expires_at': row['expires_at'],
        'recipient_name': payload['recipient_name'], 'recipient_e164': payload['recipient_e164'],
        'sender_name': prepared['account']['verified_name'], 'sender_phone': prepared['account']['display_phone_number'],
        'template_name': prepared['template']['name'], 'language': prepared['template']['language'],
        'category': prepared['template']['category'], 'rendered_text': prepared['rendered_text'],
        'delivery_status': 'unverified'}


def business_tools(hub, connection, identity, *, can_execute, clock=time.time):
    if (identity.get('role') not in {'adult', 'elder'} or not identity.get('principal_id')
            or connection.principal_id != identity['principal_id'] or not callable(can_execute) or not callable(clock)):
        raise BusinessToolError('A current paired adult or elder business connection is required.')
    identity = deepcopy(identity)
    intents = {}

    def now():
        value = clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise BusinessToolError('The business messaging clock is invalid.')
        return value

    def authorized(value):
        return (isinstance(value, dict) and value.get('principal_id') == identity['principal_id']
                and value.get('role') == identity['role']
                and value.get('capabilities', {}).get('business_message_proposals') is True)

    def require_current():
        try:
            if can_execute() is not True or not authorized(hub.capabilities()):
                raise ValueError()
            connection.revalidate()
            if not authorized(hub.capabilities()) or can_execute() is not True:
                raise ValueError()
        except Exception:
            raise BusinessToolError('Business access or this request changed. Reconnect or review in the current private session.') from None

    def bound(row):
        account = row['account']
        baseline = connection.account_snapshot
        return (account['connection_id'] == connection.connection_id
                and {k:v for k,v in account.items() if k != 'verified_at'} == {k:v for k,v in baseline.items() if k != 'verified_at'})

    def list_business_templates() -> str:
        """List this member's registered WhatsApp Business templates from local records. These are cached observations; trusted review refreshes provider approval. Does not send, verify a live account or grant consent."""
        require_current()
        rows = [_validated('binding', row) for row in _rows(hub.business_bindings(), 'bindings')]
        require_current()
        values = []
        for row in rows:
            if not bound(row):
                continue
            account, template = row['account'], row['template']
            values.append({'binding_id': row['id'], 'binding_version': row['version'],
                'sender_name': account['verified_name'], 'sender_phone': account['display_phone_number'],
                'template_name': template['name'], 'language': template['language'], 'category': template['category'],
                'body': template['body'], 'verified_at': account['verified_at'],
                'fresh_provider_review_required': True})
        return _bounded('templates', values)

    def draft_business_message(contact_id: str, contact_version: str, binding_id: str, binding_version: str, parameters: list) -> str:
        """Save a local WhatsApp Business draft for an exact user-selected contact/version and registered template/version. Parameters are plain text in positional order. Recipient permission must already exist; this never grants permission or sends. User must review and confirm outside the model."""
        require_current()
        if (any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f-]{36}', value) for value in (contact_id, binding_id))
                or any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for value in (contact_version, binding_version))):
            raise BusinessToolError('Choose exact registered contact and template IDs with their current versions.')
        binding = _validated('binding', hub.business_binding(binding_id))
        if binding['id'] != binding_id or binding['version'] != binding_version or not bound(binding):
            raise BusinessToolError('The selected template or sender binding changed.')
        try:
            rendered = _render(binding['template'], parameters)
        except Exception:
            raise BusinessToolError('Supply only the exact bounded positional text parameters required by this template.') from None
        contacts = _rows(hub.contacts(), 'contacts', 2000)
        selected = [row for row in contacts if isinstance(row, dict) and row.get('id') == contact_id]
        if len(selected) != 1 or selected[0].get('version') != contact_version:
            raise BusinessToolError('The exact selected contact changed. Find and choose the current contact again.')
        contact = selected[0]
        if (not isinstance(contact.get('phone_e164'), str) or not re.fullmatch(r'\+[1-9][0-9]{1,14}', contact['phone_e164'])
                or not isinstance(contact.get('display_name'), str) or not 1 <= len(contact['display_name']) <= 100
                or not contact['display_name'].isprintable()):
            raise BusinessToolError('The selected contact needs a valid explicit international phone number and name.')
        permissions = [_validated('permission', row) for row in _rows(hub.business_permissions(), 'permissions')]
        eligible = [row for row in permissions if row['connection_id'] == connection.connection_id
            and row['account_version'] == binding['account']['version'] and row['contact_id'] == contact_id
            and row['contact_version'] == contact_version and row['recipient_e164'] == contact['phone_e164']
            and row['status'] == 'granted' and row['expires_at'] > now()
            and binding['template']['category'] in row['allowed_categories']]
        if len(eligible) != 1:
            raise BusinessToolError('A current explicitly recorded recipient permission for this sender and category is required.')
        permission = eligible[0]
        expected = {'binding_id': binding_id, 'binding_version': binding_version, 'contact_id': contact_id,
            'contact_version': contact_version, 'recipient_name': contact['display_name'], 'recipient_e164': contact['phone_e164'],
            'permission_version': permission['version'], 'prepared': {'account': binding['account'], 'template': binding['template'],
                'parameters': deepcopy(parameters), 'rendered_text': rendered}}
        stamp = now()
        for key in list(intents):
            if stamp - intents[key][1] >= 300:
                del intents[key]
        # Stable within this local intent window; there is never a provider retry here.
        request = {'binding_id': binding_id, 'binding_version': binding_version, 'contact_id': contact_id,
            'contact_version': contact_version, 'permission_version': permission['version'], 'parameters': parameters}
        intent = hashlib.sha256(_json(request).encode()).hexdigest()
        if intent not in intents:
            if len(intents) >= 128:
                raise BusinessToolError('Too many local drafts. Review existing proposals before creating more.')
            intents[intent] = (uuid.uuid4().hex, stamp)
        require_current()
        row = _validated('business_message', hub.propose_business_message(binding_id, binding_version, contact_id,
            contact_version, permission['version'], parameters, intents[intent][0]))
        require_current()
        # A same-content binding refresh must not create a second intent or change
        # the old journal snapshot. Only its observational timestamp may differ.
        observed, selected = deepcopy(row['payload']), deepcopy(expected)
        observed['prepared']['account'].pop('verified_at')
        selected['prepared']['account'].pop('verified_at')
        if observed != selected:
            raise BusinessToolError('The saved draft differs from the selected sender, recipient or full rendered content.')
        result = message_summary(row)
        result['next_step'] = ('Say review business message, then send reviewed business message for a fresh exact-content confirmation.'
            if row['status'] in {'awaiting_approval', 'approved'} else
            'This request already has a recorded decision or attempt. Inspect its state; do not resend automatically. Reconcile unknown outcomes first.')
        result['notice'] = 'This operation only saves or retrieves a local record. Provider acceptance, when available, does not prove delivery.'
        return _json(result)

    def list_business_messages() -> str:
        """List owned WhatsApp Business draft/attempt summaries. Accepted is not delivered; unknown requires reconciliation and must not be automatically resent."""
        require_current()
        rows = _rows(hub.business_messages(), 'messages')
        require_current()
        return _bounded('messages', [message_summary(row) for row in rows])

    fields = ('contact_id', 'contact_version', 'binding_id', 'binding_version')
    schema = {'type': 'function', 'function': {'name': 'draft_business_message', 'description': draft_business_message.__doc__,
        'parameters': {'type': 'object', 'additionalProperties': False, 'required': [*fields, 'parameters'],
            'properties': {**{name: {'type': 'string'} for name in fields},
                'parameters': {'type': 'array', 'maxItems': 10,
                    'items': {'type': 'string', 'minLength': 1, 'maxLength': 1024}}}}}}
    return {'list_business_templates': Skill(list_business_templates, False, _schema(list_business_templates), local_only=True),
        'draft_business_message': Skill(draft_business_message, False, schema, local_only=True),
        'list_business_messages': Skill(list_business_messages, False, _schema(list_business_messages), local_only=True)}
