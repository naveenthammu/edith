import pytest

from brain.hub import HubError
from skills import REGISTRY
from skills import knowledge


def test_search_returns_verifiable_source_ids_and_preserves_missing_evidence(monkeypatch):
    class Client:
        def search(self, query, limit=5):
            assert query == "orchid"
            return {"citations": [{"document_id": "doc1", "chunk_id": "chunk2", "source_version": "abc123",
                                   "title": "Plant care", "excerpt": "Water on Monday", "score": 1}]}

    monkeypatch.setattr(knowledge, "client", Client())
    text = knowledge.search_knowledge("orchid")
    assert "doc1" in text and "chunk2" in text and "abc123" in text and "Water on Monday" in text
    monkeypatch.setattr(Client, "search", lambda *args, **kw: {"citations": []})
    assert "No permitted source" in knowledge.search_knowledge("orchid")


def test_missing_connection_is_not_reported_as_empty_knowledge(monkeypatch):
    monkeypatch.setattr(knowledge, "client", None)
    with pytest.raises(HubError, match="not connected"):
        knowledge.search_knowledge("orchid")


def test_save_defaults_private_and_child_sharing_requires_explicit_household_choice(monkeypatch):
    calls = []

    class Client:
        def add_document(self, **values):
            calls.append(values)
            return {"id": "doc1", "source_version": "abc123"}

    monkeypatch.setattr(knowledge, "client", Client())
    assert "doc1" in knowledge.save_knowledge("Plant care", "Water on Monday")
    assert calls == [{"title": "Plant care", "content": "Water on Monday", "visibility": "private", "child_allowed": False}]
    with pytest.raises(ValueError):
        knowledge.save_knowledge("Plant care", "Water on Monday", child_allowed=True)
    assert len(calls) == 1


def test_knowledge_skills_are_local_and_mutations_need_confirmation():
    for name in ("search_knowledge", "list_knowledge", "save_knowledge", "delete_knowledge"):
        assert REGISTRY[name].local_only
    assert REGISTRY["save_knowledge"].confirm and REGISTRY["delete_knowledge"].confirm
