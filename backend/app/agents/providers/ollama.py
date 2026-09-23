from __future__ import annotations

import json
from typing import AsyncIterator, Optional

import httpx

from app.agents.config import get_agent_settings
from app.agents.providers.base import (
    ChatChunk,
    ChatMessage,
    LLMProvider,
    LLMUnavailableError,
    ToolDefinition,
    Usage,
    chat_timeout_seconds,
)


def _think_value(raw: str) -> bool | str | None:
    """Map AGENTS_OLLAMA_THINK to Ollama's `think` field. None = don't send."""
    v = (raw or "").strip().lower()
    if v in ("", "omit", "default"):
        return None
    if v in ("false", "0", "off", "no"):
        return False
    if v in ("true", "1", "on", "yes"):
        return True
    if v in ("low", "medium", "high"):
        return v
    return None


def _serialize_messages(messages: list[ChatMessage]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        msg: dict = {"role": m.role}
        if m.content is not None:
            msg["content"] = m.content
        if m.tool_calls:
            msg["tool_calls"] = [
                {"function": {"name": tc.name, "arguments": tc.arguments}} for tc in m.tool_calls
            ]
        if m.role == "tool":
            # Ollama expects content + name for tool results
            if m.name:
                msg["name"] = m.name
        out.append(msg)
    return out


def _serialize_tools(tools: Optional[list[ToolDefinition]]) -> Optional[list[dict]]:
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, *, base_url: str = "http://ollama:11434", api_key: str = "", model: Optional[str] = None):
        super().__init__(api_key=api_key, base_url=base_url, model=model)

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        tools: Optional[list[ToolDefinition]] = None,
        temperature: float = 0.4,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[ChatChunk]:
        url = f"{self.base_url.rstrip('/')}/api/chat"
        payload: dict = {
            "model": model,
            "messages": _serialize_messages(messages),
            "stream": True,
            "options": {"temperature": temperature},
        }
        if max_tokens is not None:
            payload["options"]["num_predict"] = max_tokens
        if tools:
            payload["tools"] = _serialize_tools(tools)
        settings = get_agent_settings()
        if settings.ollama_num_ctx > 0:
            payload["options"]["num_ctx"] = int(settings.ollama_num_ctx)
        think = _think_value(settings.ollama_think)
        if think is not None:
            payload["think"] = think
        if settings.ollama_keep_alive.strip():
            payload["keep_alive"] = settings.ollama_keep_alive.strip()

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(chat_timeout_seconds(), connect=10.0)) as client:
                async with client.stream("POST", url, json=payload) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", errors="replace")
                        raise LLMUnavailableError(f"Ollama {resp.status_code}: {body}", status=resp.status_code)
                    tool_idx = 0
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        data = json.loads(line)
                        msg = data.get("message") or {}
                        # Thinking models stream their reasoning in a separate
                        # `thinking` field. It is never user-facing text, so it
                        # is deliberately not surfaced as a text_delta.
                        content = msg.get("content") or ""
                        if content:
                            yield ChatChunk(type="text_delta", text=content)
                        for call in (msg.get("tool_calls") or []):
                            fn = call.get("function") or {}
                            args = fn.get("arguments") or {}
                            if not isinstance(args, str):
                                args = json.dumps(args)
                            tcid = call.get("id") or f"call_{tool_idx}"
                            tool_idx += 1
                            yield ChatChunk(type="tool_call_start", tool_call_id=tcid, tool_name=fn.get("name") or "")
                            yield ChatChunk(type="tool_call_args_delta", tool_call_id=tcid, args_delta=args)
                            yield ChatChunk(type="tool_call_end", tool_call_id=tcid)
                        if data.get("done"):
                            usage = Usage(
                                input_tokens=int(data.get("prompt_eval_count") or 0),
                                output_tokens=int(data.get("eval_count") or 0),
                            )
                            yield ChatChunk(type="usage", usage=usage)
                            yield ChatChunk(type="finish", finish_reason=data.get("done_reason") or "stop")
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"Ollama unreachable: {exc}") from exc

    async def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        url = f"{self.base_url.rstrip('/')}/api/embed"
        out: list[list[float]] = []
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                # /api/embed accepts a list under `input` since Ollama 0.2+.
                payload: dict = {"model": model, "input": texts}
                embed_ctx = int(get_agent_settings().ollama_embed_num_ctx)
                if embed_ctx > 0:
                    payload["options"] = {"num_ctx": embed_ctx}
                resp = await client.post(url, json=payload)
                if resp.status_code >= 400:
                    raise LLMUnavailableError(f"Ollama embed {resp.status_code}: {resp.text}", status=resp.status_code)
                data = resp.json()
                embeddings = data.get("embeddings") or []
                for vec in embeddings:
                    out.append(list(vec))
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"Ollama unreachable: {exc}") from exc
        return out
