"""Agent mode: stream a turn with an Agent Builder agent via POST /api/agent_builder/converse/async."""

from __future__ import annotations

from .client import Elastic
from .render import Ui, short


def _result_summary(data: dict) -> str:
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return short(data, 200)
    first = results[0]
    kind = first.get("type", "result") if isinstance(first, dict) else "result"
    parts = [short(r.get("data", r) if isinstance(r, dict) else r, 120) for r in results[:2]]
    more = f" (+{len(results) - 2} more)" if len(results) > 2 else ""
    return f"{kind}: " + " | ".join(parts) + more


def _answer_prompts(ui: Ui, prompts: list[dict]) -> dict:
    """Turn prompt_request events (approval gates, questions) into the `prompts` field of the next call."""
    answers: dict = {}
    for p in prompts:
        pr = p.get("prompt", p)
        pid, ptype = pr.get("id"), pr.get("type", "confirmation")
        if not pid:
            continue
        if ptype == "authorization":
            ui.warn(f"the agent needs you to authorize connector {pr.get('connector_name', '')}: do it in Kibana. Declining here.")
            answers[pid] = {"authorized": False}
        elif ptype == "ask_user_question":
            out = []
            for q in pr.get("questions", []):
                ui.line(f"[bold yellow]? {q.get('question', '')}[/bold yellow]")
                opts = q.get("options", [])
                for i, o in enumerate(opts, 1):
                    ui.line(f"  {i}. {o.get('label', '')} [dim]{o.get('description', '')}[/dim]")
                raw = ui.ask("  pick a number, or type an answer (blank to skip)").strip()
                if not raw:
                    out.append({"skipped": True})
                elif raw.replace(",", "").replace(" ", "").isdigit():
                    out.append({"choice": [int(x) - 1 for x in raw.replace(",", " ").split() if x.isdigit()]})
                else:
                    out.append({"custom": raw})
            answers[pid] = {"answers": out}
        else:
            title = pr.get("title") or "The agent is asking for confirmation"
            ans = ui.confirm(title, pr.get("message", ""), allow_always=False)
            answers[pid] = {"allow": ans == "y"}
    return answers


def run_turn(client: Elastic, ui: Ui, *, agent_id: str, text: str, conversation_id: str | None,
             model: str | None, overrides: dict | None = None) -> dict:
    """Run one user turn (plus any approval round-trips). Returns {"conversation_id", "usage", "tools"}."""
    state = {"conversation_id": conversation_id, "usage": None, "tools": []}
    prompts: dict | None = None
    for _ in range(6):  # a turn can pause for approvals more than once
        pending: list[dict] = []
        ui.streamed = False
        for event, d in client.converse_stream(agent_id=agent_id, text=text if prompts is None else "",
                                               conversation_id=state["conversation_id"], model=model,
                                               overrides=overrides, prompts=prompts):
            ui.raw(event, d)
            if event == "conversation_id_set":
                state["conversation_id"] = d.get("conversation_id") or state["conversation_id"]
            elif event == "message_chunk":
                ui.text(d.get("text_chunk", ""))
            elif event == "message_complete":
                if not ui.streamed and d.get("message_content"):
                    ui.text(d["message_content"])
            elif event == "reasoning":
                if not d.get("transient") and d.get("reasoning"):
                    ui.reasoning(d["reasoning"])
            elif event == "tool_call":
                state["tools"].append(d.get("tool_id"))
                ui.tool_call(d.get("tool_id", "?"), d.get("params", {}))
            elif event == "tool_result":
                ui.tool_result(_result_summary(d))
            elif event == "prompt_request":
                pending.append(d)
            elif event == "round_complete":
                rnd = d.get("round", d)
                state["usage"] = rnd.get("model_usage") or state["usage"]
            elif event == "error":
                ui.error(short(d, 300))
        ui.flush()
        if not pending:
            break
        prompts = _answer_prompts(ui, pending)
    return state


def usage_line(usage: dict | None) -> str:
    if not usage:
        return ""
    return (f"{usage.get('input_tokens', 0):,} in / {usage.get('output_tokens', 0):,} out tokens"
            f" · {usage.get('llm_calls', 1)} LLM call(s) · {usage.get('connector_id', '')}")


def history_lines(conversation: dict, last: int = 3) -> list[tuple[str, str]]:
    """(role, text) pairs from the last few rounds of a conversation, for /resume."""
    out = []
    for rnd in conversation.get("rounds", [])[-last:]:
        out.append(("you", (rnd.get("input") or {}).get("message", "")))
        out.append(("agent", (rnd.get("response") or {}).get("message", "")))
    return out
