import io
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import elastic_harness.clusters as clusters_mod
import elastic_harness.local_agent as la
import elastic_harness.project as project_mod
import elastic_harness.repl as repl_mod
from elastic_harness.client import ApiError
from elastic_harness.config import Config, Profile
from elastic_harness.render import Ui
from elastic_harness.status import STATES, TabStatus, clean, mode_from_env, supported

from fakes import FakeElastic, FakeKeychain
from rich.console import Console
from elastic_harness import credstore


class TTY(io.StringIO):
    def isatty(self):
        return True


IT = {"TERM_PROGRAM": "iTerm.app"}
OSC = re.compile(r"\x1b\]21337;(.*?)\x07", re.S)


def fields(payload: str) -> dict:
    return dict(kv.split("=", 1) for kv in payload.split(";"))


def sent(stream: io.StringIO) -> list[dict]:
    return [fields(m) for m in OSC.findall(stream.getvalue())]


class Basics(unittest.TestCase):
    def test_exact_sequences_match_what_iterm_reported(self):
        out = TTY()
        t = TabStatus(stream=out, env=IT)
        t.set("working", "Who owns Kestrel?")
        t.set("done", "Priya Nair owns it.")
        self.assertEqual(out.getvalue(),
                         "\x1b]21337;status=working;indicator=#ff9500;status-color=#ff9500;detail=Who owns Kestrel?\x07"
                         "\x1b]21337;status=done;indicator=#00d75f;status-color=#00d75f;detail=Priya Nair owns it.\x07")
        self.assertEqual((STATES["working"][0], STATES["done"][0]), ("#ff9500", "#00d75f"))  # as read from live Claude Code tabs

    def test_idle_clears_the_subtitle_color_so_iterms_default_shows(self):
        out = TTY()
        TabStatus(stream=out, env=IT).set("idle")
        self.assertEqual(sent(out), [{"status": "idle", "indicator": "#00d75f", "status-color": "", "detail": ""}])

    def test_clear_only_if_something_was_set(self):
        out = TTY()
        t = TabStatus(stream=out, env=IT)
        t.clear()
        self.assertEqual(out.getvalue(), "")
        t.set("done")
        t.clear()
        t.clear()
        self.assertEqual(sent(out)[-1], {"status": "", "indicator": "", "status-color": "", "detail": ""})
        self.assertEqual(len(sent(out)), 2)

    def test_detail_is_safe_for_the_payload(self):
        out = TTY()
        t = TabStatus(stream=out, env=IT)
        t.set("done", "**Bold** a;b\x1b]0;evil\x07 line1\nline2 `code` " + "x" * 300)
        d = sent(out)[0]["detail"]
        self.assertNotIn(";", d)
        self.assertNotIn("\x1b", d)
        self.assertNotIn("\n", d)
        self.assertNotIn("**", d)
        self.assertLessEqual(len(d), 160)
        self.assertTrue(d.endswith("…"))
        self.assertEqual(len(sent(out)), 1)  # injected control sequences did not become a second sequence
        self.assertEqual(clean(None), "")

    def test_tmux_passthrough(self):
        out = TTY()
        TabStatus(stream=out, env={**IT, "TMUX": "/tmp/tmux-501/default,1,0"}).set("done")
        v = out.getvalue()
        self.assertTrue(v.startswith("\x1bPtmux;\x1b\x1b]21337;status=done"))
        self.assertTrue(v.endswith("\x07\x1b\\"))

    def test_never_writes_outside_iterm_or_when_off_or_without_a_tty(self):
        for env, kw in [({}, {}), ({"TERM_PROGRAM": "Apple_Terminal"}, {}), (IT, {"mode": "off"}), ({**IT, "EH_TAB_STATUS": "0"}, {})]:
            out = TTY()
            t = TabStatus(stream=out, env=env, **kw)
            t.set("working")
            t.clear()
            self.assertEqual(out.getvalue(), "", (env, kw))
        plain = io.StringIO()  # not a tty and not injected: stdout/stderr are pytest-style captures here
        t = TabStatus(stream=None, env=IT)
        with mock.patch("elastic_harness.status.sys.stdout", plain), mock.patch("elastic_harness.status.sys.stderr", plain):
            t = TabStatus(env=IT)
            t.set("working")
        self.assertEqual(plain.getvalue(), "")
        self.assertTrue(supported({"LC_TERMINAL": "iTerm2"}))  # also over ssh
        self.assertEqual([mode_from_env({"EH_TAB_STATUS": v}) for v in ("1", "basic", "OFF", "no", "")], ["on", "basic", "off", "off", "on"])

    def test_basic_mode_hides_the_snippet(self):
        out = TTY()
        t = TabStatus(stream=out, env=IT, mode="basic")
        t.set("done", "a private answer")
        self.assertEqual(sent(out)[0]["detail"], "")
        self.assertEqual(sent(out)[0]["status"], "done")

    def test_set_mode_switches_live(self):
        out = TTY()
        t = TabStatus(stream=out, env=IT)
        t.set("done", "answer")
        t.set_mode("basic")
        self.assertEqual(sent(out)[-1]["detail"], "")  # re-sent without the text straight away
        t.set_mode("off")
        self.assertEqual(sent(out)[-1]["status"], "")  # and cleared
        n = len(sent(out))
        t.set("working")
        self.assertEqual(len(sent(out)), n)
        with self.assertRaises(ValueError):
            t.set_mode("loud")

    def test_working_context_restores_previous_state(self):
        out = TTY()
        t = TabStatus(stream=out, env=IT)
        t.set("done", "x")
        with t.working("/compare"):
            pass
        self.assertEqual([m["status"] for m in sent(out)], ["done", "working", "done"])
        with t.working():
            t.set("error", "boom")  # the block decided the outcome itself: leave it alone
        self.assertEqual(t.state, "error")

    def test_a_dead_terminal_stops_the_writes(self):
        class Dead(TTY):
            def write(self, s):
                raise OSError("gone")
        t = TabStatus(stream=Dead(), env=IT)
        t.set("working")
        self.assertIsNone(t.stream)
        t.set("done")  # no exception


