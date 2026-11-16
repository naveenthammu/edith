import logging
import os
import re
import time

log = logging.getLogger("edith")

GOODBYE_WORDS = {"bye", "goodbye", "stop"}


def is_goodbye(text: str) -> bool:
    words = re.findall(r"[a-z]+", text.lower())
    # Short utterances only, so "stop the music on youtube" still reaches the agent.
    return len(words) <= 3 and bool(GOODBYE_WORDS & set(words))


def is_yes(text: str) -> bool:
    return "yes" in re.findall(r"[a-z]+", text.lower())


def port_open(url: str) -> bool:
    import socket
    from urllib.parse import urlparse

    u = urlparse(url)
    try:
        socket.create_connection((u.hostname, u.port or 80), timeout=1).close()
        return True
    except OSError:
        return False


def ensure_proxy(base_url: str | None, app: str | None, wait: float = 30.0, sleep=time.sleep) -> bool:
    """Start the FreeLLMAPI desktop app if its port is closed; True once it answers."""
    if not base_url or port_open(base_url):
        return bool(base_url)
    if not app or not os.path.exists(app):
        log.warning("FreeLLMAPI not running at %s and FREELLMAPI_APP not set", base_url)
        return False
    import subprocess

    # VS Code terminals export ELECTRON_RUN_AS_NODE=1, which makes the Electron app exit silently.
    env = {k: v for k, v in os.environ.items() if not k.startswith("ELECTRON_")}
    subprocess.Popen([app], env=env)
    for _ in range(int(wait)):
        sleep(1)
        if port_open(base_url):
            return True
    log.warning("FreeLLMAPI did not open %s within %ss", base_url, wait)
    return False


def converse(agent, listener, speaker, on_turn=lambda who, text: None) -> None:
    """Talk without the wake word until silence, goodbye, or a microphone error.

    on_turn(who, text) sees each exchange ("you" / "edith"), e.g. for the overlay history.
    """
    empty = 0
    while True:
        try:
            text = listener.listen(start_timeout=10.0)
        except Exception:
            log.exception("listening failed")
            speaker.say("I can't hear you, sir. Please check the microphone.")
            return
        if text is None:
            return
        if not text:
            speaker.say("I didn't catch that, sir.")
            empty += 1
            if empty >= 3:
                return
            continue
        empty = 0
        print(f"You: {text}")
        log.info("heard: %s", text)  # so misheard words can be checked later
        on_turn("you", text)
        if is_goodbye(text):
            on_turn("edith", "Goodbye sir.")
            speaker.say("Goodbye sir.")
            return
        reply = agent.handle(text)
        on_turn("edith", reply)
        speaker.say(reply)


def main() -> None:
    try:
        from ui.overlay import Overlay

        overlay = Overlay()
    except Exception:
        # No WebView2 or pywebview: EDITH still works by voice alone.
        print("Overlay unavailable, running voice-only.")
        log.exception("overlay unavailable")
        run()
        return
    overlay.run(lambda: run(overlay))
    # GUI closed (e.g. Ctrl+C); the voice thread is blocked on the mic and would keep us alive.
    logging.shutdown()
    os._exit(0)


def run(overlay=None) -> None:
    import skills
    from brain.agent import Agent
    from brain.llm import build_llm, prefetch_local_model
    from brain.memory import Memory
    from config import load_config, setup_logging
    from skills import assistant, memory as memory_skill, naukri, productivity
    from voice.stt import Listener
    from voice.tts import Speaker
    from voice.wake import WakeWord, ensure_vosk_model

    cfg = load_config()
    setup_logging(cfg.data_dir)
    speaker = Speaker()
    speaker.say("Edith is starting up, sir. Please wait.")

    skills.load_all()
    productivity.announce = lambda text: speaker.say(text, wait=False)
    listener = Listener(cfg.models_dir, cfg.mic_threshold, speaker=speaker)
    wake = WakeWord(ensure_vosk_model(cfg.models_dir), cfg.wake_word)
    ensure_proxy(cfg.base_url, os.getenv("FREELLMAPI_APP"))  # load_config already read .env
    prefetch_local_model(cfg.models_dir)  # background download so offline fallback is ready later
    llm = build_llm(cfg, on_fallback=lambda: speaker.say("Switching to offline mode, sir."))
    assistant.llm = llm
    memory = Memory(cfg.data_dir / "memory.txt")
    memory_skill.memory = memory
    naukri.start_daily()  # does nothing until the user has logged in to Naukri once

    def confirm(action: str) -> bool:
        speaker.say(f"Are you sure you want to {action}?")
        answer = listener.listen(start_timeout=6.0) or ""
        return is_yes(answer)

    agent = Agent(llm, confirm, memory=memory)
    if llm.remote is None and llm.local_ready():  # offline-only: get the slow first load out of the way now
        llm.warm_up(agent._messages(), skills.tool_schemas())
    speaker.say("Edith 3 point o is online. Say Edith when you need me.")

    while True:
        try:
            wake.wait()
        except Exception:
            log.exception("wake word failed")
            speaker.say("I can't hear you, sir. Please check the microphone.")
            time.sleep(5)
            continue
        if overlay:
            overlay.show()
        speaker.say("Yes sir?")
        converse(agent, listener, speaker, overlay.add if overlay else lambda who, text: None)
        if llm.remote and not llm.force_offline:  # the offline model is too weak to judge what's worth keeping
            try:
                agent.learn()
            except Exception:
                log.exception("learning failed")
        agent.reset()
        if overlay:
            overlay.hide()


if __name__ == "__main__":
    main()
