"""Smoke test: every module imports and the desktop wiring answers one turn, with no hardware or network."""
import importlib
from pathlib import Path

import pytest

import skills
from brain.access import desktop_agent
from brain.llm import Reply
from config import load_config

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("brain", "connectors", "skills", "voice", "ui", "tools")
MODULES = sorted(
    ".".join(p.relative_to(ROOT).with_suffix("").parts)
    for pkg in PACKAGES for p in (ROOT / pkg).glob("*.py") if p.name != "__init__.py"
) + ["main", "config"]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(name)


def test_all_skills_register_with_valid_schemas():
    skills.load_all()
    schemas = skills.tool_schemas()
    assert len(schemas) >= 10
    names = [s["function"]["name"] for s in schemas]
    assert len(names) == len(set(names)), "duplicate tool names"
    for schema in schemas:
        assert schema["type"] == "function" and schema["function"]["name"]


def test_config_loads_with_defaults(tmp_path, monkeypatch):
    for key in ("EDITH_DATA_DIR", "EDITH_LOCAL_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    cfg = load_config(tmp_path)
    assert cfg.data_dir == tmp_path / "data" and cfg.models_dir.is_dir()
    assert cfg.local_provider in {"llama_cpp", "ollama"}


class EchoLLM:
    def __init__(self):
        self.tools = None

    def chat(self, messages, tools):
        self.tools = tools
        return Reply("Online, sir.", [], {"role": "assistant", "content": "Online, sir."})


def test_desktop_agent_answers_one_turn():
    skills.load_all()
    llm = EchoLLM()
    agent = desktop_agent(llm, confirm=lambda action: False)
    assert agent.handle("are you there?") == "Online, sir."
    assert llm.tools, "agent sent no tools to the model"
