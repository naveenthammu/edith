"""Enforce paired client roles before exposing tools to the language model."""
from brain.agent import Agent
from brain.hub import HubError
from skills import REGISTRY

SHARED_TOOLS = frozenset({"get_datetime", "get_weather", "wikipedia", "search_knowledge", "list_knowledge"})
ADULT_TOOLS = SHARED_TOOLS | {"save_knowledge", "delete_knowledge"}


def desktop_agent(llm, confirm, *, hub=None, memory=None, registry=None):
    available = REGISTRY if registry is None else registry
    if hub is None:
        return Agent(llm, confirm, registry=available, memory=memory)
    identity = hub.capabilities()
    role = identity.get("role")
    if role not in {"adult", "elder", "child", "guest"} or not identity.get("principal_id"):
        raise HubError("The hub returned an invalid household identity. Re-pair this client.")
    allowed = ADULT_TOOLS if role in {"adult", "elder"} else SHARED_TOOLS
    if role == 'child':
        allowed = frozenset()  # Curated local games only; no open-ended model/tools.
    permitted = {name: tool for name, tool in available.items() if name in allowed}
    if role in {'adult', 'elder'}:
        from brain.coding import coding_tools, workspace_from_env
        workspace = workspace_from_env(identity)
        if workspace is not None:
            permitted.update(coding_tools(workspace))
        from brain.household_tools import household_tools
        capabilities = identity.get('capabilities', {})
        supported = set()
        if capabilities.get('reminders') is True:
            supported.update({'create_reminder', 'list_reminders', 'cancel_reminder', 'acknowledge_reminder'})
        if capabilities.get('contacts') is True:
            supported.update({'find_contacts', 'add_contact', 'update_contact', 'delete_contact'})
        if capabilities.get('email_proposals') is True:
            supported.update({'draft_email', 'list_email_drafts'})
        if supported:
            permitted.update({name: tool for name, tool in household_tools(hub, identity).items() if name in supported})

    def still_authorized():
        current = hub.capabilities()
        return current.get('principal_id') == identity['principal_id'] and current.get('role') == role
    # Legacy memory and macros have no owners; never infer that they belong to
    # the paired person. Their migration requires an explicit owner decision.
    agent = Agent(llm, confirm, registry=permitted, memory=None, local_session=True,
                  include_taught_tasks=False, context_guard=still_authorized, family_role=role)
    if role in {'adult', 'elder'} and identity.get('capabilities', {}).get('commerce_proposals') is True:
        from brain.commerce_tools import configured_merchant_account, commerce_tools
        account = configured_merchant_account(hub, identity)
        if account is not None:
            permitted.update(commerce_tools(hub, account, identity, can_execute=agent.turn_is_current))
    if role in {'adult', 'elder'} and identity.get('capabilities', {}).get('business_message_proposals') is True:
        from brain.business_tools import configured_business_connection, business_tools
        connection = configured_business_connection(hub, identity)
        if connection is not None:
            permitted.update(business_tools(hub, connection, identity, can_execute=agent.turn_is_current))
    return agent
