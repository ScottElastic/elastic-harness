"""/ingest: load local text files into Elasticsearch so Agent Builder agents can search them.

Everything is written to an index named ``eh-local-<slug>`` (never a shared data stream).
Document ids are stable (path + chunk number), so re-ingesting a folder updates in place.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .client import Elastic
from .guard import SKIP_DIRS, is_secret_path, is_text_file

PREFIX = "eh-local-"
TEXT_EXT = {
    ".md", ".markdown", ".txt", ".rst", ".json", ".ndjson", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".csv", ".log", ".html", ".htm", ".xml", ".py", ".js", ".ts", ".tsx", ".sh", ".sql", ".go", ".java",
    ".rb", ".tf", ".conf",
}
MAX_FILE_BYTES = 2_000_000
MAX_FILES = 2000

MAPPINGS = {
    "properties": {
        "path": {"type": "keyword"},
        "name": {"type": "keyword"},
        "ext": {"type": "keyword"},
        "root": {"type": "keyword"},
        "chunk_no": {"type": "integer"},
        "content": {"type": "text"},
        "mtime": {"type": "date"},
        "ingested_at": {"type": "date"},
        "source": {"type": "keyword"},
    }
}


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "files"


def index_name(slug: str) -> str:
    slug = slugify(slug)
    return slug if slug.startswith(PREFIX) else PREFIX + slug


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    """Split text into ~size-char chunks on paragraph boundaries, with a small overlap."""
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    cur = ""
    for para in re.split(r"\n\s*\n", text):
        while len(para) > size:  # a single huge paragraph: hard-split on whitespace
            cut = para.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(para[:cut].strip())
            para = para[max(cut - overlap, 1):].strip() if len(para) - cut > overlap else para[cut:].strip()
        if len(cur) + len(para) + 2 > size and cur:
            chunks.append(cur)
            cur = cur[-overlap:] + "\n\n" + para if overlap else para
        else:
            cur = (cur + "\n\n" + para) if cur else para
    if cur.strip():
        chunks.append(cur)
    return [c for c in (c.strip() for c in chunks) if c]


def iter_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            yield Path(dirpath) / fn


def ingest(client: Elastic, path: str, slug: str | None = None, tool: bool = False,
           agent: str | None = None, say: Callable[[str], None] = print) -> dict:
    root = Path(path).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"{root} does not exist")
    reason = is_secret_path(root)
    if reason:
        raise PermissionError(f"refusing to ingest: {reason}")

    index = index_name(slug or root.stem or root.name)
    created = client.ensure_index(index, MAPPINGS)
    say(f"index {index} {'created' if created else 'exists (updating in place)'}")

    now = datetime.now(timezone.utc).isoformat()
    files = chunks = failed = skipped = 0
    batch: list[tuple[str, dict]] = []
    for f in iter_files(root):
        if files >= MAX_FILES:
            say(f"stopped at {MAX_FILES} files")
            break
        if f.suffix.lower() not in TEXT_EXT or is_secret_path(f):
            skipped += 1
            continue
        try:
            size = f.stat().st_size
            if size > MAX_FILE_BYTES or size == 0 or not is_text_file(f):
                skipped += 1
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped += 1
            continue
        pieces = chunk_text(text)
        if not pieces:
            skipped += 1
            continue
        if not created:  # drop stale chunks from a previous, longer version of this file
            client.es("POST", f"/{index}/_delete_by_query?refresh=true&conflicts=proceed",
                      {"query": {"term": {"path": str(f)}}})
        mtime = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat()
        for n, piece in enumerate(pieces):
            doc_id = hashlib.sha1(f"{f}#{n}".encode()).hexdigest()[:20]
            batch.append((doc_id, {"path": str(f), "name": f.name, "ext": f.suffix.lower().lstrip("."),
                                   "root": str(root), "chunk_no": n, "content": piece, "mtime": mtime,
                                   "ingested_at": now, "source": "elastic-harness"}))
        files += 1
        chunks += len(pieces)
        if len(batch) >= 200:
            failed += client.bulk(index, batch)
            batch = []
    failed += client.bulk(index, batch)

    out = {"index": index, "files": files, "chunks": chunks, "skipped": skipped, "failed": failed}
    if tool or agent:
        tool_id = f"eh-search-{slugify(index[len(PREFIX):])}"
        client.create_tool({
            "id": tool_id, "type": "index_search", "tags": ["elastic-harness"],
            "description": f"Search the local files ingested from {root} by Elastic Harness (index {index}). "
                           f"Each hit is a text chunk with its source file path.",
            "configuration": {"pattern": index},
        })
        out["tool"] = tool_id
        if agent:
            out["attached_to_agent"] = agent if client.attach_tool(agent, tool_id) else f"{agent} (already had it)"
    return out
