"""Model providers behind the assistant. One neutral conversation shape (`Turn`, `ToolCall`, `Reply`) and two
transports: Claude through the official Anthropic SDK, and every OpenAI-compatible endpoint (OpenAI, DeepSeek,
Deploy, a custom gateway) through the OpenAI SDK with a `base_url`. The orchestration in `service.py` never sees a
provider-specific object."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import anthropic
import openai

from app.core.exceptions import ExternalServiceError, ValidationError

Provider = Literal["anthropic", "openai", "deepseek", "deploy", "custom", "none"]

#: the defaults a provider starts with — the administrator may change the model (and, for gateways, the base URL)
PRESETS: dict[str, dict[str, str | None]] = {
    "anthropic": {"model": "claude-opus-5-5", "base_url": None},
    "openai": {"model": "gpt-4.1", "base_url": "https://api.openai.com/v1"},
    "deepseek": {"model": "deepseek-chat", "base_url": "https://api.deepseek.com"},
    "deploy": {"model": "deepseek-ai/deepseek-v4.1-flash-1017", "base_url": "https://api.deploy.com/api/v1"},
    "custom": {"model": None, "base_url": None},
}
REQUEST_TIMEOUT = 90.0


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Turn:
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    is_error: bool = False


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall]
    #: end | tool | length | refusal
    stop: str
    usage: dict[str, int]


@dataclass
class AccountConfig:
    provider: str
    model: str
    api_key: str
    base_url: str | None = None
    effort: str | None = None
    max_tokens: int = 2048


class ChatProvider(Protocol):
    async def complete(self, *, system: str, turns: list[Turn], tools: list[dict[str, Any]]) -> Reply: ...


def _error(message: str, code: str = "ai_provider") -> ExternalServiceError:
    return ExternalServiceError(message, code=code)


# ----------------------------------------------------------------------------- Claude (Anthropic SDK)


class AnthropicProvider:
    def __init__(self, cfg: AccountConfig) -> None:
        self.cfg = cfg
        kwargs: dict[str, Any] = {"api_key": cfg.api_key, "timeout": REQUEST_TIMEOUT, "max_retries": 1}
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        self.client = anthropic.AsyncAnthropic(**kwargs)

    @staticmethod
    def _messages(turns: list[Turn]) -> list[dict[str, Any]]:
        """Neutral turns → Anthropic messages: every tool result of one assistant turn goes into ONE user message."""
        out: list[dict[str, Any]] = []
        for t in turns:
            if t.role == "user":
                out.append({"role": "user", "content": t.content or "…"})
            elif t.role == "assistant":
                blocks: list[dict[str, Any]] = []
                if t.content:
                    blocks.append({"type": "text", "text": t.content})
                for c in t.tool_calls:
                    blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.args})
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
            else:
                block = {"type": "tool_result", "tool_use_id": t.tool_call_id, "content": t.content or "", "is_error": t.is_error}
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out

    async def complete(self, *, system: str, turns: list[Turn], tools: list[dict[str, Any]]) -> Reply:
        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": self.cfg.max_tokens,
            "system": system,
            "messages": self._messages(turns),
        }
        if tools:
            kwargs["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]
        if self.cfg.effort:
            kwargs["output_config"] = {"effort": self.cfg.effort}
        try:
            r = await self.client.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise _error("AI provayder kaliti qabul qilinmadi", "ai_auth") from e
        except anthropic.RateLimitError as e:
            raise _error("AI provayder limiti tugadi — birozdan keyin urinib ko‘ring", "ai_rate_limit") from e
        except anthropic.NotFoundError as e:
            raise _error(f"Model topilmadi: {self.cfg.model}", "ai_model") from e
        except anthropic.BadRequestError as e:
            raise _error(f"AI provayder so‘rovni rad etdi: {e.message}", "ai_bad_request") from e
        except anthropic.APIStatusError as e:
            raise _error(f"AI provayder xatosi ({e.status_code})", "ai_status") from e
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            raise _error("AI provayderga ulanib bo‘lmadi", "ai_connection") from e
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in r.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, args=dict(block.input or {})))
        stop = {"end_turn": "end", "tool_use": "tool", "max_tokens": "length", "refusal": "refusal"}.get(r.stop_reason or "", "end")
        usage = {"input": int(r.usage.input_tokens or 0), "output": int(r.usage.output_tokens or 0)}
        return Reply(text="\n".join(text_parts).strip(), tool_calls=calls, stop=stop, usage=usage)


# ----------------------------------------------------------------------------- OpenAI-compatible (OpenAI SDK)


class OpenAICompatProvider:
    def __init__(self, cfg: AccountConfig) -> None:
        self.cfg = cfg
        self.client = openai.AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url or None, timeout=REQUEST_TIMEOUT, max_retries=1)

    @staticmethod
    def _messages(system: str, turns: list[Turn]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for t in turns:
            if t.role == "user":
                out.append({"role": "user", "content": t.content or "…"})
            elif t.role == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": t.content or None}
                if t.tool_calls:
                    msg["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.args, ensure_ascii=False)}} for c in t.tool_calls]
                out.append(msg)
            else:
                out.append({"role": "tool", "tool_call_id": t.tool_call_id or "", "content": t.content or ""})
        return out

    async def complete(self, *, system: str, turns: list[Turn], tools: list[dict[str, Any]]) -> Reply:
        kwargs: dict[str, Any] = {"model": self.cfg.model, "messages": self._messages(system, turns)}
        # OpenAI's newest models take `max_completion_tokens`; the compatible gateways still speak `max_tokens`
        if self.cfg.provider == "openai":
            kwargs["max_completion_tokens"] = self.cfg.max_tokens
        else:
            kwargs["max_tokens"] = self.cfg.max_tokens
        if tools:
            kwargs["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}} for t in tools]
            kwargs["tool_choice"] = "auto"
        try:
            r = await self.client.chat.completions.create(**kwargs)
        except openai.AuthenticationError as e:
            raise _error("AI provayder kaliti qabul qilinmadi", "ai_auth") from e
        except openai.RateLimitError as e:
            raise _error("AI provayder limiti tugadi — birozdan keyin urinib ko‘ring", "ai_rate_limit") from e
        except openai.NotFoundError as e:
            raise _error(f"Model yoki manzil topilmadi: {self.cfg.model}", "ai_model") from e
        except openai.BadRequestError as e:
            raise _error(f"AI provayder so‘rovni rad etdi: {e.message}", "ai_bad_request") from e
        except openai.APIStatusError as e:
            raise _error(f"AI provayder xatosi ({e.status_code})", "ai_status") from e
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            raise _error("AI provayderga ulanib bo‘lmadi", "ai_connection") from e
        if not r.choices:
            raise _error("AI provayder bo‘sh javob qaytardi", "ai_empty")
        choice = r.choices[0]
        msg = choice.message
        calls: list[ToolCall] = []
        for c in msg.tool_calls or []:
            fn = getattr(c, "function", None)
            if fn is None:
                continue
            try:
                args = json.loads(fn.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_invalid_json": fn.arguments}
            calls.append(ToolCall(id=c.id, name=fn.name, args=args if isinstance(args, dict) else {"value": args}))
        stop = {"stop": "end", "tool_calls": "tool", "length": "length", "content_filter": "refusal"}.get(choice.finish_reason or "stop", "end")
        if calls and stop == "end":
            stop = "tool"
        u = r.usage
        usage = {"input": int(getattr(u, "prompt_tokens", 0) or 0), "output": int(getattr(u, "completion_tokens", 0) or 0)} if u else {"input": 0, "output": 0}
        return Reply(text=(msg.content or "").strip(), tool_calls=calls, stop=stop, usage=usage)


# ----------------------------------------------------------------------------- factory


def build_provider(cfg: AccountConfig) -> ChatProvider:
    if cfg.provider == "anthropic":
        return AnthropicProvider(cfg)
    if cfg.provider in ("openai", "deepseek", "deploy", "custom"):
        if not cfg.base_url and cfg.provider == "custom":
            raise ValidationError("Provayder manzili (base URL) kiritilmagan", code="ai_base_url_required")
        return OpenAICompatProvider(cfg)
    raise ValidationError("Noma’lum AI provayder", code="ai_provider")
