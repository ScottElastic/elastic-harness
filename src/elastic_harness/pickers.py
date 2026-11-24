"""Pickers for `@alert:` and `@case:`: the newest open alerts and cases in the current space, as completion rows.

Typing `@alert:` in the prompt lists the 20 newest open alerts; `@case:` lists the 20 most recently created cases that
are not closed. The rows come from the same Kibana calls the mentions already use, with the session's own key, so you
see what that key can read in that space.

Listing is a network call (about 0.4 s), so it never runs on the typing thread: the completer is threaded, results are
cached for a short while, a second caller waits for a fetch already in flight instead of repeating it, and typing
`@al…` starts the fetch before the `:` is reached.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .client import DEFAULT_CASE_OWNER, ApiError, Elastic, case_owner
from .mentions import _get

KINDS = ("alert", "case")
LIMIT = 20
TTL = 45.0        # seconds a list is reused
ERROR_TTL = 5.0   # a failed listing is retried sooner
MAX_OTHER_SPACES = 6


@dataclass(frozen=True)
class Row:
    id: str
    title: str
    severity: str
    where: str     # alerts: the host; cases: the tags
    when: str
    extra: str = ""
    haystack: str = ""  # lower-case text the word typed after the colon is matched against


@dataclass(frozen=True)
class Listing:
    rows: tuple[Row, ...]
    note: str = ""  # why there are no rows (empty space, or the error), or a hint about other spaces
    ok: bool = True


def when(iso: str, now: datetime | None = None, tz: timezone | None = None) -> str:
    """'14:02' for today, 'Nov 23 15:55' otherwise, in local time (or `tz`)."""
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(tz)
    except ValueError:
        return ""
    today = (now or datetime.now(t.tzinfo)).astimezone(t.tzinfo).date()
    return t.strftime("%H:%M") if t.date() == today else f"{t:%b} {t.day} {t:%H:%M}"


def alert_row(hit: dict, tz: timezone | None = None) -> Row:
    src = hit.get("_source") or {}
    title = str(_get(src, "kibana.alert.rule.name") or "(unnamed rule)")
    sev = str(_get(src, "kibana.alert.severity") or "")
    host = str(_get(src, "host.name") or "")
    user = str(_get(src, "user.name") or "")
    return Row(id=hit.get("_id", ""), title=title, severity=sev, where=host, when=when(_get(src, "@timestamp") or "", tz=tz),
               extra=user, haystack=" ".join((title, sev, host, user, hit.get("_id", ""))).lower())


def case_row(case: dict, tz: timezone | None = None) -> Row:
    title = str(case.get("title") or "(untitled case)")
    sev = str(case.get("severity") or "")
    tags = ", ".join(case.get("tags") or [])
    n = case.get("totalAlerts") or 0
    status = str(case.get("status") or "")
    return Row(id=case.get("id", ""), title=title, severity=sev, where=tags, when=when(case.get("created_at") or "", tz=tz),
               extra=f"{status} · {n} alert{'' if n == 1 else 's'}",
               haystack=" ".join((title, sev, tags, status, case.get("id", ""))).lower())


def match(rows, word: str) -> list[Row]:
    """The rows containing what was typed after the colon (case-insensitive; one word, since the prompt token ends at a space)."""
    w = word.lower()
    return [r for r in rows if w in r.haystack]


class Picker:
    """Lists open alerts and cases per (cluster, space) with a short cache. Safe to call from several threads."""

    def __init__(self, ttl: float = TTL, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._cache: dict[tuple, tuple[float, Listing]] = {}
        self._locks: dict[tuple, threading.Lock] = {}
        self._meta = threading.Lock()

    def listing(self, client: Elastic, kind: str) -> Listing:
        """The rows for `kind` in the client's current space. Blocks while a fetch is needed."""
        snap = client.in_space(client.space)  # the space is read once: a /space switch mid-fetch cannot mix spaces
        key = (snap.kibana_url, snap.space, kind)
        with self._meta:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:  # one fetch per key at a time; a second caller waits, then finds the cache filled
            hit = self._cache.get(key)
            if hit and self.clock() < hit[0]:
                return hit[1]
            result = self._fetch(snap, kind)
            self._cache[key] = (self.clock() + (self.ttl if result.ok else ERROR_TTL), result)
            return result

    def prefetch(self, client: Elastic, kind: str) -> None:
        """Start filling the cache in the background, if it is not already warm or being filled."""
        snap = client.in_space(client.space)
        key = (snap.kibana_url, snap.space, kind)
        hit = self._cache.get(key)
        if (hit and self.clock() < hit[0]) or self._locks.get(key, threading.Lock()).locked():
            return
        threading.Thread(target=self.listing, args=(client, kind), daemon=True).start()

    def forget(self) -> None:
        self._cache.clear()

    def _fetch(self, c: Elastic, kind: str) -> Listing:
        # say which cases these are: an empty list may only mean the other owners' cases are not listed
        label = "open alerts" if kind == "alert" else ("open security cases" if case_owner() == DEFAULT_CASE_OWNER else "open cases")
        try:
            items = c.open_alerts(LIMIT) if kind == "alert" else c.open_cases(LIMIT)
        except ApiError as exc:
            return Listing((), f"could not list {label} in '{c.space}': {exc.message[:60]}", ok=False)
        rows = tuple(alert_row(i) if kind == "alert" else case_row(i) for i in items)
        if rows:
            return Listing(rows)
        return Listing((), f"no {label} in '{c.space}'" + self._elsewhere(c, kind))

    def _elsewhere(self, c: Elastic, kind: str) -> str:
        """' · try /space soc (9), prod (2)' when this space is empty but others have some. Kept short, with the
        part to act on first: the menu is anchored at the cursor and truncates what does not fit."""
        try:
            spaces = [s["id"] for s in c.spaces() if s.get("id") != c.space][:MAX_OTHER_SPACES]
        except (ApiError, KeyError, TypeError):
            return ""
        found = []
        for sp in spaces:
            try:
                n = c.in_space(sp).count_open(kind)
            except ApiError:
                continue
            if n:
                found.append((sp, n))
        if not found:
            return ""
        return " · try /space " + ", ".join(f"{sp} ({n})" for sp, n in found)
