"""The interactive session: slash commands, @mentions, agent/local modes."""

from __future__ import annotations

import argparse
import getpass
import re
import shlex
import sys
from datetime import datetime
from pathlib import Path

from rich.table import Table

from . import __version__, agentcode, clusters, transcript
from .agent_chat import history_lines, run_turn, usage_line
from .client import ApiError, Elastic
from .compare import MAX_MODELS, render as render_compare, run_compare
from .config import STATE_DIR, Config, ConfigError
from .guard import expand_mentions
from .ingest import ingest
from .local_agent import LocalSession
from .mentions import expand_elastic_mentions
from .project import context_text, load_commands
from .render import Ui, short
from .tools import Ctx

COMMANDS = {
    "help": "show this list",
    "cluster": "/cluster [name]  list or switch · add · rm · test · secure",
    "space": "/space <id>  switch Kibana space (agents are per space) · /spaces lists them",
    "agents": "list Agent Builder agents",
    "agent": "/agent <id>  switch · push|pull|diff <file>  agents as code",
    "models": "list chat models on the cluster",
    "model": "/model <words>  switch model, e.g. /model sonnet 5.5 · /model default",
    "mode": "/mode agent|local  agent = an Elastic agent answers · local = the harness runs tools on this Mac",
    "compare": "/compare sonnet 5.5 | haiku 4.5 :: prompt   same prompt on 2-4 models (no :: = your last prompt)",
    "retry": "re-run your last prompt on a fresh conversation (--keep to continue this one)",
    "copy": "/copy [code]  last answer (or its last code block) to the clipboard",
    "export": "/export [file.md]  save this conversation as Markdown",
    "open": "/open [agent|case <id>]  open this conversation in Kibana",
    "compact": "summarise older local-mode history to save tokens",
    "new": "start a fresh conversation",
    "resume": "/resume [n|id]  list or reopen a conversation",
    "ingest": "/ingest <path> [--index name] [--tool] [--agent id]  load local files into Elastic",
    "tools": "list the tools the current mode can use",
    "usage": "token usage this session",
    "doctor": "check connection, agents and models",
    "debug": "toggle raw event display",
    "clear": "clear the screen",
    "exit": "quit",
}
_PREFERRED = ["5.5-sonnet", "4.6-sonnet", "sonnet", "haiku"]
_PATH_CMDS = re.compile(r"^/(ingest|export|agent (?:push|pull|diff))\s+(\S*)$")


def pick_default_model(models: list[str]) -> str:
    for pref in _PREFERRED:
        for m in models:
            if pref in m:
                return m
    return models[0] if models else ""


def match_models(models: list[str], query: str) -> list[str]:
    q = query.strip().lower()
    if q in models:
        return [q]
    words = q.replace("-", " ").split()
    return [m for m in models if all(w in m.lower().replace("-", " ") for w in words)]


def _flags(argv: list[str], build) -> argparse.Namespace:
    ap = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    ap.error = lambda msg: (_ for _ in ()).throw(ValueError(msg))  # never exit the session on a bad flag
    build(ap)
    return ap.parse_args(argv)


