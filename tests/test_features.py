import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import elastic_harness.clusters as clusters_mod
import elastic_harness.local_agent as la
import elastic_harness.project as project_mod
import elastic_harness.repl as repl_mod
from elastic_harness import agentcode, credstore, frontmatter, transcript
from elastic_harness.agent_chat import run_turn
from elastic_harness.client import ApiError
from elastic_harness.config import Config, ConfigError, Profile
from elastic_harness.local_agent import LocalSession, estimate_tokens, trim_tool_results
from elastic_harness.mentions import _ELASTIC, expand_elastic_mentions
from elastic_harness.project import Command, find_context, load_commands
from elastic_harness.tools import Ctx

from fakes import FakeElastic, FakeKeychain, output, quiet_ui


class Frontmatter(unittest.TestCase):
    def test_parse_and_roundtrip(self):
        text = '---\nid: a-1\nname: "My: Agent"\nlabels: [x, "y z"]\nflag: true\nn: 3\ntools:\n  - t1\n  - t2\n---\nbody line\n\nmore\n'
        meta, body = frontmatter.parse(text)
        self.assertEqual(meta, {"id": "a-1", "name": "My: Agent", "labels": ["x", "y z"], "flag": True, "n": 3, "tools": ["t1", "t2"]})
        self.assertEqual(body, "body line\n\nmore")
        again, body2 = frontmatter.parse(frontmatter.dump(meta, body))
        self.assertEqual((again, body2), (meta, body))

    def test_errors_and_no_frontmatter(self):
        self.assertEqual(frontmatter.parse("just text"), ({}, "just text"))
        with self.assertRaises(ValueError):
            frontmatter.parse("---\nid: x\nno close")
        with self.assertRaises(ValueError):
            frontmatter.parse("---\n???\n---\nb")


AGENT_FILE = """---
id: kestrel-analyst
name: Kestrel Analyst
description: Answers from notes
labels: [kestrel]
access: public
elastic_capabilities: false
tools:
  - eh-search
---
Use only the search tool.
"""


class AgentCode(unittest.TestCase):
    def test_parse_and_build(self):
        meta, instr = agentcode.parse_agent_file(AGENT_FILE)
        body = agentcode.build_agent_body(meta, instr)
        self.assertEqual(body["configuration"], {"instructions": "Use only the search tool.", "tools": [{"tool_ids": ["eh-search"]}],
                                                 "enable_elastic_capabilities": False})
        self.assertEqual((body["id"], body["labels"]), ("kestrel-analyst", ["kestrel"]))

    def test_roundtrip_through_render(self):
        meta, instr = agentcode.parse_agent_file(AGENT_FILE)
        body = agentcode.build_agent_body(meta, instr)
        rendered = agentcode.render_agent({**body, "access_control": {"access_mode": "public"}})
        meta2, instr2 = agentcode.parse_agent_file(rendered)
        self.assertEqual(agentcode.build_agent_body(meta2, instr2), body)

    def test_validation(self):
        for bad in ["---\nid: Bad Id\nname: x\n---\nb", "---\nid: ok\n---\nb", "---\nid: ok\nname: x\n---\n", "---\nid: ok\nname: x\nbogus: 1\n---\nb",
                    "---\nid: ok\nname: x\naccess: shared\n---\nb"]:
            with self.assertRaises(ValueError, msg=bad):
                agentcode.parse_agent_file(bad)

    def test_risky(self):
        self.assertEqual(agentcode.risky_tools(["a", "platform.core.resume_workflow_execution", "platform.workflows.execute_workflow"]),
                         ["platform.core.resume_workflow_execution", "platform.workflows.execute_workflow"])

    def _push(self, text, client, **kw):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.md"
            p.write_text(text)
            said = []
            res = agentcode.push(client, str(p), confirm=kw.pop("confirm", lambda t, d: True), say=said.append, **kw)
            return res, said

    def test_push_create_update_unchanged(self):
        c = FakeElastic(Profile(name="p", kibana_url="https://k", api_key="x"))
        res, _ = self._push(AGENT_FILE, c)
        self.assertEqual(res["action"], "created")
        self.assertTrue(any(call[0] == "POST" and call[1] == "/api/agent_builder/agents" for call in c.calls))
        meta, instr = agentcode.parse_agent_file(AGENT_FILE)
        stored = {**agentcode.build_agent_body(meta, instr), "access_control": {"access_mode": "public"}}
        c.stored_agents = {"kestrel-analyst": stored}
        res, _ = self._push(AGENT_FILE, c)
        self.assertEqual(res["action"], "unchanged")
        res, said = self._push(AGENT_FILE.replace("Use only the search tool.", "Be brief."), c)
        self.assertEqual(res["action"], "updated")
        self.assertIn("-Use only the search tool.", "\n".join(said))
        self.assertTrue(any(call[0] == "PUT" for call in c.calls))

    def test_push_guards(self):
        c = FakeElastic(Profile(name="p", kibana_url="https://k", api_key="x"))
        with self.assertRaises(PermissionError):
            self._push(AGENT_FILE.replace("  - eh-search", "  - platform.core.resume_workflow_execution"), c)
        with self.assertRaises(ValueError):
            self._push(AGENT_FILE.replace("eh-search", "nope"), c)
        res, _ = self._push(AGENT_FILE, c, confirm=lambda t, d: False)
        self.assertEqual(res["action"], "cancelled")
        res, _ = self._push(AGENT_FILE, c, dry_run=True)
        self.assertEqual(res["action"], "would create")
        c.stored_agents = {"kestrel-analyst": {"id": "kestrel-analyst", "readonly": True}}
        with self.assertRaises(PermissionError):
            self._push(AGENT_FILE, c)


