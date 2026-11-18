import base64
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import anthropic
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
    local: bool = False  # written by the offline model


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


# The agent's system prompt carries the clock, which changes every minute. llama.cpp only reuses the
# unchanged start of a prompt, so locally the clock rides on the newest message instead: otherwise every
# request re-read the persona and the whole conversation (43 s for "what is 2 plus 2" in a live run).
_CLOCK = re.compile(r" ?Current date and time: [^\n]*?\.(?=\n|$)")


def to_local_messages(messages: list[dict], tools: list[dict]) -> list[dict]:
    """Rewrite OpenAI tool turns as plain chat turns the local model understands."""
    tool_lines = "\n".join(
        f"- {t['function']['name']}: {t['function']['description']} "
        f"args: {json.dumps(t['function']['parameters']['properties'])}"
        for t in tools
    )
    out, clock = [], ""
    for m in messages:
        role = m["role"]
        if role == "system":
            content = m["content"]
            found = _CLOCK.search(content)
            if found:
                clock, content = found.group(0).strip(), content.replace(found.group(0), "", 1)
            # Fixed tool list first, so the cached prompt prefix is as long as possible.
            tool_text = LOCAL_TOOL_PROMPT.format(tools=tool_lines) + "\n\n" if tools else ""
            out.append({"role": "system", "content": tool_text + content})
        elif role == "tool":
            out.append({"role": "user", "content": f"Tool result: {m['content'][:500]}"})
        elif role == "assistant" and m.get("tool_calls"):
            fn = m["tool_calls"][0]["function"]
            out.append(
                {"role": "assistant", "content": json.dumps({"tool": fn["name"], "args": _loads_args(fn["arguments"])})}
            )
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    if clock and out[-1]["role"] != "system":
        out[-1] = {**out[-1], "content": f"{out[-1]['content']}\n\n({clock})"}
    return out


REMOTE_TIMEOUT = 30.0  # seconds; Claude thinks before tool calls, so allow more than a plain chat reply
RETRY_REMOTE_AFTER = 60.0  # seconds offline before trying Claude again
REMOTE_ERRORS = (anthropic.APIError, openai.APIError)  # incl. offline, rate limits, no credits

LOCAL_REPO = "bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF"
# ~2.3 GB file, ~4.1 GB RAM. Benchmarked once on EDITH's 54-tool prompt (i5-1145G7, CPU only, 8 requests):
# expected tool on 7/8 at ~5 s per model call (the 8th read the time from the prompt, also a correct answer).
# Qwen2.5-7B got 1/8 (it said "Opening Notepad..." without calling the tool); Qwen2.5-3B got 7/8 (it called
# a tool that doesn't exist). A q8 KV cache saved 0.8 GB RAM but nearly doubled latency on CPU.
# End to end offline, with the saved prompt and spoken tool results: 4-13 s per request, 7 s warm-up.
LOCAL_FILE = "Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf"


def to_claude_tool(tool: dict) -> dict:
    fn = tool["function"]
    return {"name": fn["name"], "description": fn["description"], "input_schema": fn["parameters"]}


def to_claude_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Convert the agent's OpenAI-style history to Claude's (system, messages)."""
    system, out = [], []

    def add(role: str, blocks: list[dict]) -> None:
        if out and out[-1]["role"] == role:  # tool results after one reply must share one user turn
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        role = m["role"]
        if role == "system":
            system.append(m["content"])
        elif role == "tool":
            add("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"] or "(empty)"}])
        elif role == "assistant" and m.get("claude_content"):
            add("assistant", list(m["claude_content"]))  # replayed unchanged: thinking blocks must stay intact
        elif role == "assistant":
            blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            for tc in m.get("tool_calls") or []:  # written by the local model while offline
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"],
                               "input": _loads_args(tc["function"]["arguments"])})
            add("assistant", blocks or [{"type": "text", "text": "..."}])
        else:
            add("user", [{"type": "text", "text": m.get("content") or "..."}])
    return "\n\n".join(system), out


class ClaudeRemote:
    """Claude through the official SDK; speaks the agent's OpenAI-style message format at the edges."""

    def __init__(self, client, model: str):
        self.client, self.model = client, model

    def chat(self, messages: list[dict], tools: list[dict]) -> "Reply":
        system, msgs = to_claude_messages(messages)
        extra = {"tools": [to_claude_tool(t) for t in tools]} if tools else {}
        resp = self.client.beta.messages.create(
            model=self.model, max_tokens=4096, system=system, messages=msgs,
            output_config={"effort": "low"},  # spoken one-liners: low effort keeps replies fast
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",  # a declined request is retried on a fallback model
            timeout=REMOTE_TIMEOUT, **extra,
        )
        content = [b.model_dump(exclude_none=True) for b in resp.content]
        text = " ".join(b["text"] for b in content if b["type"] == "text").strip() or None
        if resp.stop_reason == "refusal":
            text = "I can't help with that, sir."
        calls = [{"id": b["id"], "name": b["name"], "arguments": b["input"]} for b in content if b["type"] == "tool_use"]
        message = {"role": "assistant", "content": text, "claude_content": content}
        if calls:
            message["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                     for c in calls]
        return Reply(text, calls, message)

    def describe_image(self, png: bytes, question: str) -> str:
        """Answer a question about a screenshot (Claude vision)."""
        resp = self.client.messages.create(
            model=self.model, max_tokens=1024, output_config={"effort": "low"}, timeout=REMOTE_TIMEOUT,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.standard_b64encode(png).decode()}},
                {"type": "text", "text": question + " Answer in one to three short spoken sentences."},
            ]}],
        )
        return " ".join(b.text for b in resp.content if b.type == "text").strip()

    def ask_images(self, images: list[bytes], prompt: str, max_tokens: int = 1024) -> str:
        """Send PNG images then a prompt; return Claude's raw text (no spoken-answer shaping)."""
        blocks = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                               "data": base64.standard_b64encode(png).decode()}} for png in images]
        resp = self.client.messages.create(
            model=self.model, max_tokens=max_tokens, output_config={"effort": "low"}, timeout=REMOTE_TIMEOUT,
            messages=[{"role": "user", "content": blocks + [{"type": "text", "text": prompt}]}],
        )
        return " ".join(b.text for b in resp.content if b.type == "text").strip()