class WaitingOnConfirm(unittest.TestCase):
    def test_confirm_and_ask_show_waiting_then_restore(self):
        out = TTY()
        st = TabStatus(stream=out, env=IT)
        ui = Ui(Console(file=io.StringIO(), highlight=False), status=st)
        st.set("working", "turn")
        with mock.patch("elastic_harness.render.sys.stdin.isatty", return_value=True), \
                mock.patch("elastic_harness.render.Prompt.ask", return_value="y"):
            self.assertEqual(ui.confirm("Run shell command", "ls"), "y")
            ui.ask("Kibana URL")
        seq = [(m["status"], m["detail"]) for m in sent(out)]
        self.assertEqual(seq, [("working", "turn"), ("waiting", "Run shell command"), ("working", ""),
                               ("waiting", "Kibana URL"), ("working", "")])

    def test_auto_approved_confirmations_do_not_flicker(self):
        out = TTY()
        ui = Ui(Console(file=io.StringIO()), auto_yes=True, status=TabStatus(stream=out, env=IT))
        ui.confirm("x")
        self.assertEqual(out.getvalue(), "")


class ReplStates(unittest.TestCase):
    def setUp(self):
        credstore.use_backend(FakeKeychain())
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        la.STATE_DIR = d / "state"
        self.cfg = Config(d / "config.json")
        self.cfg.add(Profile(name="alpha", kibana_url="https://alpha.example", es_url="https://es.alpha", api_key="ka", default_agent="eh-demo"))
        self.patches = [mock.patch.object(repl_mod, "Elastic", FakeElastic), mock.patch.object(clusters_mod, "Elastic", FakeElastic),
                        mock.patch.object(project_mod, "CONFIG_DIR", d / "cfg")]
        for p in self.patches:
            p.start()
        self.out = TTY()
        self.ui = Ui(Console(file=io.StringIO(), width=140, highlight=False), auto_yes=True,
                     status=TabStatus(stream=self.out, env=IT))
        self.cwd = d / "proj"
        self.cwd.mkdir()
        os.chdir(self.cwd)
        self.r = repl_mod.Repl(self.cfg, self.ui)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        credstore.use_backend(None)
        credstore._checked = False
        os.chdir(self.tmp.name)
        self.tmp.cleanup()

    def states(self):
        return [(m["status"], m["detail"]) for m in sent(self.out)]

    def test_turn_goes_working_then_done_with_the_answer(self):
        self.r.handle("what is up")
        self.assertEqual(self.states(), [("working", "what is up"), ("done", "hello world")])

    def test_api_error_shows_error(self):
        def boom(*a, **k):
            raise ApiError(500, "model overloaded")
        with mock.patch.object(repl_mod, "run_turn", boom):
            self.r.handle("hi")
        self.assertEqual(self.states()[-1], ("error", "HTTP 500: model overloaded"))

    def test_ctrl_c_goes_back_to_idle(self):
        def stop(*a, **k):
            raise KeyboardInterrupt
        with mock.patch.object(repl_mod, "run_turn", stop):
            self.r.handle("hi")
        self.assertEqual(self.states()[-1], ("idle", "interrupted"))

    def test_slow_command_shows_working_and_quick_ones_do_not(self):
        self.r.handle("/help")
        self.r.handle("/mode local")
        self.assertEqual(self.states(), [])
        self.r.handle("/compare haiku | sonnet 5.5 :: hi")
        self.assertEqual([s for s, _ in self.states()], ["working", "idle"])

    def test_tabstatus_command(self):
        self.r.handle("/tabstatus")
        self.assertIn("tab status: on (active)", self.ui.console.file.getvalue())
        self.r.handle("/tabstatus basic")
        self.r.handle("what is up")
        self.assertEqual(self.states()[-1], ("done", ""))
        self.r.handle("/tabstatus off")
        n = len(sent(self.out))
        self.r.handle("again")
        self.assertEqual(len(sent(self.out)), n)
        self.r.handle("/tabstatus loud")
        self.assertIn("use /tabstatus on, basic or off", self.ui.console.file.getvalue())


if __name__ == "__main__":
    unittest.main()
