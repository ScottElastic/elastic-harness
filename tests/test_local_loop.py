"""The local agent loop against a fake model: tool call -> tool result -> final answer."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from elastic_harness.local_agent import LocalSession
from elastic_harness.render import Ui
from elastic_harness.tools import Ctx, build_tools, run_tool


def tc_chunks(call_id, name, args):
    half = len(args) // 2
    return [
        {"choices": [{"delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[:half]}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[half:]}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
    ]


def text_chunks(text):
    return [{"choices": [{"delta": {"content": text}}]}, {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 7}}]


class FakeClient:
    def __init__(self, scripts):
        self.scripts, self.seen = list(scripts), []

    def chat_stream(self, model, messages, tools=None, max_tokens=0):
        self.seen.append(json.loads(json.dumps(messages)))
        yield from self.scripts.pop(0)

    def agents(self):
        return []


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["EH_STATE_DIR"] = self.tmp.name
        self.cwd = Path(self.tmp.name)
        (self.cwd / "notes.txt").write_text("first line\nsecond line\n")
        (self.cwd / "creds.local.json").write_text('{"API_KEY":"SECRET"}')
        self.ui = Ui(auto_yes=False)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("EH_STATE_DIR", None)

    def session(self, scripts):
        import elastic_harness.local_agent as la
        la.STATE_DIR = self.cwd / "state"
        client = FakeClient(scripts)
        return LocalSession(Ctx(client, self.ui, self.cwd, "m"), []), client

    def test_tool_round_trip(self):
        s, client = self.session([tc_chunks("c1", "read_file", json.dumps({"path": "notes.txt"})), text_chunks("The first line is 'first line'.")])
        s.turn("what is in notes.txt?")
        roles = [m["role"] for m in s.messages]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])
        self.assertIn("first line", s.messages[2]["content"])
        self.assertEqual(s.messages[2]["tool_call_id"], "c1")
        self.assertEqual(s.messages[1]["tool_calls"][0]["function"]["name"], "read_file")
        # the second request carried the tool result back to the model
        self.assertEqual(client.seen[1][-1]["role"], "tool")
        self.assertEqual((s.tokens_in, s.tokens_out), (30, 12))
        self.assertTrue(s.path.exists())

    def test_secret_file_refused(self):
        s, _ = self.session([tc_chunks("c1", "read_file", json.dumps({"path": "creds.local.json"})), text_chunks("ok")])
        s.turn("read the creds")
        self.assertIn("refused", s.messages[2]["content"])
        self.assertNotIn("SECRET", json.dumps(s.messages))

    def test_declined_write_does_not_write(self):
        s, _ = self.session([tc_chunks("c1", "write_file", json.dumps({"path": "out.txt", "content": "x"})), text_chunks("done")])
        s.turn("write out.txt")  # non-interactive stdin: confirmation is denied
        self.assertFalse((self.cwd / "out.txt").exists())
        self.assertIn("declined", s.messages[2]["content"])

    def test_approved_edit_and_uniqueness(self):
        ui = Ui(auto_yes=True)
        ctx = Ctx(FakeClient([]), ui, self.cwd, "m")
        tools = {t.name: t for t in build_tools()}
        self.assertIn("edited", run_tool(tools["edit_file"], {"path": "notes.txt", "old_string": "second", "new_string": "2nd"}, ctx))
        self.assertIn("2nd line", (self.cwd / "notes.txt").read_text())
        self.assertIn("must match exactly once", run_tool(tools["edit_file"], {"path": "notes.txt", "old_string": "line", "new_string": "x"}, ctx))

    def test_bash_does_not_see_eh_env(self):
        os.environ["EH_API_KEY"] = "leak-me"
        try:
            ctx = Ctx(FakeClient([]), Ui(auto_yes=True), self.cwd, "m")
            out = run_tool({t.name: t for t in build_tools()}["bash"], {"command": "env | grep -c EH_API_KEY; echo done"}, ctx)
            self.assertNotIn("leak-me", out)
            self.assertIn("0", out)
        finally:
            del os.environ["EH_API_KEY"]

    def test_interrupt_leaves_no_dangling_tool_call(self):
        class Boom(FakeClient):
            def chat_stream(self, *a, **k):
                raise KeyboardInterrupt
                yield
        import elastic_harness.local_agent as la
        la.STATE_DIR = self.cwd / "state"
        s = LocalSession(Ctx(Boom([]), self.ui, self.cwd, "m"), [])
        with self.assertRaises(KeyboardInterrupt):
            s.turn("hi")
        self.assertEqual(s.messages, [])


if __name__ == "__main__":
    unittest.main()