FREE_TIMEOUT = 30.0  # tool-calling routes via FreeLLMAPI measured ~9s; 8s made every such call fall back offline


class OpenAIRemote:
    """An OpenAI-compatible endpoint such as FreeLLMAPI (free tiers of several providers)."""

    def __init__(self, client, model: str, revive: Callable[[], bool] | None = None):
        self.client, self.model = client, model
        self.revive = revive  # restarts the local proxy if it died; True once it answers again

    def chat(self, messages: list[dict], tools: list[dict]) -> "Reply":
        # Claude's raw content is only for Claude; other providers may reject unknown fields.
        clean = [{k: v for k, v in m.items() if k != "claude_content"} for m in messages]
        extra = {"tools": tools} if tools else {}
        kw = dict(model=self.model, messages=clean, timeout=FREE_TIMEOUT, **extra)
        try:
            resp = self.client.chat.completions.create(**kw)
        except openai.APITimeoutError:
            raise  # proxy is up but slow; retrying would only double the wait
        except openai.APIConnectionError:
            # Proxy gone mid-session: relaunch it rather than drop to the minutes-slow local model.
            if not (self.revive and self.revive()):
                raise
            resp = self.client.chat.completions.create(**kw)
        msg = resp.choices[0].message
        calls = [{"id": tc.id, "name": tc.function.name, "arguments": _loads_args(tc.function.arguments)}
                 for tc in msg.tool_calls or []]
        dumped = msg.model_dump(exclude_none=True)
        message = {"role": "assistant", "content": msg.content}
        if dumped.get("tool_calls"):
            message["tool_calls"] = dumped["tool_calls"]
        return Reply(msg.content, calls, message)


class RemoteChain:
    """Try each online model in order (Claude, then FreeLLMAPI); the caller falls back to local if all fail."""

    def __init__(self, remotes: list):
        self.remotes = remotes

    def chat(self, messages: list[dict], tools: list[dict]) -> "Reply":
        for i, remote in enumerate(self.remotes):
            try:
                return remote.chat(messages, tools)
            except REMOTE_ERRORS as e:
                if i == len(self.remotes) - 1:
                    raise
                log.warning("%s failed, trying the next online model: %s", type(remote).__name__, e)

    def vision(self):
        """The first online model that can see images, or None."""
        return next((r for r in self.remotes if hasattr(r, "describe_image")), None)


class LLMUnavailable(Exception):
    """Neither Claude nor the local model could answer."""


