import logging
import re
import time

log = logging.getLogger("edith")

GOODBYE_WORDS = {"bye", "goodbye", "stop"}


def is_goodbye(text: str) -> bool:
    words = re.findall(r"[a-z]+", text.lower())
    # Short utterances only, so "stop the music on youtube" still reaches the agent.
    return len(words) <= 3 and bool(GOODBYE_WORDS & set(words))


def converse(agent, listener, speaker) -> None:
    """Talk without the wake word until silence, goodbye, or a microphone error."""
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
            continue
        print(f"You: {text}")
        if is_goodbye(text):
            speaker.say("Goodbye sir.")
            return
        speaker.say(agent.handle(text))


def main() -> None:
    import skills
    from brain.agent import Agent
    from brain.llm import build_llm, prefetch_local_model
    from config import load_config, setup_logging
    from skills import productivity
    from voice.stt import Listener
    from voice.tts import Speaker
    from voice.wake import WakeWord, ensure_vosk_model

    cfg = load_config()
    setup_logging(cfg.data_dir)
    speaker = Speaker()
    speaker.say("Edith is starting up, sir. Please wait.")

    skills.load_all()
    productivity.announce = lambda text: speaker.say(text, wait=False)
    listener = Listener(cfg.models_dir, cfg.mic_threshold)
    wake = WakeWord(ensure_vosk_model(cfg.models_dir), cfg.wake_word)
    prefetch_local_model(cfg.models_dir)  # background download so offline fallback is ready later
    llm = build_llm(cfg, on_fallback=lambda: speaker.say("Switching to offline mode, sir."))

    def confirm(action: str) -> bool:
        speaker.say(f"Are you sure you want to {action}?")
        answer = listener.listen(start_timeout=6.0) or ""
        return "yes" in answer.lower()

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
        speaker.say("Yes sir?")
        converse(agent, listener, speaker)
        agent.reset()


if __name__ == "__main__":
    main()
