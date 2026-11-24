"""eh: command-line entry point."""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from . import __version__
from .client import ApiError, Elastic
from .config import Config, ConfigError, Profile
from .ingest import ingest
from .render import Ui
from .repl import Repl


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eh", description="Elastic Harness: a terminal harness for Elastic Agent Builder")
    p.add_argument("--version", action="version", version=f"elastic-harness {__version__}")
    p.add_argument("--profile", help="profile to use (default: the active one)")
    p.add_argument("--agent", help="Agent Builder agent id")
    p.add_argument("--model", help="model words, e.g. 'sonnet 5.5' (see: eh models)")
    p.add_argument("--mode", choices=["agent", "local"], help="agent = an Elastic agent answers; local = tools run on this Mac")
    p.add_argument("-p", "--print", dest="prompt", metavar="PROMPT", help="run one prompt, print the answer, exit")
    p.add_argument("--yes", action="store_true", help="auto-approve tool confirmations (one-shot runs)")
    p.add_argument("--debug", action="store_true", help="show raw stream events")
    sub = p.add_subparsers(dest="cmd")

    pr = sub.add_parser("profile", help="manage cluster profiles")
    ps = pr.add_subparsers(dest="action", required=True)
    add = ps.add_parser("add", help="add or replace a profile")
    add.add_argument("name")
    add.add_argument("--creds", help="JSON file with KIBANA_URL, ES_URL, API_KEY (read in place, never copied)")
    add.add_argument("--kibana-url")
    add.add_argument("--es-url")
    add.add_argument("--api-key-env", help="read the API key from this environment variable")
    add.add_argument("--space", default="default")
    add.add_argument("--default-agent", default="elastic-ai-agent")
    ps.add_parser("list", help="list profiles")
    use = ps.add_parser("use", help="make a profile the default")
    use.add_argument("name")
    rm = ps.add_parser("rm", help="remove a profile")
    rm.add_argument("name")

    sub.add_parser("agents", help="list agents")
    sub.add_parser("models", help="list chat models")
    sub.add_parser("doctor", help="check connection, agents and models")
    ing = sub.add_parser("ingest", help="load local files into an eh-local-* index")
    ing.add_argument("path")
    ing.add_argument("--index")
    ing.add_argument("--tool", action="store_true", help="also create an index_search tool for it")
    ing.add_argument("--agent", dest="attach_agent", help="attach that tool to this agent")
    return p


def _profile_cmd(cfg: Config, a: argparse.Namespace, ui: Ui) -> int:
    if a.action == "list":
        for n, p in cfg.profiles.items():
            ui.console.print(f"{'[green]●[/green]' if n == cfg.active else ' '} {n}  [dim]{p.space} · {p.creds_file or p.kibana_url}[/dim]")
        return 0
    if a.action == "use":
        cfg.get(a.name)
        cfg.active = a.name
        cfg.save()
        ui.ok(f"active profile: {a.name}")
        return 0
    if a.action == "rm":
        cfg.profiles.pop(a.name, None)
        cfg.active = next(iter(cfg.profiles), "") if cfg.active == a.name else cfg.active
        cfg.save()
        ui.ok(f"removed {a.name}")
        return 0
    prof = Profile(name=a.name, space=a.space, default_agent=a.default_agent, creds_file=os.path.expanduser(a.creds or ""),
                   kibana_url=a.kibana_url or "", es_url=a.es_url or "")
    if not a.creds:
        prof.kibana_url = prof.kibana_url or ui.ask("Kibana URL")
        prof.es_url = prof.es_url or ui.ask("Elasticsearch URL")
        prof.api_key = os.environ.get(a.api_key_env or "", "") or getpass.getpass("API key (hidden): ")
    client = Elastic(prof)  # resolves creds; raises ConfigError/ValueError on problems
    version = client.version()
    cfg.add(prof)
    ui.ok(f"profile {a.name} saved ({'creds read in place from ' + a.creds if a.creds else 'key stored in ' + str(cfg.path) + ', mode 600'}) · Kibana {version}")
    return 0


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    ui = Ui(debug=a.debug, auto_yes=a.yes)
    try:
        cfg = Config()
        if a.cmd == "profile":
            return _profile_cmd(cfg, a, ui)
        repl = Repl(cfg, ui, profile=a.profile, agent=a.agent, model=a.model, mode=a.mode)
        if a.cmd == "agents":
            repl.cmd_agents("")
        elif a.cmd == "models":
            repl.cmd_models("")
        elif a.cmd == "doctor":
            repl.cmd_doctor("")
        elif a.cmd == "ingest":
            res = ingest(repl.client, a.path, a.index, a.tool, a.attach_agent, say=ui.info)
            ui.ok("ingested {files} files → {chunks} chunks into {index} ({skipped} skipped, {failed} failed)".format(**res))
            if res.get("tool"):
                ui.ok(f"tool {res['tool']}" + (f" attached to {res['attached_to_agent']}" if res.get("attached_to_agent") else ""))
        elif a.prompt is not None:
            text = a.prompt if sys.stdin.isatty() else (a.prompt + "\n\n" + sys.stdin.read()).strip()
            repl.send(text)
        else:
            repl.run()
        return 0
    except (ConfigError, ValueError) as exc:
        ui.error(str(exc))
        return 2
    except (ApiError, OSError, PermissionError) as exc:
        ui.error(str(exc))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
