import threading
import pytest

from brain.agent import Agent, TurnCancelled
from brain.llm import Reply
from skills import Skill


def test_cancel_during_model_discards_reply_and_prevents_next_tool():
    entered, release = threading.Event(), threading.Event()
    effects, answers = [], []

    class Model:
        def chat_local(self, messages, tools):
            entered.set()
            assert release.wait(2)
            return Reply(None, [{'id': 'c1', 'name': 'effect', 'arguments': {}}],
                         {'role': 'assistant', 'content': None})

    agent = Agent(Model(), lambda _: True, local_session=True, include_taught_tasks=False,
                  registry={'effect': Skill(lambda: effects.append('ran'), False,
                                             {'function': {'name': 'effect'}})})
    worker = threading.Thread(target=lambda: answers.append(agent.handle('do something')))
    worker.start()
    assert entered.wait(2)
    agent.cancel()
    release.set()
    worker.join(2)
    assert not worker.is_alive() and answers == [''] and effects == []
    assert agent.history == []


def test_cancel_during_confirmation_cannot_execute_approved_action():
    effects = []

    class Model:
        def chat_local(self, messages, tools):
            return Reply(None, [{'id': 'c1', 'name': 'effect', 'arguments': {}}],
                         {'role': 'assistant', 'content': None})

    def confirm(_):
        agent.cancel()
        return True

    agent = Agent(Model(), confirm, local_session=True, include_taught_tasks=False,
                  registry={'effect': Skill(lambda: effects.append('ran'), True,
                                             {'function': {'name': 'effect'}})})
    assert agent.handle('do something') == '' and effects == []


def test_new_turn_after_cancel_uses_new_revision():
    class Model:
        def chat_local(self, messages, tools):
            return Reply('Fresh answer', [], {'role': 'assistant', 'content': 'Fresh answer'})

    agent = Agent(Model(), lambda _: False, registry={}, local_session=True, include_taught_tasks=False)
    agent.cancel()
    assert agent.handle('new request') == 'Fresh answer'


def test_cancel_also_invalidates_turn_already_waiting_for_agent_lock():
    waiting, release = threading.Event(), threading.Event()
    answers = []

    class Gate:
        def __enter__(self):
            waiting.set()
            assert release.wait(2)
        def __exit__(self, *args):
            pass

    class Model:
        def chat_local(self, *args):
            return Reply('stale turn ran', [], {'role': 'assistant', 'content': 'stale turn ran'})

    agent = Agent(Model(), lambda _: False, registry={}, local_session=True, include_taught_tasks=False)
    agent._turn_lock = Gate()
    worker = threading.Thread(target=lambda: answers.append(agent.handle('old request')))
    worker.start()
    try:
        assert waiting.wait(2)
        agent.cancel()
    finally:
        release.set()
        worker.join(2)
    assert answers == ['']


def test_cancel_during_access_validation_prevents_confirmation_prompt():
    confirmations = []

    def guard():
        agent.cancel()
        return True

    agent = Agent(object(), lambda action: confirmations.append(action) or True,
                  registry={'effect': Skill(lambda: 'done', True, {'function': {'name': 'effect'}})},
                  context_guard=guard)
    agent._active_revision = agent.generation
    with pytest.raises(TurnCancelled):
        agent._run({'id': 'c1', 'name': 'effect', 'arguments': {}})
    assert confirmations == []
