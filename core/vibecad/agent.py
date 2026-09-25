"""Run a Claude agent against a Workspace, in-process, with full timing and token metrics.

Used by the GUI (live events) and the benchmark (headless). Uses the Claude Agent SDK, which drives the
local `claude` CLI and its login (a Claude Pro/Max subscription works; no API key needed).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, StreamEvent, TextBlock, ThinkingBlock,
    ToolResultBlock, ToolUseBlock, UserMessage, create_sdk_mcp_server, tool,
)

from .workspace import GUIDE_PATH, IR_PATH, TOOLS, Workspace, call, schema

SERVER = "vibecad"


def system_prompt(mode: str = "guide+ir", extra: str = "") -> str:
    parts = [GUIDE_PATH.read_text()]
    if mode == "guide+ir":
        parts.append("# IR reference (already loaded; no need to call ir_reference)\n\n" + IR_PATH.read_text())
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)


@dataclass
class ToolCall:
    name: str
    args_chars: int
    started: float
    seconds: float = 0.0
    ok: bool = True
    result_chars: int = 0
    image: bool = False


@dataclass
class RunMetrics:
    prompt: str
    model: str
    wall_s: float = 0.0
    api_s: float = 0.0
    tool_s: float = 0.0
    turns: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    text_chars: int = 0
    thinking_chars: int = 0
    first_output_s: float | None = None     # time until the model's first streamed token
    first_tool_s: float | None = None       # time until the first tool call
    thinking_s: float = 0.0                 # time spent streaming thinking
    tool_input_s: float = 0.0               # time spent streaming tool-call arguments (e.g. big op batches)
    text_s: float = 0.0                     # time spent streaming visible text
    stop_reason: str | None = None
    error: str | None = None
    session_id: str | None = None

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for c in self.tool_calls:
            counts[c.name] = counts.get(c.name, 0) + 1
        d = {k: v for k, v in asdict(self).items() if k != "tool_calls"}
        d["tool_counts"] = counts
        d["n_tool_calls"] = len(self.tool_calls)
        d["failed_tool_calls"] = sum(not c.ok for c in self.tool_calls)
        d["ops_rejected"] = sum(1 for c in self.tool_calls if c.name == "apply_ops" and not c.ok)
        return d


class AgentRunner:
    """One conversation. Call `run(prompt)` repeatedly for follow-up turns."""

    def __init__(self, ws: Workspace, *, model: str = "sonnet", prompt_mode: str = "guide+ir",
                 extra_system: str = "", max_turns: int = 80, effort: str | None = None,
                 thinking: dict | None = None, resume: str | None = None,
                 on_event: Callable[[dict], None] | None = None, image_results: bool = True):
        # When launched from inside a Claude Code session, the CLI would inherit that session's id and write
        # into (or resume) the parent conversation. Each agent conversation must be its own session.
        for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_AFTER_LAST_COMPACT"):
            os.environ.pop(var, None)
        self.ws = ws
        self.model = model
        self.on_event = on_event or (lambda e: None)
        self.image_results = image_results
        self.metrics: RunMetrics | None = None
        self._client: ClaudeSDKClient | None = None
        self._pending: dict[str, ToolCall] = {}
        tools = [self._sdk_tool(spec) for spec in TOOLS]
        if prompt_mode == "claude_code":
            # imitate an interactive Claude Code session: its full system prompt and built-in tools,
            # CLAUDE.md from the working directory, and the stdio MCP server in a subprocess
            from .workspace import ROOT
            self.options = ClaudeAgentOptions(
                system_prompt={"type": "preset", "preset": "claude_code"},
                setting_sources=["project"],
                mcp_servers={SERVER: {"type": "stdio", "command": "uv",
                                      "args": ["run", "--quiet", "--project", str(ROOT), "vibecad-mcp"]}},
                permission_mode="bypassPermissions",
                model=model, max_turns=max_turns, cwd=str(ws.root), include_partial_messages=True,
                **({"effort": effort} if effort else {}),
            )
            return
        self.options = ClaudeAgentOptions(
            tools=[],  # no shell or file tools: all work goes through the CAD tools
            mcp_servers={SERVER: create_sdk_mcp_server(name=SERVER, tools=tools)},
            allowed_tools=[f"mcp__{SERVER}__{t['name']}" for t in TOOLS],
            system_prompt=system_prompt(prompt_mode, extra_system),
            setting_sources=[],  # ignore CLAUDE.md / user settings: runs are reproducible
            permission_mode="bypassPermissions",
            model=model,
            max_turns=max_turns,
            cwd=str(ws.root),
            include_partial_messages=True,
            **({"resume": resume} if resume else {}),
            **({"effort": effort} if effort else {}),
            thinking=thinking or {"type": "adaptive", "display": "summarized"},
        )

    # tools run in a worker thread so the event loop (GUI websocket) stays responsive
    def _sdk_tool(self, spec: dict):
        name = spec["name"]

        @tool(name, spec["desc"], schema(spec))
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            t0 = time.perf_counter()
            tc = ToolCall(name, len(json.dumps(args)), time.time())
            if self.metrics:
                self.metrics.tool_calls.append(tc)
            try:
                text, png = await asyncio.to_thread(call, self.ws, name, args)
                if png is not None:
                    tc.image = True
                    content = [{"type": "image", "data": base64.b64encode(png).decode(), "mimeType": "image/png"}]
                    self.on_event({"type": "tool_image", "name": name, "png_b64": content[0]["data"]})
                    if not self.image_results:
                        content = [{"type": "text", "text": "(image omitted)"}]
                else:
                    tc.result_chars = len(text)
                    content = [{"type": "text", "text": text}]
                    if name == "apply_ops":
                        try:
                            tc.ok = bool(json.loads(text).get("applied"))
                        except ValueError:
                            pass
                result = {"content": content}
            except Exception as e:  # report tool errors to the model instead of crashing the run
                tc.ok = False
                result = {"content": [{"type": "text", "text": f"error: {e}"}], "is_error": True}
            tc.seconds = time.perf_counter() - t0
            return result

        return handler

    async def __aenter__(self):
        self._client = ClaudeSDKClient(options=self.options)
        await self._client.connect()
        return self

    async def __aexit__(self, *exc):
        if self._client:
            await self._client.disconnect()
        self._client = None

    async def interrupt(self) -> None:
        if self._client:
            await self._client.interrupt()

    async def set_model(self, model: str) -> None:
        """Switch model mid-conversation; the next turn uses it."""
        self.model = model
        if self._client:
            await self._client.set_model(model)

    async def run(self, prompt: str) -> RunMetrics:
        if self._client is None:
            raise RuntimeError("use `async with AgentRunner(...) as r:`")
        m = self.metrics = RunMetrics(prompt=prompt, model=self.model)
        t0 = self._t0 = time.perf_counter()
        self._phase, self._phase_t, self._phase_chars, self._last_emit = None, t0, 0, 0.0
        self.on_event({"type": "run_start", "prompt": prompt, "t": time.time()})
        await self._client.query(prompt)
        try:
            async for msg in self._client.receive_response():
                self._handle(msg, m)
        except Exception as e:
            m.error = f"{type(e).__name__}: {e}"
            self.on_event({"type": "error", "message": m.error})
        self._close_phase(m)
        m.wall_s = time.perf_counter() - t0
        m.tool_s = sum(c.seconds for c in m.tool_calls)
        self.on_event({"type": "run_done", "metrics": m.summary()})
        return m

    def _close_phase(self, m: RunMetrics) -> None:
        if self._phase:
            dt = time.perf_counter() - self._phase_t
            attr = {"thinking": "thinking_s", "tool_input": "tool_input_s", "text": "text_s"}[self._phase]
            setattr(m, attr, getattr(m, attr) + dt)
        self._phase = None

    def _stream(self, ev: dict, m: RunMetrics) -> None:
        """Token-level events: time each phase and tell the GUI what the model is doing right now."""
        now = time.perf_counter()
        et = ev.get("type")
        if et == "content_block_start":
            kind = ev.get("content_block", {}).get("type")
            phase = {"thinking": "thinking", "redacted_thinking": "thinking", "tool_use": "tool_input", "text": "text"}.get(kind)
            if m.first_output_s is None:
                m.first_output_s = now - self._t0
            if phase == "tool_input" and m.first_tool_s is None:
                m.first_tool_s = now - self._t0
            self._close_phase(m)
            self._phase, self._phase_t, self._phase_chars = phase, now, 0
            name = ev.get("content_block", {}).get("name", "")
            self.on_event({"type": "agent_phase", "phase": phase, "tool": name.split("__")[-1], "chars": 0})
        elif et == "content_block_delta":
            d = ev.get("delta", {})
            self._phase_chars += len(d.get("thinking", "") or d.get("text", "") or d.get("partial_json", ""))
            if now - self._last_emit > 0.5:
                self._last_emit = now
                self.on_event({"type": "agent_phase", "phase": self._phase, "chars": self._phase_chars,
                               "seconds": round(now - self._phase_t, 1)})
        elif et == "content_block_stop":
            self._close_phase(m)
            self.on_event({"type": "agent_phase", "phase": None})

    def _handle(self, msg, m: RunMetrics) -> None:
        now = time.time()
        if isinstance(msg, StreamEvent):
            if msg.parent_tool_use_id is None:
                self._stream(msg.event, m)
            return
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock):
                    m.text_chars += len(b.text)
                    self.on_event({"type": "agent_text", "text": b.text, "t": now})
                elif isinstance(b, ThinkingBlock):
                    m.thinking_chars += len(b.thinking)
                    self.on_event({"type": "agent_thinking", "text": b.thinking, "t": now})
                elif isinstance(b, ToolUseBlock):
                    self.on_event({"type": "tool_call", "id": b.id, "name": b.name.split("__")[-1],
                                   "input": b.input, "t": now})
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for b in msg.content:
                if isinstance(b, ToolResultBlock):
                    text = ""
                    if isinstance(b.content, list):
                        text = "\n".join(c.get("text", "") for c in b.content if isinstance(c, dict))
                    elif isinstance(b.content, str):
                        text = b.content
                    self.on_event({"type": "tool_result", "id": b.tool_use_id, "is_error": bool(b.is_error),
                                   "text": text[:4000], "t": now})
        elif isinstance(msg, ResultMessage):
            u = msg.usage or {}
            m.api_s = (msg.duration_api_ms or 0) / 1000
            m.turns = msg.num_turns or 0
            m.cost_usd = msg.total_cost_usd or 0.0
            m.input_tokens = u.get("input_tokens", 0)
            m.output_tokens = u.get("output_tokens", 0)
            m.cache_read_tokens = u.get("cache_read_input_tokens", 0)
            m.cache_write_tokens = u.get("cache_creation_input_tokens", 0)
            m.stop_reason = getattr(msg, "subtype", None)
            m.session_id = getattr(msg, "session_id", None)
