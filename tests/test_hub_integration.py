"""Opt-in real Rust/Python contract tests. Only loopback and synthetic temporary data."""
import json
import os
from pathlib import Path
import socket
import subprocess
import time

import pytest

from brain.hub import HubClient, HubError



def test_python_reaches_real_rust_scoped_retrieval_restart_and_delete(running_hub):
    clients, restart = running_hub
    owner, other, child = (clients[name] for name in ("owner", "other", "child"))
    document = owner.add_document("Orchid care", "orchidmarker: Water on Monday.")
    owner.add_document("Family plants", "familymarker: Check soil on Sunday.", "household", True)
    assert owner.search("orchidmarker")["citations"][0]["document_id"] == document["id"]
    assert other.search("orchidmarker")["citations"] == []
    assert child.search("orchidmarker")["citations"] == []
    assert "Sunday" in child.search("familymarker")["citations"][0]["excerpt"]
    restart()
    assert owner.search("orchidmarker")["citations"]
    assert owner.delete_document(document["id"])["deleted"]
    restart()
    assert owner.search("orchidmarker")["citations"] == []


def test_real_journal_approval_claim_and_restart_never_invent_completion(running_hub):
    clients, restart = running_hub
    owner = clients["owner"]
    action = owner.propose_action("home.power", {"entity_id": "light.study", "on": True}, "synthetic-action-1")
    assert action["status"] == "awaiting_approval"
    assert owner.propose_action("home.power", {"entity_id": "light.study", "on": True}, "synthetic-action-1")["id"] == action["id"]
    with pytest.raises(HubError):
        clients["other"].decide_action(action["id"], "approve", action["argument_hash"])
    approved = owner.decide_action(action["id"], "approve", action["argument_hash"])
    assert approved["status"] == "approved"
    assert owner.claim_action(action["id"])["status"] == "executing"
    with pytest.raises(HubError):
        owner.claim_action(action["id"])
    restart()
    recovered = next(item for item in owner.actions()["actions"] if item["id"] == action["id"])
    assert recovered["status"] == "unknown" and recovered["evidence"] is None


def test_real_rust_journal_and_python_coordinator_bind_exact_effect(running_hub):
    from connectors.home_coordinator import HomeCoordinator

    clients, _ = running_hub
    effects = []

    class SyntheticHome:
        def set_power(self, entity_id, on):
            effects.append((entity_id, on))
            return {"entity_id": entity_id, "requested_state": "on", "observed_state": "on",
                    "status": "verified", "command_status": "accepted", "detail": "synthetic test adapter"}

    coordinator = HomeCoordinator(clients["owner"], SyntheticHome())
    action = coordinator.prepare("light.study", True, "synthetic-coordinator-1")
    result = coordinator.decide_and_execute(action["id"], action["argument_hash"], True)
    assert result["status"] == "verified"
    assert effects == [("light.study", True)]
    assert clients["owner"].actions()["actions"][0]["evidence"]["entity_id"] == "light.study"


@pytest.mark.skipif(os.getenv("EDITH_TEST_LOCAL_MODEL") != "1", reason="Opt-in installed local-model acceptance test")
def test_real_rust_retrieval_to_existing_agent_and_installed_local_model(running_hub, monkeypatch):
    from brain.agent import Agent
    from brain.llm import LLM, OllamaLocal
    from skills import REGISTRY, knowledge

    clients, _ = running_hub
    document = clients["owner"].add_document("Azalea schedule", "The azalea must be watered on Thursday at 7 AM. This is a synthetic acceptance-test note.")
    monkeypatch.setattr(knowledge, "client", clients["owner"])
    semantic_calls = []
    if os.getenv('EDITH_TEST_LOCAL_EMBEDDING') == '1':
        from brain.embeddings import OllamaEmbeddings, index_document
        embeddings = OllamaEmbeddings()
        index_document(clients['owner'], embeddings, document['id'])

        class ObservedEmbeddings:
            def embed(self, texts, *, purpose):
                semantic_calls.append(purpose)
                return embeddings.embed(texts, purpose=purpose)

        monkeypatch.setattr(knowledge, 'embeddings', ObservedEmbeddings())
    else:
        monkeypatch.setattr(knowledge, 'embeddings', None)
    runtime = OllamaLocal("http://127.0.0.1:11434", "qwen3:4b-instruct")
    model = LLM(None, local_factory=lambda: runtime)
    agent = Agent(model, lambda _: False, registry={"search_knowledge": REGISTRY["search_knowledge"]},
                  local_session=True, include_taught_tasks=False)
    started = time.monotonic()
    answer = agent.handle("Use search_knowledge to find when the azalea should be watered. Answer only from the note and cite its title.")
    elapsed = time.monotonic() - started
    tool_results = [item["content"] for item in agent.history if item["role"] == "tool"]
    assert any(document["id"] in result for result in tool_results), answer
    assert "thursday" in answer.lower() and "7" in answer, answer
    assert "azalea schedule" in answer.lower() or document["id"] in answer, answer
    if os.getenv('EDITH_TEST_LOCAL_EMBEDDING') == '1':
        assert semantic_calls and all(purpose == 'query' for purpose in semantic_calls)
        assert any('keywords only' not in result and document['id'] in result for result in tool_results)
    print(json.dumps({"acceptance": "rust-python-local-rag", "model": "qwen3:4b-instruct",
                      "elapsed_seconds": round(elapsed, 2), "answer": answer,
                      "source_retrieved": True, "semantic_query_observed": bool(semantic_calls),
                      "cloud_provider_configured": False}))


