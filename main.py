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
    from config import load_config, setup_logging
    from skills import assistant, productivity
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
    prefetch_local_model(cfg.models_dir)  # background download so offline fallback is ready later
    llm = build_llm(cfg, on_fallback=lambda: speaker.say("Switching to offline mode, sir."))
    assistant.llm = llm

    def confirm(action: str) -> bool:
        speaker.say(f"Are you sure you want to {action}?")
        answer = listener.listen(start_timeout=6.0) or ""
        return is_yes(answer)

    agent = Agent(llm, confirm)
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
        agent.reset()
        if overlay:
            overlay.hide()


if __name__ == "__main__":
    main()
