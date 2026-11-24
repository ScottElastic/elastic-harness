"""Test doubles: a fake Elastic client and an in-memory keychain."""

import io

from rich.console import Console

from elastic_harness.client import ApiError
from elastic_harness.config import Profile
from elastic_harness.render import Ui


class FakeKeychain:
    def __init__(self):
        self.items = {}

    def get_password(self, service, name):
        return self.items.get((service, name))

    def set_password(self, service, name, key):
        self.items[(service, name)] = key

    def delete_password(self, service, name):
        self.items.pop((service, name), None)


class FakeElastic:
    """Records calls; answers from canned data. Patched in for elastic_harness.repl.Elastic."""

    spaces_data = [{"id": "default", "name": "Default"}, {"id": "demo", "name": "Demo"}]
    agents_by_space = {"default": ["elastic-ai-agent", "eh-demo"], "demo": ["elastic-ai-agent"]}
    models_data = [".anthropic-claude-4.5-haiku-chat_completion", ".anthropic-claude-5.5-sonnet-chat_completion",
                   ".anthropic-claude-5.5-opus-chat_completion"]

    def __init__(self, profile: Profile):
        self.profile = profile.resolved()
        self.kibana_url, self.es_url, self.space = self.profile.kibana_url, self.profile.es_url, self.profile.space or "default"
        self.calls = []
        self.aborted = []
        self.deleted = []
        self.stream_events = None  # override per test
        self.raise_interrupt = False

    def version(self):
        return "9.6.0"

    def whoami(self):
        return {"username": "tester", "authentication_realm": {"type": "_es_api_key"}}

    def spaces(self):
        return self.spaces_data

    def agents(self):
        return [{"id": a, "name": a, "access_control": {"access_mode": "public"}, "description": ""}
                for a in self.agents_by_space.get(self.space, [])]

    def models(self):
        return list(self.models_data)

    def conversations(self):
        return [{"id": "c-old", "title": "old chat", "agent_id": "eh-demo", "updated_at": "2026-11-01T00:00:00Z"}]

    def conversation(self, cid):
        return {"id": cid, "title": "t", "agent_id": "eh-demo", "rounds": [
            {"input": {"message": "hi"}, "response": {"message": "hello there"},
             "steps": [{"type": "tool_call", "tool_id": "eh-search", "params": {"q": "x"}}],
             "model_usage": {"input_tokens": 10, "output_tokens": 5, "connector_id": "m"}}]}

    def delete_conversation(self, cid):
        self.deleted.append(cid)

    def converse(self, **kw):
        self.calls.append(("converse", kw))
        return {"conversation_id": f"conv-{kw.get('model')}", "response": {"message": f"answer from {kw.get('model')}"},
                "model_usage": {"input_tokens": 100, "output_tokens": 20}, "steps": []}

    def converse_stream(self, **kw):
        self.calls.append(("stream", kw))
        events = self.stream_events or [("conversation_id_set", {"conversation_id": "c1"}),
                                        ("message_chunk", {"text_chunk": "hello "}), ("message_chunk", {"text_chunk": "world"}),
                                        ("round_complete", {"round": {"model_usage": {"input_tokens": 7, "output_tokens": 2, "llm_calls": 1, "connector_id": "m"}}})]
        for i, e in enumerate(events):
            if self.raise_interrupt and i == 1:
                raise KeyboardInterrupt
            yield e

    def abort_execution(self, eid, wait=True, timeout=15):
        self.aborted.append(eid)
        return True

    def tools(self):
        return [{"id": "eh-search", "type": "index_search"}, {"id": "platform.core.search", "type": "builtin"},
                {"id": "platform.core.resume_workflow_execution", "type": "builtin"}]

    def agent(self, agent_id):
        if agent_id in getattr(self, "stored_agents", {}):
            return self.stored_agents[agent_id]
        raise ApiError(404, "not found")

    def kb(self, method, path, body=None, timeout=None):
        self.calls.append((method, path, body))
        return {}

    def chat_stream(self, model, messages, tools=None, max_tokens=0):
        self.calls.append(("chat", model))
        yield {"choices": [{"delta": {"content": "SUMMARY: the user asked about kestrel; owner Priya Nair."}}]}

    def esql(self, q):
        return {"columns": [{"name": "a", "type": "keyword"}], "values": [["x"], ["y"], ["z"]]}

    def alert(self, aid):
        return None if aid == "missing" else {"_index": ".alerts-x", "_source": {"kibana.alert.rule.name": "Rule A", "host.name": "h1",
                                                                              "kibana.alert.severity": "high", "kibana.space_ids": ["default"]}}

    def case(self, cid):
        return {"id": cid, "title": "Case T", "status": "open", "severity": "high", "tags": ["a"], "description": "d", "totalAlerts": 1}

    def case_comments(self, cid):
        return [{"type": "user", "comment": "first note"}]

    def case_alerts(self, cid):
        return [{"id": "alert-1"}]


def quiet_ui(auto_yes=True):
    ui = Ui(Console(file=io.StringIO(), width=140, highlight=False), auto_yes=auto_yes)
    return ui


def output(ui) -> str:
    return ui.console.file.getvalue()
