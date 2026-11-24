"""The cluster list: add, test, remove, secure. Shared by `eh cluster …` and `/cluster …`."""

from __future__ import annotations

import getpass
import json
import re
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .client import Elastic
from .config import Config, ConfigError, Profile

_NAME = re.compile(r"^[A-Za-z0-9][\w.\-]{0,40}$")


def host(url: str) -> str:
    return urlparse(url).hostname or url


def rows(cfg: Config) -> list[dict]:
    out = []
    for name, p in cfg.profiles.items():
        try:
            r = p.resolved()
            url = r.kibana_url
        except ConfigError:
            url = p.kibana_url
        out.append({"name": name, "active": name == cfg.active, "host": host(url) if url else "?",
                    "space": p.space, "key": Config.key_source(p), "agent": p.default_agent})
    return out


def add_cluster(cfg: Config, name: str, *, creds: str | None = None, import_file: str | None = None,
                kibana_url: str = "", es_url: str = "", space: str = "default",
                default_agent: str = "elastic-ai-agent", make_active: bool | None = None,
                ask: Callable[[str], str] = input, secret: Callable[[str], str] = getpass.getpass) -> tuple[Profile, str, str]:
    """Add (or replace) a cluster after proving the key works. Returns (profile, where the key lives, Kibana version)."""
    if not _NAME.match(name):
        raise ConfigError("cluster names use letters, digits, '.', '_' and '-'")
    prof = Profile(name=name, space=space, default_agent=default_agent, kibana_url=kibana_url, es_url=es_url)
    key = ""
    if creds:
        prof.creds_file = str(Path(creds).expanduser())
        where = "creds file (read in place)"
    else:
        if import_file:
            try:
                data = json.loads(Path(import_file).expanduser().read_text())
            except (OSError, ValueError) as exc:
                raise ConfigError(f"cannot read {import_file}: {exc}") from exc
            prof.kibana_url = data.get("KIBANA_URL", prof.kibana_url)
            prof.es_url = data.get("ES_URL", prof.es_url)
            key = data.get("API_KEY", "")
            if not key:
                raise ConfigError(f"{import_file} has no API_KEY")
        else:
            prof.kibana_url = prof.kibana_url or ask("Kibana URL: ").strip()
            prof.es_url = prof.es_url or ask("Elasticsearch URL: ").strip()
            key = secret("API key (hidden): ").strip()
        if not key or not prof.kibana_url:
            raise ConfigError("a Kibana URL and an API key are required")
    # prove it works before saving anything
    probe = Profile(name=name, kibana_url=prof.kibana_url, es_url=prof.es_url, api_key=key, space=space,
                    creds_file=prof.creds_file)
    version = Elastic(probe).version()
    if key:
        where = cfg.store_key(prof, key)
    cfg.add(prof, make_active=bool(make_active) if make_active is not None else not cfg.active)
    return prof, where, version


def test_cluster(cfg: Config, name: str | None) -> dict:
    """Connect to a cluster without switching to it: version, identity, agent and model counts."""
    prof = cfg.get(name)
    client = Elastic(prof)
    out = {"name": prof.name, "host": host(client.kibana_url), "version": client.version(), "space": client.space}
    try:
        who = client.whoami()
        out["user"] = who.get("username")
        out["realm"] = (who.get("authentication_realm") or {}).get("type")
    except Exception as exc:  # noqa: BLE001 - a diagnostic, report and carry on
        out["user"] = f"(Elasticsearch: {exc})"
    out["agents"] = len(client.agents())
    out["models"] = len(client.models()) if client.es_url else 0
    return out
