import base64
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.client import HTTPException
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

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
    call_id = f"local-{uuid.uuid4().hex[:12]}"  # unique: Claude rejects repeated tool ids once back online
    call = {"id": call_id, "name": data["tool"], "arguments": args}
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": data["tool"], "arguments": json.dumps(args)}}
        ],
    }
    return Reply(None, [call], message)


# The agent's system prompt carries the clock, which changes every minute. llama.cpp only reuses the
# unchanged start of a prompt, so locally the clock rides on the newest message instead: otherwise every
# request re-read the persona and the whole conversation (43 s for "what is 2 plus 2" in a live run).
_CLOCK = re.compile(r" ?Current date and time: [^\n]*?\.(?=\n|$)")
LOCAL_RESULT_CHARS = 12_000
LOCAL_EVIDENCE_CHARS = 24_000


def _local_evidence(messages: list[dict]) -> dict[int, str]:
    """Bound retained tool evidence, favouring recent sources over old results.

    This is a character budget for tool evidence, not a tokenizer/context-size
    guarantee. Keep truncation visible so a model can request narrower excerpts.
    """
    indices = [i for i, message in enumerate(messages) if message["role"] == "tool"]
    omitted = "[Earlier tool evidence omitted.]"
    truncated = "\n[Tool evidence truncated; retrieve a narrower excerpt if needed.]"
    # Reserve space for wrappers/omission notices, including older messages whose
    # content no longer fits. No old result can crowd out the latest retrieval.
    remaining = max(0, LOCAL_EVIDENCE_CHARS - len(indices) * (len("Tool result: ") + len(omitted)))
    results = {}
    for index in reversed(indices):
        content = messages[index].get("content") or ""
        allowance = min(LOCAL_RESULT_CHARS, remaining)
        if allowance < len(truncated):
            results[index] = omitted
            continue
        if len(content) > allowance:
            content = content[:allowance - len(truncated)] + truncated
        remaining -= len(content)
        results[index] = content
    return results


def to_local_messages(messages: list[dict], tools: list[dict]) -> list[dict]:
    """Rewrite OpenAI tool turns as plain chat turns the local model understands."""
    tool_lines = "\n".join(
        f"- {t['function']['name']}: {t['function']['description']} "
        f"args: {json.dumps(t['function']['parameters']['properties'])}"
        for t in tools
    )
    out, clock = [], ""
    evidence = _local_evidence(messages)
    for index, m in enumerate(messages):
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
            out.append({"role": "user", "content": f"Tool result: {evidence[index]}"})
        elif role == "assistant" and m.get("tool_calls"):
            calls = [{"tool": call["function"]["name"], "args": _loads_args(call["function"]["arguments"])}
                     for call in m["tool_calls"]]
            out.append({"role": "assistant", "content": json.dumps(calls[0] if len(calls) == 1 else calls)})
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    if clock and out[-1]["role"] != "system":
        out[-1] = {**out[-1], "content": f"{out[-1]['content']}\n\n({clock})"}
    return out


REMOTE_TIMEOUT = 30.0  # seconds; Claude thinks before tool calls, so allow more than a plain chat reply
RETRY_REMOTE_AFTER = 60.0  # seconds offline before trying Claude again
REMOTE_ERRORS = (anthropic.APIError, openai.APIError)  # incl. offline, rate limits, no credits
TRANSIENT_ERRORS = (anthropic.APITimeoutError, anthropic.InternalServerError,  # slow or 5xx: worth one retry
                    openai.APITimeoutError, openai.InternalServerError)

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
        self._load_lock = threading.Lock()  # warm-up, voice and Telegram threads must not load the 4 GB model twice
        self._offline_until = 0.0
        self._announced = False
        self._local_retry_at = 0.0

    def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        if self.remote is not None and not self.force_offline and self.clock() >= self._offline_until:
            try:
                try:
                    reply = self.remote.chat(messages, tools)
                except TRANSIENT_ERRORS as e:
                    # FreeLLMAPI often routes the retry to a faster provider; offline takes minutes on this CPU
                    log.warning("Online model hiccup, retrying once: %s", e)
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
        return self.chat_local(messages, tools)

    def chat_local(self, messages: list[dict], tools: list[dict]) -> Reply:
        """Private-context entry point: never attempts a remote provider or fallback."""
        if self._local is None and self.clock() < self._local_retry_at:
            raise LLMUnavailable("local model unavailable")
        try:
            reply = parse_local_reply(self._local_model().complete(to_local_messages(messages, tools)))
            reply.local = True
            return reply
        except Exception as e:
            log.exception("local model failed")
            self._local_retry_at = self.clock() + RETRY_REMOTE_AFTER
            raise LLMUnavailable(str(e)) from e

    def _local_model(self):
        with self._load_lock:
            if self._local is None:
                self._local = self.local_factory()
            return self._local

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
                local = self._local_model()
                warm = getattr(local, "warm", None)
                warm(local_messages, key) if warm else local.complete(local_messages)
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