class LLM:
    def __init__(self, remote, local_factory: Callable, on_fallback: Callable | None = None,
                 clock: Callable[[], float] = time.monotonic, local_ready: Callable[[], bool] = lambda: True):
        self.remote = remote  # anything with chat(messages, tools) -> Reply, e.g. ClaudeRemote
        self.local_ready = local_ready  # True once the local model file is on disk
        self.force_offline = False  # set by the user's "switch to offline mode"
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
                reply = self.remote.chat(messages, tools)
                self._announced = False
                return reply
            except REMOTE_ERRORS as e:
                log.warning("Online model failed, using local model: %s", e)
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
            reply = parse_local_reply(self._local.complete(to_local_messages(messages, tools)))
            reply.local = True
            return reply
        except Exception as e:
            log.exception("local model failed")
            self._local_retry_at = self.clock() + RETRY_REMOTE_AFTER
            raise LLMUnavailable(str(e)) from e

    def offline(self) -> bool:
        """True when the next chat() goes to the local model."""
        return self.remote is None or self.force_offline or self.clock() < self._offline_until

    def warm_up(self, messages: list[dict], tools: list[dict]) -> None:
        """Load the local model and its long tool prompt in the background, so the first offline question
        doesn't wait for it. The processed prompt is kept on disk, keyed by the tool list."""
        local_messages = to_local_messages(messages + [{"role": "user", "content": "hello"}], tools)
        key = hashlib.sha1((LOCAL_TOOL_PROMPT + json.dumps(tools, sort_keys=True)).encode()).hexdigest()[:12]

        def run():
            try:
                if self._local is None:
                    self._local = self.local_factory()
                warm = getattr(self._local, "warm", None)
                warm(local_messages, key) if warm else self._local.complete(local_messages)
            except Exception:
                log.exception("local model warm-up failed")

        threading.Thread(target=run, daemon=True, name="local-warmup").start()

    def go_online(self) -> None:
        """Use Claude again on the next request."""
        self.force_offline = False
        self._offline_until = 0.0
        self._announced = False


class LocalModel:
    """The local model (LOCAL_FILE) on CPU through llama-cpp-python."""

    def __init__(self, model_path: Path):
        import psutil
        from llama_cpp import Llama

        self.path = model_path
        # One thread per physical core: hyper-threads slow llama.cpp down rather than speed it up.
        self.llm = Llama(model_path=str(model_path), n_ctx=8192, n_threads=psutil.cpu_count(logical=False) or 4,
                         verbose=False)
        self._lock = threading.Lock()  # llama.cpp isn't thread-safe; the startup warm-up runs on its own thread

    def complete(self, messages: list[dict]) -> str:
        with self._lock:
            out = self.llm.create_chat_completion(messages=messages, max_tokens=256, temperature=0.3)
        return out["choices"][0]["message"]["content"] or ""

    def warm(self, messages: list[dict], key: str) -> None:
        """Process the long fixed prompt once and keep the result next to the model file. Later starts load it
        in seconds instead of ~100 s of full CPU, which also slowed speech recognition right after startup."""
        import llama_cpp
        import numpy as np

        saved = self.path.with_name(f"{self.path.stem}.{key}-{llama_cpp.__version__}.prompt")
        if saved.exists():
            try:
                with self._lock:
                    self._load(saved)
                return
            except Exception:
                log.exception("saved prompt %s unusable; processing the prompt again", saved.name)
        self.complete(messages)
        with self._lock:
            state = self.llm.save_state()
        for old in self.path.parent.glob(f"{self.path.stem}.*.prompt"):  # one per tool list; drop stale ones
            old.unlink(missing_ok=True)
        tmp = saved.with_suffix(".tmp")
        with tmp.open("wb") as f:  # numpy's format, not pickle: loading it can't run code
            np.savez(f, input_ids=state.input_ids, n_tokens=state.n_tokens, seed=state.seed,
                     llama_state=np.frombuffer(state.llama_state, dtype=np.uint8))
        tmp.replace(saved)

    def _load(self, saved: Path) -> None:
        import numpy as np
        from llama_cpp.llama import LlamaState

        with np.load(saved) as d:
            blob = d["llama_state"]
            state = LlamaState(
                input_ids=d["input_ids"], n_tokens=int(d["n_tokens"]), seed=int(d["seed"]),
                llama_state=blob.tobytes(), llama_state_size=blob.size,
                # logits aren't kept without logits_all, and load_state broadcasts this one row over the rest
                scores=np.zeros((1, self.llm.n_vocab()), dtype=np.single),
            )
        self.llm.load_state(state)


def download_local_model(models_dir: Path, local_only: bool = False) -> Path:
    from huggingface_hub import hf_hub_download

    if (models_dir / LOCAL_FILE).exists():  # already downloaded: skip the Hub check and its console warning
        return models_dir / LOCAL_FILE
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


def build_llm(cfg, on_fallback: Callable | None = None, revive: Callable[[], bool] | None = None) -> LLM:
    remotes = []
    if cfg.anthropic_key:
        remotes.append(ClaudeRemote(anthropic.Anthropic(api_key=cfg.anthropic_key, max_retries=0), cfg.claude_model))
    if cfg.base_url and cfg.api_key:
        remotes.append(OpenAIRemote(openai.OpenAI(base_url=cfg.base_url.rstrip("/") + "/v1", api_key=cfg.api_key,
                                                  max_retries=0), cfg.model, revive))
    return LLM(
        RemoteChain(remotes) if remotes else None,
        local_factory=lambda: LocalModel(download_local_model(cfg.models_dir, local_only=True)),
        on_fallback=on_fallback,
        local_ready=lambda: (cfg.models_dir / LOCAL_FILE).exists(),
    )
