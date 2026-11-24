"""Local mode: the harness runs the agent loop on your Mac.

The model is an Elastic-hosted chat_completion endpoint (``POST _inference/chat_completion/<id>/_stream``,
OpenAI-style streaming tool calls). The tools run here, so they can see your files. Elastic data and
Agent Builder agents are reachable as tools (elastic_esql, ask_agent, elastic_tool).
"""

from __future__ import annotations

import json
import platform
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .client import ApiError
from .config import STATE_DIR
from .tools import Ctx, Tool, build_tools, run_tool

MAX_STEPS = 20

SYSTEM = """You are Elastic Harness in local mode: a terminal assistant on the user's {system} machine.
Working directory: {cwd}

You can read and search local files, change them or run shell commands (the harness asks the user first),
query the connected Elasticsearch cluster with ES|QL, and delegate questions to the user's Elastic Agent Builder
agents. Use the tools instead of guessing.

Rules:
- Explore read-only first. Never read or print credentials; the harness refuses credential files.
- Data that lives in Elastic: use elastic_esql (always with a LIMIT), or ask_agent when a custom agent already
  knows the domain. Say which agent or query produced an answer.
- Be concise. Cite file paths. If the user declines an action, do not retry it.

Elastic Agent Builder agents available:
{agents}"""


class LocalSession:
    def __init__(self, ctx: Ctx, agents: list[dict] | None = None, session_id: str | None = None):
        self.ctx = ctx
        self.tools: dict[str, Tool] = {t.name: t for t in build_tools()}
        self.id = session_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        listing = "\n".join(f"- {a['id']}: {a.get('name', '')} — {(a.get('description') or '')[:90]}"
                            for a in (agents or [])[:15]) or "(none)"
        self.system = SYSTEM.format(system=platform.system(), cwd=ctx.cwd, agents=listing)
        self.messages: list[dict] = []
        self.tokens_in = self.tokens_out = 0

    # -- persistence -------------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return STATE_DIR / "sessions" / f"{self.id}.json"

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"id": self.id, "model": self.ctx.model, "cwd": str(self.ctx.cwd),
                                         "saved": datetime.now(timezone.utc).isoformat(),
                                         "messages": self.messages}, indent=1))

    @classmethod
    def list_saved(cls, limit: int = 15) -> list[dict]:
        d = STATE_DIR / "sessions"
        out = []
        for f in sorted(d.glob("*.json"), reverse=True)[:limit] if d.exists() else []:
            try:
                data = json.loads(f.read_text())
            except ValueError:
                continue
            first = next((m["content"] for m in data.get("messages", []) if m.get("role") == "user"), "")
            out.append({"id": data.get("id", f.stem), "saved": data.get("saved", ""), "first": str(first)[:70]})
        return out

    def load(self, session_id: str) -> int:
        data = json.loads((STATE_DIR / "sessions" / f"{session_id}.json").read_text())
        self.id, self.messages = data["id"], data["messages"]
        return len(self.messages)

    # -- one user turn -----------------------------------------------------------------------
    def turn(self, user_text: str) -> None:
        ui, client = self.ctx.ui, self.ctx.client
        mark = len(self.messages)
        self.messages.append({"role": "user", "content": user_text})
        try:
            for _ in range(MAX_STEPS):
                text, calls, usage = self._complete()
                if usage:
                    self.tokens_in += usage.get("prompt_tokens", 0)
                    self.tokens_out += usage.get("completion_tokens", 0)
                msg: dict = {"role": "assistant", "content": text}
                if calls:
                    msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                          "function": {"name": c["name"], "arguments": c["args"] or "{}"}} for c in calls]
                self.messages.append(msg)
                ui.flush()
                if not calls:
                    break
                for c in calls:
                    self.messages.append({"role": "tool", "tool_call_id": c["id"], "content": self._run(c)})
            else:
                ui.warn(f"stopped after {MAX_STEPS} tool rounds")
        except (KeyboardInterrupt, ApiError):
            del self.messages[mark:]  # never leave a tool call without its result
            ui.flush()
            raise
        self.save()

    def _run(self, call: dict) -> str:
        ui = self.ctx.ui
        tool = self.tools.get(call["name"])
        try:
            args = json.loads(call["args"] or "{}")
        except ValueError:
            return "error: tool arguments were not valid JSON"
        ui.tool_call(call["name"], args)
        if tool is None:
            return f"error: unknown tool {call['name']!r}"
        result = run_tool(tool, args, self.ctx)
        ui.tool_result(result)
        return result

    def _complete(self) -> tuple[str, list[dict], dict | None]:
        ui = self.ctx.ui
        text, slots, usage = "", {}, None
        msgs = [{"role": "system", "content": self.system}] + self.messages
        for chunk in self.ctx.client.chat_stream(self.ctx.model, msgs, [t.schema() for t in self.tools.values()]):
            ui.raw("chunk", chunk)
            if chunk.get("usage"):
                usage = chunk["usage"]
            for ch in chunk.get("choices") or []:
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    text += delta["content"]
                    ui.text(delta["content"])
                for tc in delta.get("tool_calls") or []:
                    slot = slots.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["name"] = fn.get("name") or slot["name"]
                    slot["args"] += fn.get("arguments") or ""
        return text, [slots[i] for i in sorted(slots)], usage
