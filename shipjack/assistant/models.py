"""Chat models for the agents: a primary, a fallback, and a scripted model for tests.

Gemini goes through Google's own client (``langchain-google-genai``): Gemini 3 models return a
"thought signature" with each tool call that must be sent back on the next turn, and the
OpenAI-compatible endpoint's client drops it (the next turn then fails with a 400). Groq goes through
``ChatOpenAI``. Keys come from the environment (``GEMINI_API_KEY``, ``GROQ_API_KEY``), never from code
or a command line. A provider without a key is skipped.

``Models.ainvoke`` tries each model in turn and reports which one answered and why the others did not,
so a fallback shows up in the timeline instead of happening silently.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from ..llm import PROVIDERS


def chat_model(provider: str, model: str, *, temperature: float = 0.2, timeout: float = 45.0):
    """A chat model for ``provider`` (gemini or groq), or None when its key is not set."""
    url, env = PROVIDERS[provider]
    key = os.environ.get(env, "")
    if not key:
        return None
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model, google_api_key=key, temperature=temperature, timeout=timeout,
                                      max_retries=2)
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=model, base_url=url, api_key=key, temperature=temperature, timeout=timeout,
                      max_retries=2, stream_usage=True)


def text_of(content: Any) -> str:
    """A message's text, whether the provider returned a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content or "")


class Models:
    """The models the agents use, in order of preference."""

    def __init__(self, models: Sequence[Any]):
        self.models = [m for m in models if m is not None]
        if not self.models:
            raise ValueError("no chat model available: set GEMINI_API_KEY or GROQ_API_KEY")

    @classmethod
    def from_env(cls) -> Models:
        primary = (os.environ.get("SHIPJACK_MODEL_PROVIDER", "gemini"), os.environ.get("SHIPJACK_MODEL", "gemini-3.8-flash"))
        fallback = (os.environ.get("SHIPJACK_FALLBACK_PROVIDER", "groq"),
                    os.environ.get("SHIPJACK_FALLBACK_MODEL", "openai/gpt-oss-120b"))
        return cls([chat_model(*primary), chat_model(*fallback)])

    @property
    def names(self) -> list[str]:
        return [getattr(m, "model_name", None) or getattr(m, "model", None) or getattr(m, "name", None) or type(m).__name__
                for m in self.models]

    async def ainvoke(self, messages: list[BaseMessage], tools: Sequence | None = None) -> tuple[AIMessage, dict]:
        """Ask each model in turn until one answers (each client already retries its own transient
        errors). Returns the reply and {"model", "failed": [{"model", "error"}]}. Raises AllModelsFailed
        (with every model's error) when none answers."""
        failed, last = [], None
        for model, name in zip(self.models, self.names, strict=True):
            try:
                reply = await (model.bind_tools(tools) if tools else model).ainvoke(messages)
                return reply, {"model": name, "failed": failed}
            except Exception as exc:  # a provider error, a timeout, a rate limit, a bad request
                failed.append({"model": name, "error": f"{type(exc).__name__}: {_short(exc)}"})
                last = exc
        raise AllModelsFailed(failed) from last


class AllModelsFailed(RuntimeError):
    def __init__(self, failed: list[dict]):
        super().__init__("; ".join(f"{f['model']}: {f['error']}" for f in failed))
        self.failed = failed


def _short(exc: Exception) -> str:
    import re
    text = re.sub(r"(AIza|gsk_|sk-)[A-Za-z0-9_\-]+", "<key>", str(exc))
    return " ".join(text.split())[:160]


class ScriptedChat(BaseChatModel):
    """A chat model that answers from a script, for tests. Each item is an AIMessage or a function of
    the messages it was given (so a test can look at what the agent saw)."""

    script: list[Any] = []
    seen: list[list[BaseMessage]] = []
    name: str = "scripted"

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None,
                  run_manager: CallbackManagerForLLMRun | None = None, **kwargs: Any) -> ChatResult:
        self.seen.append(list(messages))
        if not self.script:
            raise RuntimeError("the scripted model ran out of replies")
        step = self.script.pop(0)
        message = step(messages) if callable(step) else step
        if isinstance(message, Exception):
            raise message
        message = message.model_copy(update={"usage_metadata": message.usage_metadata or
                                             {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}})
        return ChatResult(generations=[ChatGeneration(message=message)])


def say(text: str) -> AIMessage:
    return AIMessage(content=text)


def call(name: str, args: dict, id: str | None = None) -> AIMessage:
    import uuid
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id or f"call_{uuid.uuid4().hex[:8]}",
                                              "type": "tool_call"}])