@pytest.mark.skipif(os.getenv('EDITH_TEST_LOCAL_EMBEDDING') != '1', reason='Opt-in installed local embedding acceptance test')
def test_real_local_semantic_synonym_retrieval_and_revision_invalidation(running_hub):
    from brain.embeddings import OllamaEmbeddings, index_document, search_documents

    clients, restart = running_hub
    owner, other, child = clients['owner'], clients['other'], clients['child']
    document = owner.add_document('Transport guide', 'The automobile is parked inside the garage.')
    # A different principal's exact-match source must not alter our results.
    other.add_document('car car car', 'car car car exact private match')
    assert owner.search('car')['citations'] == []
    model = OllamaEmbeddings()
    started = time.monotonic()
    receipt = index_document(owner, model, document['id'])
    result = search_documents(owner, 'car', model)
    elapsed = time.monotonic() - started
    assert receipt['indexed'] is True and result['retrieval_mode'] == 'hybrid'
    assert result['citations'][0]['document_id'] == document['id']
    assert search_documents(child, 'car', model)['citations'] == []
    with pytest.raises(HubError):
        other.document_chunks(document['id'])
    restart()
    assert search_documents(owner, 'car', model)['citations'][0]['document_id'] == document['id']
    references = [{'document_id': document['id'], 'source_version': document['source_version']}]
    assert owner.validate_sources(references)['sources'][0]['valid'] is True
    updated = owner.update_document(document['id'], document['source_version'], 'Revised transport guide',
                                    'The automobile is parked inside the garage.')
    assert updated['source_version'] != document['source_version']
    assert owner.validate_sources(references)['sources'][0]['valid'] is False
    # Metadata-only updates invalidate both the citation and stored vectors.
    after = search_documents(owner, 'car', model)
    assert after['citations'] == [] and after['semantic_available'] is False
    with pytest.raises(HubError):
        owner.update_document(document['id'], document['source_version'], 'Stale', 'Overwrite')
    print(json.dumps({'acceptance': 'local-semantic-synonym', 'model_id': receipt['model_id'],
                      'elapsed_seconds': round(elapsed, 2), 'synonym_found': True,
                      'private_scope_preserved': True, 'revision_invalidated': True}))


def test_real_reminders_dispatch_persist_and_are_member_scoped(running_hub):
    import threading
    from brain.reminders import ReminderDispatcher

    clients, restart = running_hub
    owner, other, child = clients['owner'], clients['other'], clients['child']
    reminder = owner.create_reminder('Synthetic: inspect the kitchen window.', int(time.time()), 'reminder-fixture-1')
    assert other.reminders()['reminders'] == []
    with pytest.raises(HubError):
        child.claim_due_reminder()
    with pytest.raises(HubError):
        other.cancel_reminder(reminder['id'])
    spoken = []

    class SilentSpeaker:
        generation = 0
        def say_tracked(self, text, *, expected_generation):
            assert expected_generation == self.generation
            spoken.append(text)
            return 'completed'

    worker = ReminderDispatcher(owner, SilentSpeaker(), lambda: True, threading.Lock(), expected_principal='owner')
    assert worker.poll_once() == 'announced'
    assert len(spoken) == 1
    restart()
    assert owner.reminders()['reminders'][0]['status'] == 'announced'
    assert worker.poll_once() == 'idle' and len(spoken) == 1
    assert owner.acknowledge_reminder(reminder['id'])['status'] == 'acknowledged'
    due = owner.create_reminder('Synthetic cancellation.', int(time.time()), 'reminder-fixture-2')
    claim = owner.claim_due_reminder()['reminder']
    assert claim['id'] == due['id']
    owner.cancel_reminder(due['id'])
    with pytest.raises(HubError):
        owner.complete_reminder_delivery(due['id'], claim['delivery_id'], 'announced')


