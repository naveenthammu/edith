import logging
import os
import re
import time

from brain.approval import is_affirmative

log = logging.getLogger("edith")

GOODBYE_WORDS = {"bye", "goodbye", "stop"}


def is_goodbye(text: str) -> bool:
    words = re.findall(r"[a-z]+", text.lower())
    # Short utterances only, so "stop the music on youtube" still reaches the agent.
    return len(words) <= 3 and bool(GOODBYE_WORDS & set(words))


def is_yes(text: str) -> bool:
    return is_affirmative(text)


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


def interaction_token(mic, speaker, agent=None):
    return (getattr(mic, 'capture_revision', None), getattr(speaker, 'generation', None),
            getattr(agent, 'generation', None))


def interaction_current(token, mic, speaker, agent=None):
    return (not (mic is not None and mic.muted.is_set())
            and token == interaction_token(mic, speaker, agent))


def capture_wake_request(snapshot, listener, speaker, agent):
    """Caller holds the conversation lock; never replace the accepted wake token."""
    mic = listener.mic
    if snapshot is None or not interaction_current(snapshot.token, mic, speaker, agent):
        mic.drop()
        return None
    if speaker.utterances != snapshot.speech_count or speaker.speaking.is_set():
        # A reminder may finish between wake detection and acquiring the lock.
        mic.drop()
        return snapshot.token, None
    first = listener.listen(start_timeout=0.6, keep_buffer=True)
    if not interaction_current(snapshot.token, mic, speaker, agent):
        mic.drop()
        return None
    return snapshot.token, first


def close_microphone(mic):
    """Always attempt handle closure even if stopping its native stream fails."""
    for operation in ('stop', 'close'):
        try:
            getattr(mic.stream, operation)()
        except Exception:
            log.warning('Microphone %s did not complete normally.', operation)


def say_current(speaker, text, mic=None, *, token=None, agent=None, wait=True):
    token = interaction_token(mic, speaker, agent) if token is None else token
    if not interaction_current(token, mic, speaker, agent):
        return
    if token[1] is None:
        speaker.say(text, wait=wait)
    else:
        speaker.say(text, wait=wait, expected_generation=token[1])


def confirm_current(prompt, listener, speaker, mic=None, *, token=None, agent=None, on_prompt=lambda text: None):
    token = interaction_token(mic, speaker, agent) if token is None else token
    if not interaction_current(token, mic, speaker, agent):
        return False
    on_prompt(prompt)
    if not interaction_current(token, mic, speaker, agent):
        return False
    outcome = speaker.say_tracked(prompt, expected_generation=token[1])
    if outcome != 'completed' or not interaction_current(token, mic, speaker, agent):
        return False
    answer = listener.listen(start_timeout=8.0) or ''
    return interaction_current(token, mic, speaker, agent) and is_yes(answer)


