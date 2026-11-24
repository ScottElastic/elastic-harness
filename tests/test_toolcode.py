"""Tools as code, the front-matter parser's nesting and comments, the abort probe, `eh -c`, `eh -p --json`."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import elastic_harness.cli as cli_mod
import elastic_harness.clusters as clusters_mod
import elastic_harness.local_agent as la
import elastic_harness.project as project_mod
import elastic_harness.repl as repl_mod
from elastic_harness import agentcode, credstore, frontmatter, toolcode
from elastic_harness.client import ApiError, classify_abort_probe
from elastic_harness.config import Config, ConfigError, Profile

from fakes import FakeElastic, FakeKeychain, output, quiet_ui

TOOL_FILE = """---
id: kestrel-risks
type: esql
description: Open Kestrel risks owned by one person
tags: [kestrel, demo]
params:
  owner:
    type: string
    description: "Owner's full name: first and last"
  limit:
    type: integer
    description: Most rows to return
    optional: true
    defaultValue: 5
---
FROM eh-local-kestrel
| WHERE owner == ?owner  // why?
| LIMIT ?limit
"""

SEARCH_FILE = """---
id: kestrel-notes
type: index_search
description: Search the Kestrel notes
pattern: eh-local-kestrel
---
"""


def client():
    return FakeElastic(Profile(name="p", kibana_url="https://k", api_key="x"))


class FrontmatterNesting(unittest.TestCase):
    def test_trailing_comments_are_ignored(self):
        # the README's agent example has comments after values; 0.1.0 read `public    # public | private` as the value
        meta, _ = agentcode.parse_agent_file("---\nid: a\nname: A\naccess: public        # public | private\n"
                                             "elastic_capabilities: false   # all off\nlabels: [x, y]  # two\n---\nbody")
        self.assertEqual((meta["access"], meta["elastic_capabilities"], meta["labels"]), ("public", False, ["x", "y"]))

    def test_comment_characters_inside_quotes_and_words_are_kept(self):
        meta, _ = frontmatter.parse('---\na: "x # y"\nb: C# is fine\nc: [a, "b # c"]\n---\n')
        self.assertEqual(meta, {"a": "x # y", "b": "C# is fine", "c": ["a", "b # c"]})

    def test_nested_maps_and_roundtrip(self):
        text = "---\nid: t\nparams:\n  limit:\n    type: integer\n    optional: true\n  who:\n    type: string\ntags:\n  - a\n---\nQ"
        meta, body = frontmatter.parse(text)
        self.assertEqual(meta["params"], {"limit": {"type": "integer", "optional": True}, "who": {"type": "string"}})
        self.assertEqual(meta["tags"], ["a"])
        self.assertEqual(frontmatter.parse(frontmatter.dump(meta, body)), (meta, body))

    def test_list_at_the_keys_own_indent_and_empty_values(self):
        meta, _ = frontmatter.parse("---\ntools:\n- a\n- b\nnone:\nnext: 1\n---\n")
        self.assertEqual(meta, {"tools": ["a", "b"], "none": [], "next": 1})

    def test_bad_indentation_is_an_error(self):
        for bad in ("---\n  indented: 1\n---\n", "---\na: 1\n    b: 2\n---\n", "---\na:\n  - x\n  y: 1\n---\n"):
            with self.assertRaises(ValueError, msg=bad):
                frontmatter.parse(bad)

    def test_strings_that_need_quotes_survive(self):
        tricky = {"quote": 'say "hi"', "multi": "line one\nline two", "num": "42", "hash": "a # b", "colon": "a: b", "lead": "- x"}
        meta, _ = frontmatter.parse(frontmatter.dump(tricky, "b"))
        self.assertEqual(meta, tricky)


class ToolFiles(unittest.TestCase):
    def test_parse_and_build(self):
        meta, query = toolcode.parse_tool_file(TOOL_FILE)
        body = toolcode.build_tool_body(meta, query)
        self.assertEqual(body["id"], "kestrel-risks")
        self.assertEqual(body["tags"], ["kestrel", "demo"])
        self.assertEqual(body["configuration"]["params"]["limit"], {"type": "integer", "description": "Most rows to return",
                                                                  "optional": True, "defaultValue": 5})
        self.assertTrue(body["configuration"]["query"].startswith("FROM eh-local-kestrel\n| WHERE"))
        self.assertEqual(toolcode.build_tool_body(*toolcode.parse_tool_file(SEARCH_FILE))["configuration"],
                         {"pattern": "eh-local-kestrel"})

    def test_roundtrip_through_render(self):
        for text in (TOOL_FILE, SEARCH_FILE):
            meta, q = toolcode.parse_tool_file(text)
            body = toolcode.build_tool_body(meta, q)
            again = toolcode.build_tool_body(*toolcode.parse_tool_file(toolcode.render_tool(body)))
            self.assertEqual(again, body)

    def test_server_fields_do_not_show_as_changes(self):
        body = toolcode.build_tool_body(*toolcode.parse_tool_file(TOOL_FILE))
        served = {**body, "readonly": False, "experimental": False, "confirmation": {"askUser": "never"}, "schema": {"type": "object"}}
        self.assertEqual(toolcode.render_tool(served), toolcode.render_tool(body))

    def test_validation(self):
        bad = {
            "id: kestrel-risks": "id: Bad Id",
            "type: esql": "type: workflow",
            "description: Open Kestrel risks owned by one person": "description:",
            "    type: integer": "    type: keyword",
            "    optional: true": "    optional: maybe",
            "    defaultValue: 5": "    defaultValue: lots",
            "FROM eh-local-kestrel": "",  # an empty query once the rest of the body is also gone, below
        }
        for old, new in bad.items():
            text = TOOL_FILE.replace(old, new)
            if old == "FROM eh-local-kestrel":
                text = TOOL_FILE.split("---\n")[0] + "---\n" + TOOL_FILE.split("---\n")[1] + "---\n\n"
            with self.assertRaises(ValueError, msg=old):
                toolcode.parse_tool_file(text)
        with self.assertRaises(ValueError):
            toolcode.parse_tool_file(TOOL_FILE.replace("tags:", "colour: red\ntags:"))  # unknown key
        with self.assertRaises(ValueError):
            toolcode.parse_tool_file(SEARCH_FILE.replace("pattern: eh-local-kestrel\n", ""))  # no pattern
        with self.assertRaises(ValueError):
            toolcode.parse_tool_file(SEARCH_FILE + "FROM x")  # a search tool has no query
        with self.assertRaises(ValueError):
            toolcode.parse_tool_file(TOOL_FILE.replace("---\nFROM", "pattern: x\n---\nFROM"))  # pattern is for index_search

    def test_placeholders_must_be_declared_and_strings_are_ignored(self):
        with self.assertRaises(ValueError) as cm:
            toolcode.parse_tool_file(TOOL_FILE.replace("LIMIT ?limit", "LIMIT ?limit | WHERE x == ?other"))
        self.assertIn("?other", str(cm.exception))
        ok = TOOL_FILE.replace("// why?", '| WHERE note != "really?yes"')  # a ? inside a string literal is not a parameter
        toolcode.parse_tool_file(ok)
        meta, q = toolcode.parse_tool_file(TOOL_FILE.replace("LIMIT ?limit", "LIMIT 5"))
        self.assertEqual(toolcode.lint(meta, q), ["param 'limit' is declared but the query never uses it"])

    def test_default_values_take_the_declared_type(self):
        meta, _ = toolcode.parse_tool_file(TOOL_FILE.replace("defaultValue: 5", "defaultValue: 2.5").replace("type: integer", "type: float"))
        self.assertEqual(meta["params"]["limit"]["defaultValue"], 2.5)
        meta, _ = toolcode.parse_tool_file(TOOL_FILE.replace("type: integer", "type: string"))
        self.assertEqual(meta["params"]["limit"]["defaultValue"], "5")


class ToolPush(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def push(self, text, c, **kw):
        p = self.d / "t.md"
        p.write_text(text)
        said = []
        res = toolcode.push(c, str(p), confirm=kw.pop("confirm", lambda t, d: True), say=said.append, **kw)
        return res, said

    def test_create_update_unchanged(self):
        c = client()
        res, _ = self.push(TOOL_FILE, c)
        self.assertEqual(res["action"], "created")
        post = next(call for call in c.calls if call[0] == "POST")
        self.assertEqual(post[1], "/api/agent_builder/tools")
        self.assertEqual(post[2]["type"], "esql")
        c.stored_tools = {"kestrel-risks": {**post[2], "readonly": False, "schema": {}}}
        self.assertEqual(self.push(TOOL_FILE, c)[0]["action"], "unchanged")
        res, said = self.push(TOOL_FILE.replace("LIMIT ?limit", "LIMIT ?limit | SORT owner"), c)
        self.assertEqual(res["action"], "updated")
        self.assertIn("+| LIMIT ?limit | SORT owner", "\n".join(said))
        put = next(call for call in c.calls if call[0] == "PUT")
        self.assertEqual(put[1], "/api/agent_builder/tools/kestrel-risks")
        self.assertNotIn("id", put[2])
        self.assertNotIn("type", put[2])  # 9.6.0 rejects `type` on PUT

    def test_guards(self):
        c = client()
        self.assertEqual(self.push(TOOL_FILE, c, confirm=lambda t, d: False)[0]["action"], "cancelled")
        self.assertEqual(self.push(TOOL_FILE, c, dry_run=True)[0]["action"], "would create")
        self.assertFalse([x for x in c.calls if x[0] in ("POST", "PUT")])
        c.stored_tools = {"kestrel-risks": {"id": "kestrel-risks", "type": "builtin", "readonly": True}}
        with self.assertRaises(PermissionError):
            self.push(TOOL_FILE, c)
        c.stored_tools = {"kestrel-risks": {"id": "kestrel-risks", "type": "index_search", "readonly": False,
                                            "configuration": {"pattern": "x"}}}
        with self.assertRaises(ValueError) as cm:
            self.push(TOOL_FILE, c)
        self.assertIn("type cannot change", str(cm.exception))

    def test_pull(self):
        c = client()
        body = toolcode.build_tool_body(*toolcode.parse_tool_file(TOOL_FILE))
        search = toolcode.build_tool_body(*toolcode.parse_tool_file(SEARCH_FILE))
        c.stored_tools = {"kestrel-risks": body, "kestrel-notes": search,
                          "wf": {"id": "wf", "type": "workflow", "readonly": False}}
        out = toolcode.pull(c, "kestrel-risks", str(self.d / "tools"))
        self.assertEqual(toolcode.build_tool_body(*toolcode.parse_tool_file(out.read_text())), body)
        with self.assertRaises(FileExistsError):
            toolcode.pull(c, "kestrel-risks", str(self.d / "tools"))
        toolcode.pull(c, "kestrel-risks", str(self.d / "tools"), force=True)
        with self.assertRaises(ValueError):
            toolcode.pull(c, "wf", str(self.d / "tools"))
        paths = toolcode.pull_all(c, str(self.d / "all"))  # esql and index_search only; built-ins and workflows skipped
        self.assertEqual(sorted(p.name for p in paths), ["eh-search.md", "kestrel-notes.md", "kestrel-risks.md"])

    def test_listing(self):
        c = client()
        c.stored_tools = {"kestrel-risks": toolcode.build_tool_body(*toolcode.parse_tool_file(TOOL_FILE)),
                          "wf": {"id": "wf", "type": "workflow", "readonly": False}}
        rows, footer = toolcode.listing(c)
        self.assertEqual([r[0] for r in rows], ["eh-search", "kestrel-risks"])
        self.assertIn("esql · 2 params", rows[1][1])
        self.assertIn("2 custom tools", footer)
        self.assertIn("2 built-in", footer)
        self.assertIn("1 of other types", footer)


class Compatibility(unittest.TestCase):
    def test_abort_probe_answers(self):
        self.assertEqual(classify_abort_probe(200, '{"acknowledged":false,"terminal_persisted":false}'), "present")
        self.assertEqual(classify_abort_probe(404, '{"statusCode":404,"error":"Not Found","message":"Not Found"}'), "missing")
        # a handler that exists but says the execution is unknown is not a missing route
        self.assertEqual(classify_abort_probe(404, '{"statusCode":404,"message":"Execution abc not found"}'), "unknown")
        for status, body in ((403, "{}"), (500, "oops"), (200, "<html>"), (200, "{}")):
            self.assertEqual(classify_abort_probe(status, body), "unknown")

    def test_notes(self):
        self.assertEqual([lvl for lvl, _ in clusters_mod.compat_notes("9.6.1", "present")], ["ok"])
        notes = clusters_mod.compat_notes("9.7.0", "missing")
        self.assertEqual([lvl for lvl, _ in notes], ["warn", "warn"])
        self.assertIn("9.7.0", notes[0][1])
        self.assertIn("keeps going", notes[1][1])
        self.assertEqual(clusters_mod.compat_notes("?", "unknown")[0][0], "info")
        self.assertEqual(clusters_mod.compat_notes("9.6.0", "unknown")[0][0], "warn")


class OneShotAndResume(unittest.TestCase):
    def setUp(self):
        credstore.use_backend(FakeKeychain())
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        la.STATE_DIR = self.d / "state"
        self.cfg = Config(self.d / "config.json")
        self.cfg.add(Profile(name="alpha", kibana_url="https://alpha.example", es_url="https://es.alpha", api_key="ka",
                             default_agent="elastic-ai-agent"))
        FakeElastic.record = []
        self._stream = FakeElastic.converse_stream

        def recording(inst, **kw):
            FakeElastic.record.append(kw)
            return self._stream(inst, **kw)

        self.patches = [mock.patch.object(repl_mod, "Elastic", FakeElastic), mock.patch.object(clusters_mod, "Elastic", FakeElastic),
                        mock.patch.object(cli_mod, "Elastic", FakeElastic), mock.patch.object(cli_mod, "Config", lambda: self.cfg),
                        mock.patch.object(project_mod, "CONFIG_DIR", self.d / "cfg"),
                        mock.patch.object(FakeElastic, "converse_stream", recording)]
        for p in self.patches:
            p.start()
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

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch("sys.stdin", io.StringIO("")):
            with mock.patch("sys.stdin.isatty", return_value=True, create=True):
                code = cli_mod.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_json_output_is_only_json_on_stdout(self):
        code, out, err = self.run_cli("-p", "hi there", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)  # raises if anything else reached stdout
        self.assertEqual((data["ok"], data["answer"], data["conversation_id"], data["agent"], data["mode"]),
                         (True, "hello world", "c1", "elastic-ai-agent", "agent"))
        self.assertEqual(data["usage"]["input_tokens"], 7)
        self.assertEqual(data["tools"], [])
        self.assertIn("in /", err)  # the usage line went to stderr

    def test_json_records_tools(self):
        events = [("conversation_id_set", {"conversation_id": "c9"}), ("tool_call", {"tool_id": "eh-search", "params": {}}),
                  ("message_chunk", {"text_chunk": "done"})]
        orig = FakeElastic.__init__

        def init(inst, profile):
            orig(inst, profile)
            inst.stream_events = events
        with mock.patch.object(FakeElastic, "__init__", init):
            code, out, _ = self.run_cli("-p", "hi", "--json")
        self.assertEqual((code, json.loads(out)["tools"], json.loads(out)["answer"]), (0, ["eh-search"], "done"))

    def test_a_failed_one_shot_exits_nonzero(self):
        with mock.patch.object(FakeElastic, "converse_stream", side_effect=ApiError(500, "boom")):
            code, out, err = self.run_cli("-p", "hi")
            self.assertEqual(code, 1)
            self.assertIn("boom", err + out)
            code, out, _ = self.run_cli("-p", "hi", "--json")
        data = json.loads(out)
        self.assertEqual((code, data["ok"], "boom" in data["error"]), (1, False, True))

    def test_json_errors_before_the_prompt_are_json_too(self):
        code, out, _ = self.run_cli("--cluster", "nope", "-p", "hi", "--json")
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)["ok"])

    def test_flag_combinations(self):
        for argv in (["--json"], ["-c", "agents"], ["-c", "--resume", "x"]):
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
                cli_mod.main(argv)
            self.assertEqual(cm.exception.code, 2, argv)

    def test_continue_picks_the_latest_conversation(self):
        convs = [{"id": "c-old", "title": "old", "agent_id": "eh-demo", "updated_at": "2026-11-01T00:00:00Z"},
                 {"id": "c-new", "title": "new", "agent_id": "elastic-ai-agent", "updated_at": "2026-11-20T00:00:00Z"},
                 {"id": "c-mid", "title": "mid", "agent_id": "eh-demo", "updated_at": "2026-11-10T00:00:00Z"}]
        with mock.patch.object(FakeElastic, "conversations", lambda inst: convs):
            code, out, _ = self.run_cli("-c", "-p", "and then?", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(FakeElastic.record[-1]["conversation_id"], "c-new")
            self.assertEqual(json.loads(out)["agent"], "elastic-ai-agent")
            self.run_cli("-c", "--agent", "eh-demo", "-p", "and then?", "--json")  # the latest one WITH that agent
            self.assertEqual((FakeElastic.record[-1]["conversation_id"], FakeElastic.record[-1]["agent_id"]), ("c-mid", "eh-demo"))
            self.run_cli("--resume", "c-old", "-p", "x", "--json")
            self.assertEqual(FakeElastic.record[-1]["conversation_id"], "c-old")

    def test_continue_with_nothing_to_continue(self):
        with mock.patch.object(FakeElastic, "conversations", lambda inst: []):
            code, out, err = self.run_cli("-c", "-p", "hi", "--json")
        self.assertEqual(code, 2)
        self.assertIn("no earlier conversation", json.loads(out)["error"])

    def test_continue_in_local_mode_takes_the_newest_session(self):
        sessions = la.STATE_DIR / "sessions"
        sessions.mkdir(parents=True)
        for sid, first in (("20261101-090000-aaaa", "old question"), ("20261120-100000-bbbb", "new question")):
            (sessions / f"{sid}.json").write_text(json.dumps({"id": sid, "saved": sid, "messages": [{"role": "user", "content": first}]}))
        r = repl_mod.Repl(self.cfg, quiet_ui(), mode="local")
        r.resume_for_cli(None)
        self.assertEqual((r.local.id, r.local.messages[0]["content"]), ("20261120-100000-bbbb", "new question"))
        for f in sessions.glob("*.json"):
            f.unlink()
        with self.assertRaises(ConfigError):
            repl_mod.Repl(self.cfg, quiet_ui(), mode="local").resume_for_cli(None)

    def test_doctor_reports_version_and_abort_route(self):
        ui = quiet_ui()
        repl_mod.Repl(self.cfg, ui).cmd_doctor("")
        self.assertIn("Ctrl+C can stop agent runs", output(ui))
        self.assertNotIn("rehearsed on", output(ui))
        ui = quiet_ui()
        with mock.patch.object(FakeElastic, "version", lambda inst: "9.7.2"), mock.patch.object(FakeElastic, "abort_route", "missing"):
            repl_mod.Repl(self.cfg, ui).cmd_doctor("")
        self.assertIn("rehearsed on 9.6", output(ui))
        self.assertIn("abort route is missing", output(ui))

    def test_tool_command_in_a_session(self):
        ui = quiet_ui()
        r = repl_mod.Repl(self.cfg, ui)
        Path("t.md").write_text(TOOL_FILE)
        r.handle("/tool push t.md --dry-run")
        self.assertIn("would create", output(ui))
        r.handle("/tool list")
        self.assertIn("custom tools here", output(ui))
        r.handle("/tool")
        self.assertIn("usage: /tool", output(ui))
        self.assertIn("tool", repl_mod.COMMANDS)

    def test_tool_subcommand(self):
        Path("t.md").write_text(TOOL_FILE)
        code, out, err = self.run_cli("tool", "diff", "t.md")
        self.assertEqual(code, 0)
        self.assertIn("would create", out + err)
        code, out, err = self.run_cli("tool", "push", "bad.md")
        self.assertEqual(code, 1)  # FileNotFoundError is an OSError


if __name__ == "__main__":
    unittest.main()
