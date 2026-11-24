"""/compare: the same prompt on several models, side by side."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .client import ApiError, Elastic

MAX_MODELS = 4


@dataclass
class Result:
    model: str
    seconds: float = 0.0
    answer: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    tools: list = field(default_factory=list)
    error: str = ""


def _run_agent(client: Elastic, model: str, agent_id: str, prompt: str, keep: bool) -> Result:
    r = Result(model)
    t0 = time.time()
    cid = None
    try:
        res = client.converse(agent_id=agent_id, text=prompt, model=model)
        cid = res.get("conversation_id")
        r.answer = (res.get("response") or {}).get("message", "")
        usage = res.get("model_usage") or {}
        r.tokens_in, r.tokens_out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        r.tools = [s.get("tool_id") for s in res.get("steps", []) if s.get("type") == "tool_call"]
    except ApiError as exc:
        r.error = str(exc)
    r.seconds = time.time() - t0
    if cid and not keep:
        try:
            client.delete_conversation(cid)
        except ApiError:
            pass
    return r


def _run_plain(client: Elastic, model: str, prompt: str) -> Result:
    r = Result(model)
    t0 = time.time()
    try:
        for chunk in client.chat_stream(model, [{"role": "user", "content": prompt}]):
            if chunk.get("usage"):
                r.tokens_in, r.tokens_out = chunk["usage"].get("prompt_tokens", 0), chunk["usage"].get("completion_tokens", 0)
            for ch in chunk.get("choices") or []:
                r.answer += (ch.get("delta") or {}).get("content") or ""
    except ApiError as exc:
        r.error = str(exc)
    r.seconds = time.time() - t0
    return r


def run_compare(client: Elastic, models: list[str], prompt: str, *, mode: str, agent_id: str, keep: bool = False) -> list[Result]:
    """agent mode: the agent answers once per model (its tools run for real). local mode: plain model answers, no tools."""
    if not 2 <= len(models) <= MAX_MODELS:
        raise ValueError(f"compare needs 2 to {MAX_MODELS} models")
    with ThreadPoolExecutor(max_workers=len(models)) as pool:
        futures = [pool.submit(_run_agent, client, m, agent_id, prompt, keep) if mode == "agent"
                   else pool.submit(_run_plain, client, m, prompt) for m in models]
        return [f.result() for f in futures]


def render(results: list[Result]):
    t = Table(box=None, padding=(0, 2), header_style="bold")
    for col in ("model", "time", "in tokens", "out tokens", "tools"):
        t.add_column(col)
    fastest = min((r.seconds for r in results if not r.error), default=0)
    for r in results:
        t.add_row(r.model.strip("."), f"{r.seconds:.1f}s" + (" ⚡" if r.seconds == fastest and not r.error else ""),
                  f"{r.tokens_in:,}", f"{r.tokens_out:,}", ", ".join(dict.fromkeys(r.tools)) or "-")
    panels = [Panel(Markdown(r.answer or f"[error] {r.error}"), title=r.model.strip("."), title_align="left",
                    border_style="red" if r.error else "dim") for r in results]
    return Group(t, *panels)
