"""Remote entry-point policy until per-device household pairing is implemented."""
from brain.agent import Agent
from skills import REGISTRY

PUBLIC_REMOTE_TOOLS = frozenset({"get_datetime", "get_weather", "wikipedia"})


def remote_agent(llm, confirm, registry=None):
    available = REGISTRY if registry is None else registry
    public = {name: item for name, item in available.items()
              if name in PUBLIC_REMOTE_TOOLS and not item.local_only}
    return Agent(llm, confirm, registry=public, memory=None, include_taught_tasks=False)
