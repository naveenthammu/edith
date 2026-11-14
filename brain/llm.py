import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import openai

log = logging.getLogger(__name__)

LOCAL_TOOL_PROMPT = """You can use these tools:
{tools}
To use a tool, reply with only a JSON object like {{"tool": "tool_name", "args": {{"name": "value"}}}} and nothing else.
After you get the tool result, answer the user in plain words."""


@dataclass
class Reply:
    text: str | None
    tool_calls: list[dict] = field(default_factory=list)  # [{"id", "name", "arguments": dict}]
    message: dict = field(default_factory=dict)  # assistant message to append to history as-is


def _loads_args(raw: str | None) -> dict:
    try:
        args = json.loads(raw or "{}")
    except ValueError:
        return {}
    return args if isinstance(args, dict) else {}


def parse_local_reply(text: str) -> Reply:
    plain = Reply(text, [], {"role": "assistant", "content": text})
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        return plain
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return plain
    if not isinstance(data, dict) or not isinstance(data.get("tool"), str):
        return plain
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    call = {"id": "local-1", "name": data["tool"], "arguments": args}
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "local-1", "type": "function", "function": {"name": data["tool"], "arguments": json.dumps(args)}}
        ],
    }
    return Reply(None, [call], message)


def to_local_messages(messages: list[dict], tools: list[dict]) -> list[dict]:
    """Rewrite OpenAI tool turns as plain chat turns the local model understands."""
    tool_lines = "\n".join(
        f"- {t['function']['name']}: {t['function']['description']} "
        f"args: {json.dumps(t['function']['parameters']['properties'])}"
        for t in tools
    )
    out = []
    for m in messages:
        role = m["role"]
        if role == "system":
            # Fixed tool list first: llama-cpp reuses a cached prompt prefix, and the persona text
            # changes every minute (it holds the time), so putting it first forced a ~25 s re-read.
            tool_text = LOCAL_TOOL_PROMPT.format(tools=tool_lines) + "\n\n" if tools else ""
            out.append({"role": "system", "content": tool_text + m["content"]})
        elif role == "tool":
            out.append({"role": "user", "content": f"Tool result: {m['content'][:500]}"})
        elif role == "assistant" and m.get("tool_calls"):
            fn = m["tool_calls"][0]["function"]
            out.append(
                {"role": "assistant", "content": json.dumps({"tool": fn["name"], "args": _loads_args(fn["arguments"])})}
            )
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return out


REMOTE_TIMEOUT = 8.0  # seconds
RETRY_REMOTE_AFTER = 60.0  # seconds offline before trying FreeLLMAPI again
REMOTE_ERRORS = (openai.APIError,)

LOCAL_REPO = "Qwen/Qwen2.5-3B-Instruct-GGUF"
LOCAL_FILE = "qwen2.5-3b-instruct-q4_k_m.gguf"


class LLMUnavailable(Exception):
    """Neither FreeLLMAPI nor the local model could answer."""


class LLM:
    def __init__(self, remote, model: str, local_factory: Callable, on_fallback: Callable | None = None,
                 clock: Callable[[], float] = time.monotonic, local_ready: Callable[[], bool] = lambda: True):
        self.remote = remote
        self.local_ready = local_ready  # True once the local model file is on disk
        self.force_offline = False  # set by the user's "switch to offline mode"
        self.model = model
        self.local_factory = local_factory
        self.on_fallback = on_fallback
        self.clock = clock
        self._local = None
        self._offline_until = 0.0
        self._announced = False
        self._local_retry_at = 0.0

    def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        if self.remote is not None and not self.force_offline and self.clock() >= self._offline_until:
            try:
                reply = self._remote_chat(messages, tools)
                self._announced = False
                return reply
            except REMOTE_ERRORS as e:
                log.warning("FreeLLMAPI failed, using local model: %s", e)
                self._offline_until = self.clock() + RETRY_REMOTE_AFTER
                if self.on_fallback and not self._announced:
                    self._announced = True
                    try:
                        self.on_fallback()
                    except Exception:
                        log.exception("fallback announcement failed")
        if self._local is None and self.clock() < self._local_retry_at:
            raise LLMUnavailable("local model unavailable")
        try:
            if self._local is None:
                self._local = self.local_factory()
            return parse_local_reply(self._local.complete(to_local_messages(messages, tools)))
        except Exception as e:
            log.exception("local model failed")
            self._local_retry_at = self.clock() + RETRY_REMOTE_AFTER
            raise LLMUnavailable(str(e)) from e

    def go_online(self) -> None:
        """Use FreeLLMAPI again on the next request."""
        self.force_offline = False
        self._offline_until = 0.0
        self._announced = False

    def _remote_chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        extra = {"tools": tools} if tools else {}
        resp = self.remote.chat.completions.create(
            model=self.model, messages=messages, timeout=REMOTE_TIMEOUT, **extra
        )
        msg = resp.choices[0].message
        calls = [
            {"id": tc.id, "name": tc.function.name, "arguments": _loads_args(tc.function.arguments)}
            for tc in msg.tool_calls or []
        ]
        dumped = msg.model_dump(exclude_none=True)
        message = {"role": "assistant", "content": msg.content}
        if dumped.get("tool_calls"):
            message["tool_calls"] = dumped["tool_calls"]
        return Reply(msg.content, calls, message)


class LocalModel:
    """Qwen2.5-3B on CPU through llama-cpp-python."""

    def __init__(self, model_path: Path):
        from llama_cpp import Llama

        self.llm = Llama(model_path=str(model_path), n_ctx=8192, n_threads=4, verbose=False)

    def complete(self, messages: list[dict]) -> str:
        out = self.llm.create_chat_completion(messages=messages, max_tokens=256, temperature=0.3)
        return out["choices"][0]["message"]["content"] or ""


def download_local_model(models_dir: Path, local_only: bool = False) -> Path:
    from huggingface_hub import hf_hub_download

    return Path(
        hf_hub_download(repo_id=LOCAL_REPO, filename=LOCAL_FILE, local_dir=models_dir, local_files_only=local_only)
    )


def prefetch_local_model(models_dir: Path) -> None:
    """Download the local model in the background at startup so a later fallback never waits on the network."""

    def run():
        try:
            download_local_model(models_dir)
        except Exception:
            log.exception("local model download failed")

    threading.Thread(target=run, daemon=True, name="model-download").start()


def build_llm(cfg, on_fallback: Callable | None = None) -> LLM:
    remote = None
    if cfg.base_url and cfg.api_key:
        remote = openai.OpenAI(base_url=cfg.base_url.rstrip("/") + "/v1", api_key=cfg.api_key, max_retries=0)
    return LLM(
        remote,
        cfg.model,
        local_factory=lambda: LocalModel(download_local_model(cfg.models_dir, local_only=True)),
        on_fallback=on_fallback,
        local_ready=lambda: (cfg.models_dir / LOCAL_FILE).exists(),
    )