class Mentions(unittest.TestCase):
    def test_pattern_ignores_emails_and_files(self):
        for t in ["mail me@alert:x.com", "see @notes.md", "@alerts are noisy"]:
            self.assertIsNone(_ELASTIC.search(t), t)
        self.assertEqual(_ELASTIC.search("triage @alert:abc123 now").groups(), ("alert", "abc123"))
        self.assertEqual(_ELASTIC.search("fields of @index:logs-*, ok").group(2), "logs-*")

    def test_expand(self):
        c = FakeElastic(Profile(name="p", kibana_url="https://k", api_key="x"))
        text, notes = expand_elastic_mentions("Triage @alert:a1 and @case:c1 and @index:eh-local-x and @alert:missing", c)
        self.assertIn("Triage alert a1 and case c1 and index eh-local-x and alert missing", text)
        self.assertIn('<elastic_alert id="a1"', text)
        self.assertIn("kibana.alert.rule.name: Rule A", text)
        self.assertNotIn("kibana.space_ids", text)
        self.assertIn("first note", text)
        self.assertIn("alert-1", text)
        self.assertIn("a:keyword", text)
        st = {n["ref"]: n["status"] for n in notes}
        self.assertEqual(st["alert:a1"], "attached")
        self.assertIn("not found", st["alert:missing"])