class Repl:
    def __init__(self, config: Config, ui: Ui, profile: str | None = None, agent: str | None = None,
                 model: str | None = None, mode: str | None = None, space: str | None = None):
        self.config, self.ui = config, ui
        self.cwd = Path.cwd()
        self.always: set[str] = set()
        self.agents_cache: list[dict] | None = None
        self.models_cache: list[str] | None = None
        self.conversation_id: str | None = None
        self.local: LocalSession | None = None
        self.tokens = [0, 0]  # agent-mode in / out
        self.last_prompt: str | None = None
        self.connect(profile)
        self.reload_project()
        if space:
            self.switch_space(space)
        if agent:
            ids = [a["id"] for a in self.agents()]
            if agent not in ids:
                raise ConfigError(f"no agent {agent!r} in space {self.client.space} (have: {', '.join(ids)})")
            self.agent_id = self.preferred_agent = agent
        if model and model.lower() not in ("default", "cluster", "none"):
            hits = match_models(self.models(), model)
            if len(hits) != 1:
                raise ConfigError(f"model {model!r} matches {len(hits)} models" + (": " + ", ".join(hits[:6]) if hits else " (see: eh models)"))
            self.model = hits[0]
        elif model:
            self.model = ""
        if mode:
            self.mode = mode

    # -- connection / state -----------------------------------------------------------------
    def connect(self, name: str | None = None) -> None:
        self.raw = self.config.get(name)
        self.client = Elastic(self.raw)
        self.agent_id = self.preferred_agent = self.raw.default_agent or "elastic-ai-agent"
        self.model = self.raw.default_model
        self.mode = "agent"  # a mode never carries over from a previous session
        self.agents_cache = self.models_cache = None
        self.conversation_id, self.local = None, None
        self.always = set()

    def reload_project(self) -> None:
        self.context, self.context_files = context_text(self.cwd)
        self.cmds, self.cmd_warnings = load_commands(self.cwd, reserved=set(COMMANDS) | {"profile", "profiles", "quit", "q"})

    def agents(self) -> list[dict]:
        if self.agents_cache is None:
            self.agents_cache = self.client.agents()
        return self.agents_cache

    def models(self) -> list[str]:
        if self.models_cache is None:
            try:
                self.models_cache = self.client.models()
            except ApiError as exc:
                self.ui.warn(f"could not list models: {exc}")
                self.models_cache = []
        return self.models_cache

    def _resolve_model(self, query: str) -> str:
        if query.lower() in ("default", "cluster", "none", ""):
            return ""
        hits = match_models(self.models(), query)
        if len(hits) == 1:
            return hits[0]
        self.ui.error(f"no model matches {query!r} (try /models)" if not hits else
                      "several match, be more specific:\n  " + "\n  ".join(hits[:12]))
        return ""

    def _persist(self, **changes) -> None:
        for k, v in changes.items():
            setattr(self.raw, k, v)
        try:
            self.config.save()
        except OSError as exc:
            self.ui.warn(f"could not save defaults: {exc}")

    def kibana_link(self, path: str) -> str:
        prefix = "" if self.client.space in ("", "default") else f"/s/{self.client.space}"
        return f"{self.client.kibana_url}{prefix}/app{path}"

    # -- sending ----------------------------------------------------------------------------
    def send(self, text: str, record: bool = True) -> None:
        if record:
            self.last_prompt = text
        text, enotes = expand_elastic_mentions(text, self.client)
        text, notes = expand_mentions(text, self.cwd)
        for n in enotes:
            bad = n["status"] != "attached"
            self.ui.line(f"[{'yellow' if bad else 'dim'}]@ {n['ref']} · {n['status']}[/{'yellow' if bad else 'dim'}]")
        for n in notes:
            colour = "yellow" if n["status"].startswith(("refused", "skipped")) else "dim"
            self.ui.line(f"[{colour}]@ {n['path']} · {n['status']}" + (f" · {n['chars']:,} chars" if n["chars"] else "") + f"[/{colour}]")
        try:
            if self.mode == "local":
                self._send_local(text)
            else:
                self._send_agent(text)
        except KeyboardInterrupt:
            self.ui.flush()
            self.ui.warn("interrupted")
        except ApiError as exc:
            self.ui.error(str(exc))

    def _send_agent(self, text: str) -> None:
        if self.conversation_id is None and self.context:  # EH.md goes in once, at the start of a conversation
            text = f"<project_context>\n{self.context}\n</project_context>\n\n{text}"
            self.ui.info(f"EH.md attached ({len(self.context):,} chars) · {', '.join(p.name for p in self.context_files)}")
        res = run_turn(self.client, self.ui, agent_id=self.agent_id, text=text,
                       conversation_id=self.conversation_id, model=self.model or None)
        self.conversation_id = res["conversation_id"]
        if res["usage"]:
            self.tokens[0] += res["usage"].get("input_tokens", 0)
            self.tokens[1] += res["usage"].get("output_tokens", 0)
            self.ui.usage(usage_line(res["usage"]))

    def _ensure_local(self) -> bool:
        if not self.model:
            self.model = pick_default_model(self.models())
            if not self.model:
                self.ui.error("no chat models found on this cluster")
                return False
            self.ui.info(f"local mode needs a model; using {self.model} (change with /model)")
        if self.local is None:
            self.local = LocalSession(Ctx(self.client, self.ui, self.cwd, self.model, self.always), self.agents(),
                                      extra_system=self.context)
        self.local.ctx.model = self.model
        return True

    def _send_local(self, text: str) -> None:
        if not self._ensure_local():
            return
        self.local.turn(text)
        self.ui.usage(f"{self.local.tokens_in:,} in / {self.local.tokens_out:,} out tokens this session · {self.model}")

    # -- commands ---------------------------------------------------------------------------
    def handle(self, line: str) -> bool:
        """Run one line. Returns False to quit."""
        line = line.strip()
        if not line:
            return True
        if line in ("exit", "quit"):
            return False
        if not line.startswith("/"):
            self.send(line)
            return True
        name, _, arg = line[1:].partition(" ")
        if name in ("quit", "q"):
            return False
        fn = getattr(self, f"cmd_{name}", None)
        try:
            if fn is not None:
                fn(arg.strip())
            elif name in self.cmds:
                self.run_custom(name, arg.strip())
            else:
                self.ui.error(f"unknown command /{name} (try /help)")
        except (ApiError, ConfigError, OSError, ValueError, PermissionError, SystemExit) as exc:
            self.ui.error(str(exc) or "bad arguments")
        return True

    def run_custom(self, name: str, arg: str) -> None:
        c = self.cmds[name]
        saved = (self.mode, self.model, self.agent_id, self.conversation_id)
        if c.mode:
            self.mode = c.mode
        if c.model:
            m = self._resolve_model(c.model)
            if not m:
                return
            self.model = m
        if c.agent:
            self.agent_id, self.conversation_id = c.agent, None
        self.ui.info(f"/{name}" + (f" · {c.description}" if c.description else ""))
        try:
            self.send(c.expand(arg))
        finally:  # a command's mode/model/agent apply to that one run only
            self.mode, self.model = saved[0], saved[1]
            if c.agent:
                self.agent_id, self.conversation_id = saved[2], saved[3]

    def cmd_help(self, arg: str) -> None:
        t = Table(show_header=False, box=None, padding=(0, 2))
        for k, v in COMMANDS.items():
            t.add_row(f"[cyan]/{k}[/cyan]", v)
        for k, c in sorted(self.cmds.items()):
            t.add_row(f"[green]/{k}[/green]", (c.description or "custom command") + f" [dim]({c.source.parent.parent.name if c.source else ''})[/dim]")
        self.ui.console.print(t)
        self.ui.console.print("[dim]@path attaches a file or folder · @alert:<id> @case:<id> @index:<pattern> pull Elastic objects in · "
                              "Alt+Enter = newline · Ctrl+C stops a run[/dim]")
        for w in self.cmd_warnings:
            self.ui.warn(w)
        if self.context_files:
            self.ui.info("project instructions: " + ", ".join(str(p) for p in self.context_files))

    # -- clusters ---------------------------------------------------------------------------
    def cmd_cluster(self, arg: str) -> None:
        argv = shlex.split(arg) if arg else []
        sub = argv[0] if argv else ""
        if not argv:
            return self._cluster_table()
        if sub == "add":
            def build(ap):
                ap.add_argument("name")
                ap.add_argument("--creds")
                ap.add_argument("--import", dest="import_file")
                ap.add_argument("--space", default="default")
                ap.add_argument("--kibana-url", default="")
                ap.add_argument("--es-url", default="")
            a = _flags(argv[1:], build)
            prof, where, version = clusters.add_cluster(self.config, a.name, creds=a.creds, import_file=a.import_file,
                                                        kibana_url=a.kibana_url, es_url=a.es_url, space=a.space,
                                                        ask=self.ui.ask, secret=getpass.getpass)
            self.ui.ok(f"cluster {prof.name} added · key: {where} · Kibana {version} · /cluster {prof.name} to switch")
        elif sub in ("rm", "remove", "delete"):
            if len(argv) != 2:
                return self.ui.info("usage: /cluster rm <name>")
            prof = self.config.get(argv[1])
            if prof.name == self.raw.name:
                return self.ui.error("that is the cluster you are on: switch to another first")
            if self.ui.confirm(f"Remove cluster {prof.name} and its stored key?", Config.key_source(prof), allow_always=False) == "y":
                self.config.remove(prof.name)
                self.ui.ok(f"removed {prof.name}")
        elif sub == "test":
            info = clusters.test_cluster(self.config, argv[1] if len(argv) > 1 else self.raw.name)
            self.ui.ok(f"{info['name']} · {info['host']} · Kibana {info['version']} · space {info['space']} · "
                       f"user {info.get('user')} · {info['agents']} agents · {info['models']} models")
        elif sub == "secure":
            if len(argv) != 2:
                return self.ui.info("usage: /cluster secure <name>  (moves a key from the config file into the Keychain)")
            self.ui.ok(f"{argv[1]}: key now in {self.config.secure(argv[1])}")
        else:
            self.switch_cluster(arg)

    def _cluster_table(self) -> None:
        t = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        for col in ("", "cluster", "host", "space", "key", "default agent"):
            t.add_column(col)
        for r in clusters.rows(self.config):
            here = r["name"] == self.raw.name
            t.add_row("[green]●[/green]" if here else "", f"[bold]{r['name']}[/bold]" if here else r["name"], r["host"],
                      r["space"], r["key"], r["agent"])
        self.ui.console.print(t)
        self.ui.info("/cluster <name> switches · /cluster add <name> · rm · test · secure")

    def switch_cluster(self, name: str) -> None:
        had_session = bool(self.conversation_id or (self.local and self.local.messages))
        self.connect(name)
        self.config.active = self.raw.name
        self.config.save()
        self.ui.line(f"[bold green]→ cluster {self.raw.name}[/bold green] [dim]{clusters.host(self.client.kibana_url)} · "
                     f"Kibana {self.client.version()} · space {self.client.space} · agent {self.agent_id}[/dim]")
        if had_session:
            self.ui.info("started a fresh conversation (conversations belong to one cluster)")
        self.reload_project()

    cmd_profile = cmd_cluster  # older name
    cmd_profiles = cmd_cluster

    # -- spaces -----------------------------------------------------------------------------
    def switch_space(self, space: str) -> None:
        previous = self.client.space
        try:
            known = {s["id"] for s in self.client.spaces()}
            if known and space not in known:
                raise ConfigError(f"no space {space!r} (have: {', '.join(sorted(known))})")
        except ApiError:
            pass  # this key may not be allowed to list spaces: fall back to trying the space itself
        self.client.space = space
        try:
            self.agents_cache = self.client.agents()
        except ApiError as exc:
            self.client.space = previous
            raise ConfigError(f"cannot use space {space!r}: {exc}") from None
        self.conversation_id, self.local = None, None
        ids = [a["id"] for a in self.agents_cache]
        if self.preferred_agent in ids:  # your chosen agent comes back as soon as a space has it
            self.agent_id = self.preferred_agent
        elif self.agent_id not in ids:
            fallback = "elastic-ai-agent" if "elastic-ai-agent" in ids else (ids[0] if ids else "")
            self.ui.info(f"agent {self.preferred_agent!r} is not in space {space}; using {fallback!r} there")
            self.agent_id = fallback

    def cmd_space(self, arg: str) -> None:
        if not arg:
            return self.cmd_spaces("")
        self.switch_space(arg)
        self.ui.ok(f"space: {self.client.space} · {len(self.agents_cache or [])} agents · agent {self.agent_id} · fresh conversation")

    def cmd_spaces(self, arg: str) -> None:
        try:
            spaces = self.client.spaces()
        except ApiError as exc:
            return self.ui.warn(f"cannot list spaces with this key ({exc.message[:80]}); /space <id> still works if you know it")
        for s in spaces:
            self.ui.console.print(f"{'[green]●[/green]' if s['id'] == self.client.space else ' '} {s['id']}  [dim]{s.get('name', '')}[/dim]")

    # -- agents, models, mode ----------------------------------------------------------------
    def cmd_agents(self, arg: str) -> None:
        self.agents_cache = None
        t = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        for col in ("", "id", "name", "access", "description"):
            t.add_column(col)
        for a in self.agents():
            mark = "●" if a["id"] == self.agent_id else ""
            t.add_row(mark, a["id"], a.get("name", ""), (a.get("access_control") or {}).get("access_mode", ""), short(a.get("description", ""), 70))
        self.ui.console.print(t)

    def cmd_agent(self, arg: str) -> None:
        if not arg:
            return self.cmd_agents("")
        argv = shlex.split(arg)
        if argv[0] in ("push", "pull", "diff"):
            return self._agent_code(argv)
        ids = [a["id"] for a in self.agents()]
        hits = [i for i in ids if i == arg] or [i for i in ids if arg.lower() in i.lower()]
        if len(hits) != 1:
            self.ui.error(f"{'no agent' if not hits else 'several agents'} match {arg!r}: {', '.join(hits) or ', '.join(ids)}")
            return
        self.agent_id, self.conversation_id = hits[0], None
        self.preferred_agent = self.agent_id
        self._persist(default_agent=self.agent_id)
        self.ui.ok(f"agent: {self.agent_id} (new conversation)")
        if self.mode == "local":
            self.ui.info("in local mode agents are reached through the ask_agent tool; /mode agent to chat with one directly")

    def _agent_code(self, argv: list[str]) -> None:
        sub = argv[0]

        def build(ap):
            ap.add_argument("target", nargs="?")
            ap.add_argument("out", nargs="?", default="agents")
            ap.add_argument("--all", action="store_true")
            ap.add_argument("--force", action="store_true")
            ap.add_argument("--dry-run", action="store_true")
            ap.add_argument("--allow-risky", action="store_true")
        a = _flags(argv[1:], build)
        if sub in ("push", "diff"):
            if not a.target:
                return self.ui.info(f"usage: /agent {sub} <file.md>")
            res = agentcode.push(self.client, a.target, confirm=lambda t, d: self.ui.confirm(t, d, allow_always=False) == "y",
                                 say=lambda m: self.ui.console.print(m, markup=False, highlight=False),
                                 allow_risky=a.allow_risky, dry_run=a.dry_run or sub == "diff")
            self.agents_cache = None
            self.ui.ok(f"{res['id']}: {res['action']}")
        else:
            if a.all:
                paths = agentcode.pull_all(self.client, a.target or "agents", force=a.force)
                self.ui.ok(f"pulled {len(paths)} agents into {Path(a.target or 'agents')}/")
            elif a.target:
                self.ui.ok(f"wrote {agentcode.pull(self.client, a.target, a.out, force=a.force)}")
            else:
                self.ui.info("usage: /agent pull <id> [dir] [--force]  or  /agent pull --all [dir]")

    def cmd_models(self, arg: str) -> None:
        self.models_cache = None
        for m in self.models():
            self.ui.console.print(f"{'[green]●[/green]' if m == self.model else ' '} {m}")
        self.ui.info("agent mode with no model picked uses the cluster default · /model <words> to switch")

    def cmd_model(self, arg: str) -> None:
        if not arg:
            return self.ui.info(f"model: {self.model or 'cluster default'}")
        if arg.lower() in ("default", "cluster", "none"):
            self.model = ""
        else:
            m = self._resolve_model(arg)
            if not m:
                return
            self.model = m
        if self.local:
            self.local.ctx.model = self.model
        self._persist(default_model=self.model)
        self.ui.ok(f"model: {self.model or 'cluster default'}")

    def cmd_mode(self, arg: str) -> None:
        if arg not in ("agent", "local"):
            return self.ui.info(f"mode: {self.mode} (use /mode agent or /mode local)")
        self.mode = arg  # session-only: a new session always starts in agent mode
        self.ui.ok(f"mode: {arg}" + (" · the harness runs tools on this Mac" if arg == "local" else " · an Agent Builder agent answers"))

    # -- compare, retry, copy, export, open --------------------------------------------------
    def cmd_compare(self, arg: str) -> None:
        left, sep, right = arg.partition("::")
        queries = [q.strip() for q in left.split("|") if q.strip()]
        if len(queries) < 2:
            return self.ui.info("usage: /compare sonnet 5.5 | haiku 4.5 [| …] :: your prompt   (no :: = your last prompt)")
        if len(queries) > MAX_MODELS:
            return self.ui.error(f"at most {MAX_MODELS} models")
        models = []
        for q in queries:
            hits = match_models(self.models(), q)
            if len(hits) != 1:
                return self.ui.error(f"{q!r} matches {len(hits)} models" + (": " + ", ".join(hits[:5]) if hits else ""))
            models.append(hits[0])
        prompt = right.strip() if sep and right.strip() else self.last_prompt
        if not prompt:
            return self.ui.error("nothing to compare yet: add ':: your prompt'")
        self.last_prompt = prompt
        prompt, enotes = expand_elastic_mentions(prompt, self.client)
        prompt, _ = expand_mentions(prompt, self.cwd)
        what = (f"agent {self.agent_id} (tools run for real)" if self.mode == "agent" else "plain model answers, no tools (local mode)")
        with self.ui.console.status(f"running on {len(models)} models · {what}…"):
            results = run_compare(self.client, models, prompt, mode=self.mode, agent_id=self.agent_id)
        self.ui.console.print(render_compare(results))
        if self.mode == "agent":
            self.ui.info("the comparison chats were deleted from Kibana")

    def cmd_retry(self, arg: str) -> None:
        if not self.last_prompt:
            return self.ui.error("nothing to retry yet")
        if "--keep" not in arg:
            self.conversation_id, self.local = None, None
            self.ui.info("retrying on a fresh conversation")
        self.send(self.last_prompt, record=False)

    def cmd_copy(self, arg: str) -> None:
        text = self.ui.last_answer
        if not text.strip():
            return self.ui.error("no answer to copy yet")
        if arg == "code":
            text = transcript.last_code_block(text) or ""
            if not text:
                return self.ui.error("the last answer has no code block")
        how = transcript.copy_to_clipboard(text)
        self.ui.ok(f"copied {len(text):,} characters ({how})")

    def cmd_export(self, arg: str) -> None:
        header = {"cluster": self.raw.name, "space": self.client.space, "agent": self.agent_id if self.mode == "agent" else "(local mode)",
                  "model": self.model or "cluster default", "mode": self.mode, "exported": datetime.now().strftime("%Y-%m-%d %H:%M")}
        if self.mode == "local":
            if not self.local or not self.local.messages:
                return self.ui.error("nothing to export yet")
            md = transcript.local_markdown(self.local.messages, header)
        else:
            if not self.conversation_id:
                return self.ui.error("nothing to export yet")
            md = transcript.agent_markdown(self.client.conversation(self.conversation_id), header)
        path = Path(arg).expanduser() if arg else transcript.default_export_path(self.cwd)
        if path.exists():
            return self.ui.error(f"{path} already exists")
        path.write_text(md, encoding="utf-8")
        self.ui.ok(f"saved {path} ({len(md):,} chars)")

    def cmd_open(self, arg: str) -> None:
        argv = arg.split()
        if argv[:1] == ["case"] and len(argv) == 2:
            url = self.kibana_link(f"/security/cases/{argv[1]}")
        elif self.conversation_id and argv[:1] != ["agent"]:
            url = self.kibana_link(f"/agent_builder/agents/{self.agent_id}/conversations/{self.conversation_id}")
        else:
            url = self.kibana_link(f"/agent_builder/agents/{self.agent_id}")
        self.ui.console.print(url, markup=False, highlight=False, soft_wrap=True)
        if not transcript.open_url(url):
            self.ui.info("could not launch a browser; copy the link above")

    # -- conversations -----------------------------------------------------------------------
    def cmd_new(self, arg: str) -> None:
        self.conversation_id, self.local = None, None
        self.reload_project()
        self.ui.ok("new conversation")

    def cmd_compact(self, arg: str) -> None:
        if self.mode != "local" or not self.local or not self.local.messages:
            return self.ui.info("compaction applies to local mode, once it has a conversation")
        self._ensure_local()
        with self.ui.console.status("summarising earlier turns…"):
            before, after = self.local.compact(force=True)
        self.ui.ok(f"compacted ~{before // 1000}k → ~{after // 1000}k tokens (estimated)")

    def cmd_resume(self, arg: str) -> None:
        if self.mode == "local":
            saved = LocalSession.list_saved()
            if not arg:
                for i, s in enumerate(saved, 1):
                    self.ui.console.print(f"[cyan]{i:>2}[/cyan] {s['saved'][:16]}  {s['first']}")
                return self.ui.info("/resume <n> to reopen") if saved else self.ui.info("no saved local sessions")
            sid = saved[int(arg) - 1]["id"] if arg.isdigit() and int(arg) <= len(saved) else arg
            self.local = LocalSession(Ctx(self.client, self.ui, self.cwd, self.model, self.always), self.agents(), sid,
                                      extra_system=self.context)
            self.ui.ok(f"resumed {sid} ({self.local.load(sid)} messages)")
            return
        convs = sorted(self.client.conversations(), key=lambda c: c.get("updated_at", ""), reverse=True)[:15]
        if not arg:
            for i, c in enumerate(convs, 1):
                self.ui.console.print(f"[cyan]{i:>2}[/cyan] {c.get('updated_at', '')[:16]}  [dim]{c.get('agent_id', '')}[/dim]  {short(c.get('title', ''), 60)}")
            return self.ui.info("/resume <n> to reopen") if convs else self.ui.info("no conversations yet")
        conv_id = convs[int(arg) - 1]["id"] if arg.isdigit() and int(arg) <= len(convs) else arg
        conv = self.client.conversation(conv_id)
        self.conversation_id = conv_id
        self.agent_id = conv.get("agent_id") or self.agent_id
        self.ui.ok(f"resumed '{short(conv.get('title', ''), 60)}' with agent {self.agent_id}")
        for role, text in history_lines(conv):
            self.ui.line(f"[bold]{role}[/bold] [dim]{short(text, 160)}[/dim]")

    def cmd_ingest(self, arg: str) -> None:
        argv = shlex.split(arg)
        if not argv:
            return self.ui.info("usage: /ingest <path> [--index name] [--tool] [--agent id]")
        path, slug, tool, agent, i = argv[0], None, False, None, 1
        while i < len(argv):
            if argv[i] == "--index" and i + 1 < len(argv):
                slug, i = argv[i + 1], i + 2
            elif argv[i] == "--agent" and i + 1 < len(argv):
                agent, i = argv[i + 1], i + 2
            elif argv[i] == "--tool":
                tool, i = True, i + 1
            else:
                return self.ui.error(f"unexpected {argv[i]!r}")
        self.ui.info(f"target: cluster {self.raw.name} · space {self.client.space}")
        res = ingest(self.client, path, slug, tool, agent, say=self.ui.info)
        self.ui.ok("ingested {files} files → {chunks} chunks into {index} ({skipped} skipped, {failed} failed)".format(**res))
        if res.get("tool"):
            self.ui.ok(f"tool {res['tool']} created" + (f" and attached to {res['attached_to_agent']}" if res.get("attached_to_agent") else ""))
        elif not tool:
            self.ui.info("add --tool (and --agent <id>) so an Agent Builder agent can search it")

    def cmd_tools(self, arg: str) -> None:
        if self.mode == "local":
            from .tools import build_tools
            for t in build_tools():
                self.ui.console.print(f"[cyan]{t.name}[/cyan]  [dim]{t.description}[/dim]")
            return
        cfg = self.client.agent(self.agent_id).get("configuration", {})
        ids = [t for g in cfg.get("tools", []) for t in g.get("tool_ids", [])]
        self.ui.info(f"{self.agent_id}: {len(ids)} tools" + (" + Elastic built-ins" if cfg.get("enable_elastic_capabilities", True) else ""))
        for t in ids:
            self.ui.console.print(f"  {t}")

    def cmd_usage(self, arg: str) -> None:
        self.ui.info(f"agent mode: {self.tokens[0]:,} in / {self.tokens[1]:,} out" +
                     (f" · local mode: {self.local.tokens_in:,} in / {self.local.tokens_out:,} out" if self.local else ""))

    def cmd_doctor(self, arg: str) -> None:
        self.ui.ok(f"cluster {self.raw.name} · Kibana {self.client.version()} · space {self.client.space} · {clusters.host(self.client.kibana_url)}")
        try:
            who = self.client.whoami()
            self.ui.ok(f"authenticated as {who.get('username')} (realm {(who.get('authentication_realm') or {}).get('type')}) · key in {Config.key_source(self.raw)}")
        except ApiError as exc:
            self.ui.warn(f"Elasticsearch: {exc}")
        self.ui.ok(f"{len(self.agents())} agents · {len(self.models())} chat models")
        self.ui.info(f"agent {self.agent_id} · model {self.model or 'cluster default'} · mode {self.mode}")

    def cmd_debug(self, arg: str) -> None:
        self.ui.debug = not self.ui.debug
        self.ui.info(f"debug {'on' if self.ui.debug else 'off'}")

    def cmd_clear(self, arg: str) -> None:
        self.ui.console.clear()

    def cmd_exit(self, arg: str) -> None:  # handled in handle(); kept so /help is honest
        pass

    # -- interactive loop -------------------------------------------------------------------
    def banner(self) -> None:
        self.ui.console.print(f"[bold]Elastic Harness[/bold] {__version__} · cluster [cyan]{self.raw.name}[/cyan] · "
                              f"Kibana {self.client.version()} · space {self.client.space}")
        extras = []
        if self.context_files:
            extras.append("EH.md")
        if self.cmds:
            extras.append(f"{len(self.cmds)} custom commands")
        self.ui.console.print(f"[dim]agent {self.agent_id} · model {self.model or 'cluster default'} · mode {self.mode}"
                              + (f" · {', '.join(extras)}" if extras else "") + " · /help for commands[/dim]")

    def run(self) -> None:
        if not sys.stdin.isatty():
            for line in sys.stdin:
                if not self.handle(line):
                    break
            return
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion, PathCompleter
        from prompt_toolkit.document import Document
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings

        repl = self

        class Comp(Completer):
            path = PathCompleter(expanduser=True)

            def _paths(self, token, event):
                for c in self.path.get_completions(Document(token), event):
                    yield Completion(c.text, c.start_position, display=c.display)

            def get_completions(self, document, event):
                text = document.text_before_cursor
                pm = _PATH_CMDS.match(text)
                if pm:
                    yield from self._paths(pm.group(2), event)
                    return
                m = re.search(r"@(\S*)$", text)
                if m:
                    tok = m.group(1)
                    for kind in ("alert:", "case:", "index:"):
                        if kind.startswith(tok) and tok != kind:
                            yield Completion(kind, -len(tok), display=f"@{kind}", display_meta="Elastic object")
                    if ":" not in tok:
                        yield from self._paths(tok, event)
                    return
                if not text.startswith("/"):
                    return
                cmd, sp, rest = text[1:].partition(" ")
                if not sp:
                    for k in list(COMMANDS) + list(repl.cmds):
                        if k.startswith(cmd):
                            meta = COMMANDS.get(k) or repl.cmds[k].description or "custom command"
                            yield Completion(k, -len(cmd), display=f"/{k}", display_meta=meta[:50])
                    return
                pool = {"agent": [a["id"] for a in repl.agents_cache or []] + ["push", "pull", "diff"],
                        "model": repl.models_cache or [], "mode": ["agent", "local"],
                        "cluster": list(repl.config.profiles) + ["add", "rm", "test", "secure"],
                        "profile": list(repl.config.profiles)}.get(cmd, [])
                for v in pool:
                    if rest.lower() in v.lower():
                        yield Completion(v, -len(rest))

        kb = KeyBindings()

        @kb.add("escape", "enter")
        def _newline(event):
            event.current_buffer.insert_text("\n")

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        session = PromptSession(history=FileHistory(str(STATE_DIR / "history")), completer=Comp(), key_bindings=kb,
                                bottom_toolbar=lambda: (f" {repl.raw.name} · {repl.client.space} · agent {repl.agent_id} · "
                                                        f"model {repl.model or 'cluster default'} · mode {repl.mode} "),
                                complete_while_typing=True)
        self.banner()
        try:  # warm the completion caches so tab completion has data
            self.agents(), self.models()
        except ApiError:
            pass
        for w in self.cmd_warnings:
            self.ui.warn(w)
        while True:
            try:
                label = "local" if self.mode == "local" else self.agent_id
                line = session.prompt([("bold ansicyan", f"{label} › ")])
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            if not self.handle(line):
                break