def test_real_contact_and_trusted_email_review_bind_one_synthetic_send(running_hub):
    from brain.message_review import MessageReview
    from connectors.message_coordinator import MessageCoordinator

    clients, restart = running_hub
    owner, other = clients['owner'], clients['other']
    contact = owner.add_contact('Synthetic Ravi', 'synthetic@example.com')
    assert other.contacts()['contacts'] == []
    with pytest.raises(HubError):
        clients['child'].add_contact('Denied', 'denied@example.com')
    sent = []

    class SyntheticGmail:
        def send(self, recipient, subject, body, *, can_execute=lambda: True):
            assert can_execute()
            sent.append((recipient, subject, body))
            return {'status': 'accepted', 'provider': 'gmail', 'recipient': recipient,
                    'provider_message_id': 'synthetic-message-1', 'delivery_status': 'unverified'}

    coordinator = MessageCoordinator(owner, SyntheticGmail())
    draft = coordinator.prepare(contact['id'], contact['version'], 'Fixture', 'Synthetic message only.', 'email-fixture-1')
    review = MessageReview(owner, expected_principal='owner', coordinator=coordinator)
    shown = review.handle('review email', confirm=lambda _: False, still_current=lambda: True)
    assert 'synthetic@example.com' in shown and sent == []
    answer = review.handle('send reviewed email', confirm=lambda prompt: 'Synthetic message only.' in prompt,
                           still_current=lambda: True)
    assert 'accepted' in answer.lower() and len(sent) == 1
    assert owner.message(draft['id'])['evidence']['delivery_status'] == 'unverified'
    review.handle('send reviewed email', confirm=lambda _: True, still_current=lambda: True)
    assert len(sent) == 1
    with pytest.raises(HubError):
        other.message(draft['id'])
    pending = coordinator.prepare(contact['id'], contact['version'], 'Old contact', 'Do not send.', 'email-fixture-2')
    owner.update_contact(contact['id'], contact['version'], 'Synthetic Ravi', 'new@example.com')
    with pytest.raises(HubError):
        owner.decide_message(pending['id'], 'approve', pending['argument_hash'])
    restart()
    assert owner.message(draft['id'])['status'] == 'accepted' and len(sent) == 1


@pytest.mark.skipif(os.getenv('EDITH_TEST_LOCAL_MODEL') != '1', reason='Opt-in installed local-model household test')
def test_local_model_creates_exact_confirmed_reminder_in_real_rust(running_hub, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from brain.access import desktop_agent
    from brain.llm import LLM, OllamaLocal

    clients, restart = running_hub
    owner = clients['owner']
    monkeypatch.delenv('EDITH_WORKSPACE_CONFIG', raising=False)
    runtime = OllamaLocal('http://127.0.0.1:11434', 'qwen3:4b-instruct')
    model = LLM(None, local_factory=lambda: runtime)
    confirmations = []
    agent = desktop_agent(model, lambda prompt: confirmations.append(prompt) or True,
                          hub=owner, registry={})
    due = (datetime.now(timezone(timedelta(hours=5, minutes=30))) + timedelta(hours=1)).replace(microsecond=0)
    text = 'Synthetic test: inspect the blue notebook.'
    started = time.monotonic()
    answer = agent.handle(f'Create one reminder with the exact text "{text}" at {due.isoformat()}. '
                          'Use create_reminder with that exact time and text, then report the saved result.')
    rows = owner.reminders()['reminders']
    assert confirmations and text in confirmations[0], answer
    assert len(rows) == 1, answer
    assert rows[0]['text'] == text and rows[0]['due_at'] == int(due.timestamp()), answer
    assert rows[0]['status'] == 'scheduled', answer
    assert any(item['role'] == 'tool' and rows[0]['id'] in item['content'] for item in agent.history), answer
    restart()
    assert owner.reminders()['reminders'][0]['id'] == rows[0]['id']
    print(json.dumps({'acceptance': 'local-model-confirmed-reminder', 'model': 'qwen3:4b-instruct',
                      'elapsed_seconds': round(time.monotonic() - started, 2),
                      'exact_text_and_utc_time': True, 'confirmation_observed': True,
                      'restart_persisted': True, 'synthetic_input_only': True,
                      'cloud_provider_configured': False}))


def test_real_phone_contacts_preserve_scope_and_revoke_old_email_approval(running_hub):
    from brain.household_tools import household_tools

    clients, restart = running_hub
    owner = clients['owner']
    tools = household_tools(owner, owner.capabilities())
    contact = json.loads(tools['add_contact'].func('Synthetic phone contact', phone_e164='+919876543210'))
    assert contact['email'] is None and contact['phone_e164'] == '+919876543210'
    assert clients['other'].contacts()['contacts'] == []
    with pytest.raises(HubError):
        owner.propose_message(contact['id'], contact['version'], 'Fixture', 'No email destination.', 'phone-only-fixture')
    restart()
    found = json.loads(tools['find_contacts'].func('+919876543210'))['contacts'][0]
    assert found['id'] == contact['id'] and found['version'] == contact['version']
    updated = owner.update_contact(contact['id'], contact['version'], contact['display_name'], 'synthetic@example.com',
                                   phone_e164=contact['phone_e164'])
    draft = owner.propose_message(updated['id'], updated['version'], 'Fixture', 'Synthetic only.', 'phone-revision-fixture')
    owner.update_contact(updated['id'], updated['version'], updated['display_name'], updated['email'],
                         phone_e164='+919876543211')
    with pytest.raises(HubError):
        owner.decide_message(draft['id'], 'approve', draft['argument_hash'])
    assert owner.message(draft['id'])['status'] == 'awaiting_approval'