class Project(unittest.TestCase):
    def test_context_walks_up_and_command_precedence(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as cfgdir:
            home = Path(d).resolve()
            proj = home / "work" / "cust"
            (proj / "sub").mkdir(parents=True)
            (home / "EH.md").write_text("HOME RULES")
            (proj / "EH.md").write_text("PROJECT RULES")
            with mock.patch.object(project_mod, "CONFIG_DIR", Path(cfgdir)):
                files = find_context(proj / "sub", home)
                self.assertEqual([t.strip() for _, t in files], ["HOME RULES", "PROJECT RULES"])
                (Path(cfgdir) / "commands").mkdir()
                (Path(cfgdir) / "commands" / "triage.md").write_text("---\ndescription: user one\n---\nUSER $ARGS")
                (Path(cfgdir) / "commands" / "help.md").write_text("nope")
                (proj / ".eh" / "commands").mkdir(parents=True)
                (proj / ".eh" / "commands" / "triage.md").write_text("---\ndescription: project one\nmode: local\nmodel: sonnet\n---\nPROJ $1 then $2")
                (proj / ".eh" / "commands" / "Bad Name.md").write_text("x")
                cmds, warns = load_commands(proj, reserved={"help"})
                self.assertEqual(cmds["triage"].description, "project one")
                self.assertEqual((cmds["triage"].mode, cmds["triage"].model), ("local", "sonnet"))
                self.assertNotIn("help", cmds)
                self.assertEqual(len(warns), 2)

    def test_expand(self):
        self.assertEqual(Command("x", "Look at $ARGS now").expand("a b"), "Look at a b now")
        self.assertEqual(Command("x", "first $1 second $2 third $3").expand("one two"), "first one second two third ")
        self.assertEqual(Command("x", "Do the thing").expand("extra words"), "Do the thing\n\nextra words")
        self.assertEqual(Command("x", "Do the thing").expand(""), "Do the thing")


class Compaction(unittest.TestCase):
    def msgs(self):
        out = []
        for i in range(3):
            out += [{"role": "user", "content": f"question {i}"},
                    {"role": "assistant", "content": "", "tool_calls": [{"id": f"t{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": f"t{i}", "content": "BIG " * 2000},
                    {"role": "assistant", "content": f"answer {i}"}]
        return out

    def test_trim_keeps_recent(self):
        m = self.msgs()
        saved = trim_tool_results(m)
        self.assertGreater(saved, 7_000)  # only the old tool result is outside the recent window
        self.assertTrue(m[2]["content"].startswith("[trimmed"))
        self.assertFalse(m[10]["content"].startswith("[trimmed"))  # last tool result is in the recent window

    def session(self, messages):
        c = FakeElastic(Profile(name="p", kibana_url="https://k", api_key="x"))
        with tempfile.TemporaryDirectory() as d:
            la.STATE_DIR = Path(d)
            s = LocalSession(Ctx(c, quiet_ui(), Path(d), "m"), [])
        s.messages = messages
        return s, c

    def test_compact_summarises_head_and_keeps_valid_order(self):
        s, c = self.session(self.msgs())
        before, after = s.compact(force=True)
        self.assertLess(after, before * 0.5)  # the latest turn, tool output included, stays verbatim
        self.assertEqual(s.messages[0]["role"], "user")
        self.assertTrue(s.messages[0]["content"].startswith(la.SUMMARY_TAG))
        self.assertIn("Priya", s.messages[0]["content"])
        self.assertEqual(s.messages[1]["role"], "assistant")
        self.assertEqual(s.messages[2], {"role": "user", "content": "question 2"})  # the latest turn is kept verbatim
        # every tool message still follows an assistant message with a matching tool_call
        ids = set()
        for m in s.messages:
            for tc in m.get("tool_calls") or []:
                ids.add(tc["id"])
            if m["role"] == "tool":
                self.assertIn(m["tool_call_id"], ids)

    def test_auto_compaction_keeps_two_turns_when_they_fit(self):
        msgs = []
        for i in range(6):
            msgs += [{"role": "user", "content": f"question {i} " + "q" * 150}, {"role": "assistant", "content": f"answer {i} " + "a" * 150}]
        s, c = self.session(msgs)
        total = estimate_tokens(msgs)
        with mock.patch.object(la, "CONTEXT_TOKENS", int(total * 0.9)):
            s.compact()
        users = [m["content"][:10] for m in s.messages if m["role"] == "user"]
        self.assertEqual(users[0][:9], "[Summary ")
        self.assertEqual(users[1:], ["question 4", "question 5"])  # the last two turns stayed verbatim
        s2, _ = self.session(msgs)
        with mock.patch.object(la, "CONTEXT_TOKENS", int(total * 0.9)):
            s2.compact(force=True)
        self.assertEqual([m["content"][:10] for m in s2.messages if m["role"] == "user"][1:], ["question 5"])  # forced: only the latest

    def test_summary_input_is_not_chopped(self):
        big = "R3,Night shift,Priya Nair,medium,open\n" * 50
        text = la._transcript([{"role": "tool", "content": big}])
        self.assertIn("Priya Nair", text)
        self.assertNotIn("shortened", text)
        self.assertIn("shortened for this summary", la._transcript([{"role": "tool", "content": "x" * 9000}]))

    def test_auto_compaction_before_a_request(self):
        s, c = self.session(self.msgs())
        self.assertGreater(estimate_tokens(s.messages), 3000)
        with mock.patch.object(la, "CONTEXT_TOKENS", 3000):
            s._make_room()
        self.assertLess(estimate_tokens(s.messages), 3000)
        self.assertIn(("chat", "m"), c.calls)

    def test_context_error_triggers_one_compact_retry(self):
        s, c = self.session(self.msgs()[:4])
        calls = {"n": 0}
        real = s._complete

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ApiError(400, "prompt is too long: 250000 tokens > 200000 maximum")
            return "ok", [], None
        s._complete = flaky
        s.turn("another question")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(s.messages[-1]["content"], "ok")


class Transcript(unittest.TestCase):
    def test_code_block_and_markdown(self):
        self.assertEqual(transcript.last_code_block("a\n```py\nx=1\n```\nb\n```\ny=2\n```\n"), "y=2")
        self.assertIsNone(transcript.last_code_block("none"))
        md = transcript.agent_markdown(FakeElastic(Profile(name="p", kibana_url="https://k", api_key="x")).conversation("c"), {"cluster": "p"})
        self.assertIn("## You\n\nhi", md)
        self.assertIn("`eh-search`", md)
        self.assertIn("hello there", md)
        lm = transcript.local_markdown([{"role": "user", "content": "q"}, {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "grep", "arguments": "{}"}}]}, {"role": "tool", "content": "res"}, {"role": "assistant", "content": "a"}], {})
        self.assertIn("tool `grep`", lm)
        self.assertIn("result: res", lm)

    def test_clipboard(self):
        with mock.patch.object(transcript.shutil, "which", lambda n: "/usr/bin/" + n if n == "xclip" else None), \
                mock.patch.object(transcript.sys, "platform", "linux"):
            self.assertEqual(transcript.clipboard_command()[0], "xclip")
        with mock.patch.object(transcript.shutil, "which", lambda n: None):
            with self.assertRaises(RuntimeError):
                transcript.copy_to_clipboard("x")
        with mock.patch.object(transcript, "clipboard_command", lambda: ["cat"]), mock.patch.object(transcript.subprocess, "run") as run:
            self.assertEqual(transcript.copy_to_clipboard("héllo"), "cat")
            self.assertEqual(run.call_args.kwargs["input"], "héllo".encode())