class _OllamaNoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OllamaLocal:
    """Use an already installed local GGUF model through a loopback Ollama server.

    No model pulls, remote provider calls or automatic downloads are performed.
    Run the trusted Ollama server with OLLAMA_NO_CLOUD=1 as a second boundary;
    a localhost URL alone does not mean that an Ollama model is local.
    """

    MAX_RESPONSE = 1_048_576
    MAX_REQUEST = 1_048_576

    def __init__(self, url: str, model: str, timeout: float = 90):
        try:
            parsed = urlsplit(url)
            safe = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                    and parsed.port is not None and 0 < parsed.port < 65536
                    and not parsed.username and not parsed.password
                    and parsed.path in ("", "/") and not parsed.query and not parsed.fragment)
        except (ValueError, TypeError, AttributeError):
            safe = False
        if not safe:
            raise ValueError("Ollama URL must be an explicit http://127.0.0.1:port address")
        if (not isinstance(model, str) or len(model) > 200
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9._-]+)?", model)
                or model.lower().endswith(("-cloud", ":cloud"))):
            raise ValueError("Choose an installed local Ollama model, not a cloud model")
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
            raise ValueError("Ollama timeout must be greater than zero and at most 300 seconds")
        self.url = url.rstrip("/")
        self.model = model if ":" in model else model + ":latest"
        self.timeout = timeout
        self._opener = build_opener(ProxyHandler({}), _OllamaNoRedirect())

    def _request(self, path: str, payload=None, *, timeout: float | None = None) -> dict:
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() if payload is not None else None
        except (TypeError, ValueError):
            raise LLMUnavailable("Ollama request is not valid JSON") from None
        if body is not None and len(body) > self.MAX_REQUEST:
            raise LLMUnavailable("The local model request exceeds the size limit; shorten the context")
        request = Request(self.url + path, data=body, method="POST" if body is not None else "GET",
                          headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                raw = response.read(self.MAX_RESPONSE + 1)
        except HTTPError as error:
            error.close()
            raise LLMUnavailable("Ollama rejected the local request; check its model and runtime") from None
        except (URLError, OSError, TimeoutError, HTTPException):
            raise LLMUnavailable("Ollama is unavailable or timed out; start the local runtime and retry") from None
        if len(raw) > self.MAX_RESPONSE:
            raise LLMUnavailable("Ollama response exceeded the size limit")
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("error"):
                raise ValueError("invalid response")
        except (ValueError, UnicodeError):
            raise LLMUnavailable("Ollama returned an invalid response") from None
        return value

    def _require_local_model(self) -> dict:
        value = self._request("/api/tags", timeout=min(self.timeout, 3))
        models = value.get("models")
        if not isinstance(models, list):
            raise LLMUnavailable("Ollama returned an invalid model inventory")
        installed = next((m for m in models if isinstance(m, dict)
                          and self.model in (m.get("name"), m.get("model"))), None)
        if installed is None:
            raise LLMUnavailable("The selected model is not installed in Ollama; no automatic download was attempted")
        if installed.get("remote_host") or installed.get("remote_model"):
            raise LLMUnavailable("Cloud-backed Ollama models are not permitted for local inference")
        details = self._request("/api/show", {"model": self.model}, timeout=min(self.timeout, 5))
        model_details = details.get("details")
        if (details.get("remote_host") or details.get("remote_model")
                or not isinstance(model_details, dict) or model_details.get("format") != "gguf"):
            raise LLMUnavailable("The selected Ollama model is not a verified installed local GGUF model")
        return installed

    def available(self) -> bool:
        try:
            self._require_local_model()
            return True
        except LLMUnavailable:
            return False

    def complete(self, messages: list[dict]) -> str:
        # Recheck metadata before sending private content, including after a model
        # alias changes. This trusts the local server; it is not a server sandbox.
        self._require_local_model()
        value = self._request("/api/chat", {
            "model": self.model, "messages": messages, "stream": False, "keep_alive": "5m",
            "options": {"num_predict": 512, "num_ctx": 8192, "temperature": 0.2},
        })
        message = value.get("message")
        if (value.get("remote_host") or value.get("remote_model") or value.get("done") is not True
                or value.get("model") != self.model or not isinstance(message, dict)
                or message.get("role") != "assistant" or not isinstance(message.get("content"), str)
                or not message["content"].strip()):
            raise LLMUnavailable("Ollama did not return a complete local answer")
        return message["content"].strip()


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
    provider = getattr(cfg, "local_provider", "llama_cpp")
    if provider == "ollama":
        local = OllamaLocal(cfg.ollama_url, cfg.ollama_model)
        factory = lambda: local
        ready = local.available
    elif provider == "llama_cpp":
        factory = lambda: LocalModel(download_local_model(cfg.models_dir, local_only=True))
        ready = lambda: (cfg.models_dir / LOCAL_FILE).exists()
    else:
        raise ValueError("Unsupported local model provider")
    return LLM(
        RemoteChain(remotes) if remotes else None,
        local_factory=factory,
        on_fallback=on_fallback,
        local_ready=ready,
    )