def converse(agent, listener, speaker, on_turn=lambda who, text: None, first: str | None = None,
             first_token=None, trusted_commands=None) -> None:
    """Talk without the wake word until silence, goodbye, or a microphone error.

    on_turn(who, text) sees each exchange ("you" / "edith"), e.g. for the overlay history.
    first is a request already heard together with the wake word ("Edith, what's the time").
    """
    empty = 0
    mic = getattr(listener, 'mic', None)
    while True:
        token = first_token if first_token is not None else interaction_token(mic, speaker, agent)
        first_token = None
        if first is not None:
            text, first = first, None
        else:
            try:
                text = listener.listen(start_timeout=10.0)
            except Exception:
                log.exception("listening failed")
                say_current(speaker, "I can't hear you, sir. Please check the microphone.", mic, token=token, agent=agent)
                return
        if not interaction_current(token, mic, speaker, agent):
            return
        if text is None:
            return
        if not text:
            say_current(speaker, "I didn't catch that, sir.", mic, token=token, agent=agent)
            empty += 1
            if empty >= 3:
                return
            continue
        empty = 0
        print(f"You: {text}")
        log.info("heard utterance (%d characters)", len(text))
        on_turn("you", text)
        if not interaction_current(token, mic, speaker, agent):
            return
        if is_goodbye(text):
            on_turn("edith", "Goodbye Naveen.")
            say_current(speaker, "Goodbye Naveen.", mic, token=token, agent=agent)
            return
        rate_choice = {'speak slower': (140, "I'll speak more slowly."),
                       'speak faster': (220, "I'll speak more quickly."),
                       'normal speech speed': (180, 'Normal speech speed restored.')}.get(text.strip().lower().rstrip('.!?'))
        if rate_choice is not None:
            speaker.set_rate(rate_choice[0])
            on_turn('edith', rate_choice[1])
            say_current(speaker, rate_choice[1], mic, token=token, agent=agent)
            continue
        reply = None
        if trusted_commands is not None:
            reply = trusted_commands.handle(text,
                confirm=lambda prompt: confirm_current(prompt, listener, speaker, mic, token=token,
                                                        agent=agent, on_prompt=lambda prompt: on_turn('edith', prompt)),
                still_current=lambda: interaction_current(token, mic, speaker, agent))
        if reply is None:
            if token[2] is None:
                reply = agent.handle(text)
            else:
                reply = agent.handle(text, expected_generation=token[2])
        if not reply or not interaction_current(token, mic, speaker, agent):
            return
        on_turn("edith", reply)
        say_current(speaker, reply, mic, token=token, agent=agent)


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
    import threading
    import skills
    from brain.agent import Agent
    from brain.llm import build_llm, prefetch_local_model
    from brain.memory import Memory
    from config import load_config, setup_logging
    from skills import assistant, memory as memory_skill, naukri, productivity
    from voice.stt import Listener, Mic
    from voice.tts import Speaker
    from voice.wake import WakeWord, ensure_vosk_model

    cfg = load_config()
    setup_logging(cfg.data_dir)
    speaker = Speaker()
    speaker.say("Edith is starting up, AI models are getting ready to serve you, naveen.")

    skills.load_all()
    from brain.hub import HubClient
    from brain.embeddings import OllamaEmbeddings
    from skills import knowledge
    knowledge.client = HubClient.from_env()
    knowledge.embeddings = OllamaEmbeddings.from_env()
    identity = knowledge.client.capabilities() if knowledge.client is not None else None
    def announce(text):
        import queue
        try:
            say_current(speaker, text, mic, wait=False)
        except queue.Full:
            log.warning('Speech notification queue is full; notification was not spoken.')

    productivity.announce = announce
    mic = Mic()
    mic.on_mute = speaker.stop
    startup_token = interaction_token(mic, speaker)
    if overlay:
        overlay.mic = mic  # the card's mic button mutes it
    listener = Listener(cfg.models_dir, cfg.mic_threshold, speaker=speaker, mic=mic)
    wake = WakeWord(ensure_vosk_model(cfg.models_dir), cfg.wake_word)
    revive = lambda: ensure_proxy(cfg.base_url, os.getenv("FREELLMAPI_APP"))  # load_config already read .env
    if knowledge.client is None:
        revive()
    if cfg.local_provider == "llama_cpp":
        prefetch_local_model(cfg.models_dir)  # Ollama mode reuses its installed model without a second GGUF download
    llm = build_llm(cfg, on_fallback=lambda: log.info('Local model fallback selected.'), revive=revive)
    assistant.llm = llm
    memory = Memory(cfg.data_dir / "memory.txt")
    memory_skill.memory = memory
    naukri.announce = productivity.announce
    if knowledge.client is None:
        naukri.start_daily()  # legacy personal mode only; paired household sessions need explicit enrollment

    def confirm(action: str) -> bool:
        token = interaction_token(mic, speaker, agent)
        if not agent.turn_is_current():
            return False
        answer = confirm_current(f"Are you sure you want to {action}?", listener, speaker, mic, token=token,
                                 agent=agent, on_prompt=lambda prompt: overlay.add('edith', prompt) if overlay else None)
        return agent.turn_is_current() and answer

    from brain.access import desktop_agent
    agent = desktop_agent(llm, confirm, hub=knowledge.client, memory=memory)

    def interrupt_turn():
        agent.cancel()
        speaker.stop()

    mic.on_mute = interrupt_turn
    conversation_lock = threading.Lock()
    reminder_dispatcher = None
    trusted_commands = None
    if identity is not None and identity['role'] in {'adult', 'elder'}:
        from brain.trusted_commands import configured_trusted_commands
        from brain.reminders import ReminderDispatcher
        trusted_commands = configured_trusted_commands(knowledge.client, identity)
        if identity.get('capabilities', {}).get('reminders') is True:
            reminder_dispatcher = ReminderDispatcher(knowledge.client, speaker,
                lambda: not mic.muted.is_set() and not speaker.speaking.is_set(), conversation_lock,
                expected_principal=identity['principal_id'])
    from skills import telegram
    from brain.remote import remote_agent
    telegram.start_bot(lambda ask: remote_agent(llm, ask))
    if agent.family_role != 'child' and llm.local_ready():  # Child games do not need an open-ended model.
        llm.warm_up(agent._messages(), [entry.schema for entry in agent.registry.values()])
    say_current(speaker, "Edith 3 point o is online. Say Edith when you need me.", mic, token=startup_token)
    if reminder_dispatcher is not None:
        reminder_dispatcher.start()

    try:
        while True:
            wake_token = interaction_token(mic, speaker, agent)
            try:
                wake_snapshot = wake.wait(mic, speaker=speaker, current_generation=lambda: agent.generation)
            except Exception:
                log.warning('Wake-word capture failed.')
                say_current(speaker, "I can't hear you. Please check the microphone.", mic, token=wake_token, agent=agent)
                time.sleep(5)
                continue
            with conversation_lock:
                try:
                    captured = capture_wake_request(wake_snapshot, listener, speaker, agent)
                except Exception:
                    log.warning('Initial request capture failed.')
                    say_current(speaker, "I can't hear you. Please check the microphone.", mic,
                                token=wake_snapshot.token if wake_snapshot is not None else wake_token, agent=agent)
                    continue
                if captured is None:
                    continue
                turn_token, first = captured
                if not interaction_current(turn_token, mic, speaker, agent):
                    continue
                if overlay:
                    overlay.show()
                if not first:
                    say_current(speaker, "Yes Naveen?", mic, token=turn_token, agent=agent)
                converse(agent, listener, speaker, overlay.add if overlay else lambda who, text: None,
                         first=first or None, first_token=turn_token, trusted_commands=trusted_commands)
            if not llm.offline():
                try:
                    agent.learn()
                except Exception:
                    log.warning('Conversation learning failed.')
            agent.reset()
            if trusted_commands is not None:
                trusted_commands.clear()
            if overlay and not overlay.muted():
                overlay.hide()
    finally:
        if reminder_dispatcher is not None:
            reminder_dispatcher.stop()
        agent.cancel()
        speaker.close()
        close_microphone(mic)


if __name__ == "__main__":
    main()