class Keychain(unittest.TestCase):
    def setUp(self):
        self.kc = FakeKeychain()
        credstore.use_backend(self.kc)
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(Path(self.tmp.name) / "config.json")

    def tearDown(self):
        credstore.use_backend(None)
        credstore._checked = False
        self.tmp.cleanup()

    def test_key_goes_to_keychain_not_the_file(self):
        p = Profile(name="prod", kibana_url="https://k.example", es_url="https://e.example")
        self.assertEqual(self.cfg.store_key(p, "SECRET-KEY"), "keychain")
        self.cfg.add(p)
        self.assertNotIn("SECRET-KEY", (Path(self.tmp.name) / "config.json").read_text())
        again = Config(Path(self.tmp.name) / "config.json").get("prod")
        self.assertEqual(again.resolved().api_key, "SECRET-KEY")
        self.assertEqual(Config.key_source(again), "keychain")
        self.cfg.remove("prod")
        self.assertEqual(self.kc.items, {})

    def test_missing_keychain_entry_is_a_clear_error(self):
        p = Profile(name="gone", kibana_url="https://k", key_store="keychain")
        with self.assertRaises(ConfigError) as cm:
            p.resolved()
        self.assertIn("not in the Keychain", str(cm.exception))

    def test_secure_moves_plaintext_key(self):
        self.cfg.add(Profile(name="old", kibana_url="https://k", api_key="PLAIN"))
        self.assertIn("PLAIN", (Path(self.tmp.name) / "config.json").read_text())
        self.assertEqual(self.cfg.secure("old"), "keychain")
        self.assertNotIn("PLAIN", (Path(self.tmp.name) / "config.json").read_text())

    def test_fallback_to_file_without_keychain(self):
        credstore.use_backend(None)
        p = Profile(name="x", kibana_url="https://k")
        self.assertEqual(self.cfg.store_key(p, "K"), "config file")
        self.assertEqual(p.api_key, "K")

    def test_fuzzy_get(self):
        self.cfg.add(Profile(name="acme-prod", kibana_url="https://a"))
        self.cfg.add(Profile(name="acme-dev", kibana_url="https://b"), make_active=False)
        self.assertEqual(self.cfg.get("prod").name, "acme-prod")
        with self.assertRaises(ConfigError):
            self.cfg.get("acme")
        with self.assertRaises(ConfigError):
            self.cfg.get("zzz")

    def test_add_cluster_flows(self):
        with mock.patch.object(clusters_mod, "Elastic", FakeElastic):
            prof, where, ver = clusters_mod.add_cluster(self.cfg, "typed", kibana_url="https://k", es_url="https://e",
                                                        secret=lambda q: "TYPED-KEY")
            self.assertEqual((where, ver, self.kc.get_password(credstore.SERVICE, "typed")), ("keychain", "9.6.0", "TYPED-KEY"))
            self.assertEqual(self.cfg.active, "typed")  # first cluster becomes active
            creds = Path(self.tmp.name) / "c.json"
            creds.write_text(json.dumps({"KIBANA_URL": "https://k2", "ES_URL": "https://e2", "API_KEY": "FILE-KEY"}))
            clusters_mod.add_cluster(self.cfg, "inplace", creds=str(creds))
            self.assertNotIn("FILE-KEY", (Path(self.tmp.name) / "config.json").read_text())
            self.assertEqual(self.cfg.active, "typed")  # adding another does not switch
            clusters_mod.add_cluster(self.cfg, "imported", import_file=str(creds))
            self.assertEqual(self.kc.get_password(credstore.SERVICE, "imported"), "FILE-KEY")
            with self.assertRaises(ConfigError):
                clusters_mod.add_cluster(self.cfg, "bad name!", kibana_url="https://k", secret=lambda q: "k")

    def test_bad_key_saves_nothing(self):
        class Boom(FakeElastic):
            def version(self):
                raise ApiError(401, "unauthorized")
        with mock.patch.object(clusters_mod, "Elastic", Boom):
            with self.assertRaises(ApiError):
                clusters_mod.add_cluster(self.cfg, "x", kibana_url="https://k", es_url="https://e", secret=lambda q: "bad")
        self.assertEqual((self.cfg.profiles, self.kc.items), ({}, {}))


