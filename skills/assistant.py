from skills import skill

# main.py sets this to the running brain.llm.LLM.
llm = None


@skill()
def get_brain_mode() -> str:
    """Tell whether EDITH is answering online (cloud model) or offline (local model on this PC)."""
    if llm is None:
        return "error: brain not available"
    if llm.remote is None or llm.force_offline:
        return "Offline mode: using the local model on this PC."
    if llm.clock() < llm._offline_until:
        return "Temporarily offline: the online model failed, retrying it within a minute."
    return "Online mode."


@skill()
def set_brain_mode(mode: str) -> str:
    """Switch EDITH's brain. mode 'offline' uses only the local model on this PC; 'online' goes back to Claude."""
    mode = mode.strip().lower()
    if llm is None:
        return "error: brain not available"
    if mode == "offline":
        if not llm.local_ready():
            return "The offline model is still downloading, sir. Staying online."
        llm.force_offline = True
        return "Switched to offline mode."
    if mode == "online":
        llm.go_online()
        return "Switched to online mode."
    return "error: mode must be online or offline"