class ReplFeatures(unittest.TestCase):
    def setUp(self):
        credstore.use_backend(FakeKeychain())
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        la.STATE_DIR = self.d / "state"
        self.cfg = Config(self.d / "config.json")
        self.cfg.add(Profile(name="alpha", kibana_url="https://alpha.example", es_url="https://es.alpha", api_key="ka", default_agent="eh-demo"))
        self.cfg.add(Profile(name="beta", kibana_url="https://beta.example", es_url="https://es.beta", api_key="kb"), make_active=False)
        self.patches = [mock.patch.object(repl_mod, "Elastic", FakeElastic), mock.patch.object(clusters_mod, "Elastic", FakeElastic),
                        mock.patch.object(project_mod, "CONFIG_DIR", self.d / "cfg")]
        for p in self.patches:
            p.start()
        self.ui = quiet_ui()
        self.cwd = self.d / "proj"
        self.cwd.mkdir()
        os.chdir(self.cwd)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        credstore.use_backend(None)
        credstore._checked = False
        os.chdir(self.d.parent)
        self.tmp.cleanup()

    def repl(self, **kw):
        return repl_mod.Repl(self.cfg, self.ui, **kw)

    def test_space_switch(self):
        r = self.repl()
        r.handle("/space demo")
        self.assertEqual(r.client.space, "demo")
        self.assertEqual(r.agent_id, "elastic-ai-agent")  # eh-demo does not exist in that space
        self.assertIn("not in space demo", output(self.ui))
        r.handle("/space nope")
        self.assertEqual(r.client.space, "demo")
        self.assertIn("no space 'nope'", output(self.ui))
        self.assertEqual(self.repl(space="demo").client.space, "demo")
        r.handle("/space default")
        self.assertEqual(r.agent_id, "eh-demo")  # the preferred agent returns when the space has it again

    def test_cluster_switch_add_rm_test(self):
        r = self.repl()
        r.handle("/cluster")
        self.assertIn("alpha", output(self.ui))
        r.handle("/cluster beta")
        self.assertEqual((r.raw.name, self.cfg.active, r.client.kibana_url), ("beta", "beta", "https://beta.example"))
        self.assertEqual(r.mode, "agent")
        r.handle("/cluster test")
        self.assertIn("9.6.0", output(self.ui))
        r.handle("/cluster rm beta")
        self.assertIn("switch to another first", output(self.ui))
        r.handle("/cluster alpha")
        r.handle("/cluster rm beta")
        self.assertNotIn("beta", self.cfg.profiles)
        r.handle("/cluster nope")
        self.assertIn("no cluster 'nope'", output(self.ui))

    def test_retry_copy_export_open(self):
        r = self.repl()
        r.handle("what is up")
        self.assertEqual((r.conversation_id, r.last_prompt), ("c1", "what is up"))
        self.assertEqual(r.ui.last_answer, "hello world")
        with mock.patch.object(transcript, "copy_to_clipboard", return_value="pbcopy") as cp:
            r.handle("/copy")
            cp.assert_called_with("hello world")
        out = self.d / "t.md"
        r.handle(f"/export {out}")
        self.assertIn("hello there", out.read_text())
        r.handle(f"/export {out}")
        self.assertIn("already exists", output(self.ui))
        urls = []
        with mock.patch.object(transcript, "open_url", lambda u: urls.append(u) or True):
            r.handle("/open")
            r.handle("/open agent")
            r.handle("/open case abc")
        self.assertEqual(urls[0], "https://alpha.example/app/agent_builder/agents/eh-demo/conversations/c1")
        self.assertEqual(urls[1], "https://alpha.example/app/agent_builder/agents/eh-demo")
        self.assertEqual(urls[2], "https://alpha.example/app/security/cases/abc")
        calls_before = len([c for c in r.client.calls if c[0] == "stream"])
        r.handle("/retry")
        streams = [c for c in r.client.calls if c[0] == "stream"]
        self.assertEqual(len(streams), calls_before + 1)
        self.assertIsNone(streams[-1][1]["conversation_id"])  # fresh conversation
        r.handle("/space demo")
        r.handle("/space default")
        self.assertIsNone(r.conversation_id)

    def test_compare(self):
        r = self.repl()
        r.handle("/compare haiku | sonnet 5.5 :: say hi")
        self.assertEqual(sorted(r.client.deleted), ["conv-.anthropic-claude-4.5-haiku-chat_completion", "conv-.anthropic-claude-5.5-sonnet-chat_completion"])
        text = output(self.ui)
        self.assertIn("answer from .anthropic-claude-4.5-haiku", text)
        self.assertIn("answer from .anthropic-claude-5.5-sonnet", text)
        r.handle("/compare haiku | opus")  # no :: → last prompt
        self.assertEqual(r.last_prompt, "say hi")
        r.handle("/compare haiku")
        self.assertIn("usage: /compare", output(self.ui))
        r.handle("/compare 5.5 | haiku :: x")  # "5.5" matches two models
        self.assertIn("matches 2 models", output(self.ui))

    def test_custom_command_and_eh_md(self):
        (self.cwd / "EH.md").write_text("Always answer in French.")
        (self.cwd / ".eh" / "commands").mkdir(parents=True)
        (self.cwd / ".eh" / "commands" / "owner.md").write_text("---\ndescription: who owns it\nmodel: opus\n---\nWho owns $1?")
        r = self.repl()
        self.assertEqual(list(r.cmds), ["owner"])
        r.handle("/owner kestrel")
        s1 = [c for c in r.client.calls if c[0] == "stream"][0][1]
        self.assertIn("<project_context>", s1["text"])
        self.assertIn("Always answer in French.", s1["text"])
        self.assertIn("Who owns kestrel?", s1["text"])
        self.assertEqual(s1["model"], ".anthropic-claude-5.5-opus-chat_completion")
        self.assertEqual(r.model, "")  # the command's model was for that run only
        r.handle("second question")
        s2 = [c for c in r.client.calls if c[0] == "stream"][1][1]
        self.assertNotIn("project_context", s2["text"])  # attached once per conversation

    def test_mentions_in_messages(self):
        r = self.repl()
        r.handle("triage @alert:a1 please")
        sent = [c for c in r.client.calls if c[0] == "stream"][0][1]["text"]
        self.assertIn("triage alert a1 please", sent)
        self.assertIn("Rule A", sent)

    def test_ctrl_c_aborts_the_server_run(self):
        c = FakeElastic(Profile(name="p", kibana_url="https://k", api_key="x"))
        c.raise_interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            run_turn(c, self.ui, agent_id="a", text="go", conversation_id=None, model=None)
        sent_eid = [call for call in c.calls if call[0] == "stream"][0][1]["execution_id"]
        self.assertEqual(c.aborted, [sent_eid])
        self.assertIn("stopped the agent run", output(self.ui))


if __name__ == "__main__":
    unittest.main()
