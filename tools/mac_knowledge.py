#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import time
from typing import Iterable
from urllib import error as url_error
from urllib import request as url_request


DEFAULT_DB_PATH = "~/.local/share/seq/mac_knowledge.db"
DEFAULT_ZVEC_URL = "http://127.0.0.1:8900"
DEFAULT_ZVEC_MODEL = "mac_kg_hashed"

TEXT_EXTENSIONS = {
    ".c",
    ".cc",
    ".cfg",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".m",
    ".md",
    ".mm",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
    ".zsh",
}

SKIP_DIRS = {
    ".git",
    ".hg",
    ".jj",
    ".svn",
    ".idea",
    ".vscode",
    ".venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "target",
    ".next",
    ".turbo",
}

TOKEN_RE = re.compile(r"[a-z0-9]{2,}", re.IGNORECASE)


@dataclasses.dataclass(slots=True)
class FileRow:
    path: str
    root: str
    kind: str
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    inode: int
    sha256: str | None
    git_repo: str | None
    git_branch: str | None
    indexed_ms: int
    last_seen_ms: int
    title: str
    content: str


@dataclasses.dataclass(slots=True)
class WatchLink:
    watcher: str
    confidence: float
    reason: str
    source_file: str | None
    source_line: int | None


class GitResolver:
    def __init__(self) -> None:
        self._dir_cache: dict[str, str | None] = {}
        self._branch_cache: dict[str, str | None] = {}

    def resolve(self, file_path: str) -> tuple[str | None, str | None]:
        parent = str(Path(file_path).parent)
        repo = self._repo_for_dir(parent)
        if not repo:
            return None, None
        branch = self._branch_cache.get(repo)
        if branch is None and repo not in self._branch_cache:
            self._branch_cache[repo] = self._detect_branch(repo)
            branch = self._branch_cache[repo]
        return repo, branch

    def _repo_for_dir(self, directory: str) -> str | None:
        if directory in self._dir_cache:
            return self._dir_cache[directory]

        parts = Path(directory).resolve()
        for current in [parts, *parts.parents]:
            marker = current / ".git"
            if marker.exists():
                repo = str(current)
                self._dir_cache[directory] = repo
                return repo
        self._dir_cache[directory] = None
        return None

    @staticmethod
    def _detect_branch(repo: str) -> str | None:
        try:
            out = subprocess.check_output(
                ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.0,
            )
        except Exception:
            return None
        value = out.strip()
        return value or None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _expand(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def _ensure_parent(path: str) -> None:
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _connect(db_path: str) -> sqlite3.Connection:
    resolved = _expand(db_path)
    _ensure_parent(resolved)
    conn = sqlite3.connect(resolved)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA cache_size = -200000")
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
          path TEXT PRIMARY KEY,
          root TEXT NOT NULL,
          kind TEXT NOT NULL,
          size INTEGER NOT NULL,
          mtime_ns INTEGER NOT NULL,
          ctime_ns INTEGER NOT NULL,
          mode INTEGER NOT NULL,
          inode INTEGER NOT NULL,
          sha256 TEXT,
          git_repo TEXT,
          git_branch TEXT,
          indexed_ms INTEGER NOT NULL,
          last_seen_ms INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_files_root ON files(root);
        CREATE INDEX IF NOT EXISTS idx_files_last_seen ON files(last_seen_ms);
        CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime_ns);

        CREATE TABLE IF NOT EXISTS file_content (
          path TEXT PRIMARY KEY REFERENCES files(path) ON DELETE CASCADE,
          title TEXT NOT NULL,
          content TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS file_fts
        USING fts5(path UNINDEXED, title, content, tokenize='porter unicode61');

        CREATE TABLE IF NOT EXISTS watcher_links (
          path TEXT NOT NULL,
          watcher TEXT NOT NULL,
          confidence REAL NOT NULL,
          reason TEXT NOT NULL,
          source_file TEXT,
          source_line INTEGER,
          updated_ms INTEGER NOT NULL,
          PRIMARY KEY(path, watcher, reason, source_file, source_line)
        );

        CREATE INDEX IF NOT EXISTS idx_watcher_links_path ON watcher_links(path);
        """
    )
    conn.commit()


def _is_text_candidate(path: str) -> bool:
    return Path(path).suffix.lower() in TEXT_EXTENSIONS


def _safe_read_text(path: str, max_bytes: int) -> str:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(max_bytes)
    except Exception:
        return ""
    if b"\x00" in raw:
        return ""
    return raw.decode("utf-8", errors="ignore")


def _title_for(path: str, text: str) -> str:
    for line in text.splitlines():
        item = line.strip()
        if not item:
            continue
        if item.startswith("#"):
            item = item.lstrip("#").strip()
        return item[:200]
    return Path(path).name


def _sha256_for(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _kind_for_mode(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _iter_files(roots: list[str], include_hidden: bool = False) -> Iterable[tuple[str, str, os.stat_result]]:
    for root in roots:
        root_path = Path(root)
        if not root_path.exists():
            continue

        stack: list[Path] = [root_path]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        name = entry.name
                        if not include_hidden and name.startswith(".") and name not in {".config"}:
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if name in SKIP_DIRS:
                                continue
                            stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        yield root, entry.path, st
            except OSError:
                continue


def _load_existing_signatures(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    rows = conn.execute("SELECT path, mtime_ns, size FROM files").fetchall()
    return {str(row[0]): (int(row[1]), int(row[2])) for row in rows}


def _fts_replace(conn: sqlite3.Connection, path: str, title: str, content: str) -> None:
    conn.execute("DELETE FROM file_fts WHERE path = ?", (path,))
    conn.execute(
        "INSERT INTO file_fts(path, title, content) VALUES(?, ?, ?)",
        (path, title, content),
    )


def _upsert_file(conn: sqlite3.Connection, row: FileRow) -> None:
    conn.execute(
        """
        INSERT INTO files(
          path, root, kind, size, mtime_ns, ctime_ns, mode, inode,
          sha256, git_repo, git_branch, indexed_ms, last_seen_ms
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
          root = excluded.root,
          kind = excluded.kind,
          size = excluded.size,
          mtime_ns = excluded.mtime_ns,
          ctime_ns = excluded.ctime_ns,
          mode = excluded.mode,
          inode = excluded.inode,
          sha256 = excluded.sha256,
          git_repo = excluded.git_repo,
          git_branch = excluded.git_branch,
          indexed_ms = excluded.indexed_ms,
          last_seen_ms = excluded.last_seen_ms
        """,
        (
            row.path,
            row.root,
            row.kind,
            row.size,
            row.mtime_ns,
            row.ctime_ns,
            row.mode,
            row.inode,
            row.sha256,
            row.git_repo,
            row.git_branch,
            row.indexed_ms,
            row.last_seen_ms,
        ),
    )
    conn.execute(
        """
        INSERT INTO file_content(path, title, content)
        VALUES(?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
          title = excluded.title,
          content = excluded.content
        """,
        (row.path, row.title, row.content),
    )
    _fts_replace(conn, row.path, row.title, row.content)


def _delete_stale(conn: sqlite3.Connection, roots: list[str], seen_ms: int) -> int:
    placeholders = ",".join("?" for _ in roots)
    if not placeholders:
        return 0

    stale_paths = conn.execute(
        f"""
        SELECT path FROM files
        WHERE root IN ({placeholders}) AND last_seen_ms != ?
        """,
        (*roots, seen_ms),
    ).fetchall()
    stale_list = [str(row[0]) for row in stale_paths]
    if not stale_list:
        return 0

    conn.executemany("DELETE FROM file_fts WHERE path = ?", ((item,) for item in stale_list))
    conn.executemany("DELETE FROM file_content WHERE path = ?", ((item,) for item in stale_list))
    conn.executemany("DELETE FROM watcher_links WHERE path = ?", ((item,) for item in stale_list))
    conn.executemany("DELETE FROM files WHERE path = ?", ((item,) for item in stale_list))
    return len(stale_list)


def _hashed_embedding(text: str, dim: int = 768) -> list[float]:
    vec = [0.0] * dim
    for token in TOKEN_RE.findall(text.lower()):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        h = int.from_bytes(digest, "little", signed=False)
        idx = h % dim
        sign = -1.0 if ((h >> 8) & 1) else 1.0
        vec[idx] += sign

    norm_sq = sum(value * value for value in vec)
    if norm_sq <= 0.0:
        return vec
    inv = norm_sq ** -0.5
    return [value * inv for value in vec]


def _http_json(url: str, payload: dict) -> dict | None:
    body = json.dumps(payload).encode("utf-8")
    req = url_request.Request(
        url,
        data=body,
        method="POST",
        headers={"content-type": "application/json", "accept": "application/json"},
    )
    try:
        with url_request.urlopen(req, timeout=2.5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (url_error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def _zvec_upsert(zvec_url: str, model: str, rows: list[FileRow], dim: int = 768) -> tuple[int, int]:
    if not rows:
        return 0, 0

    docs = []
    for row in rows:
        text = f"{row.title}\n{row.path}\n{row.content[:50000]}"
        docs.append(
            {
                "chunk_id": row.path,
                "embedding": _hashed_embedding(text, dim=dim),
                "project_path": row.path,
                "agent": "mac-kg",
                "ts_ms": row.indexed_ms,
            }
        )

    failed = 0
    upserted = 0
    batch_size = 64
    for idx in range(0, len(docs), batch_size):
        payload = {"docs": docs[idx : idx + batch_size], "model": model}
        data = _http_json(f"{zvec_url.rstrip('/')}/upsert", payload)
        if not data:
            failed += len(payload["docs"])
            continue
        upserted += int(data.get("upserted", 0))
        failed += int(data.get("failed", 0))
    return upserted, failed


def _zvec_search(zvec_url: str, model: str, query: str, limit: int, dim: int = 768) -> list[dict]:
    payload = {
        "vector": _hashed_embedding(query, dim=dim),
        "model": model,
        "topk": limit,
    }
    data = _http_json(f"{zvec_url.rstrip('/')}/search", payload)
    if not data:
        return []
    rows = data.get("results")
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        out.append(item)
    return out


def _normalize_roots(raw_roots: list[str] | None) -> list[str]:
    if raw_roots:
        return [_expand(item) for item in raw_roots]
    env = os.getenv("SEQ_MAC_KG_ROOTS", "").strip()
    if env:
        return [_expand(item) for item in env.split(":") if item.strip()]
    return [
        _expand("~/config"),
        _expand("~/code/seq"),
        _expand("~/code/org/la/la"),
        _expand("~/code/myflow"),
    ]


def run_index(
    *,
    db_path: str,
    roots: list[str],
    include_hidden: bool,
    max_bytes: int,
    max_files: int | None,
    full_hash: bool,
    zvec_url: str | None,
    zvec_model: str,
) -> dict:
    roots = _normalize_roots(roots)
    conn = _connect(db_path)
    _init_db(conn)

    signatures = _load_existing_signatures(conn)
    git = GitResolver()

    run_ms = _now_ms()
    scanned = 0
    changed = 0
    skipped_unchanged = 0
    indexed_rows: list[FileRow] = []

    try:
        conn.execute("BEGIN")
        for root, path, st in _iter_files(roots, include_hidden=include_hidden):
            scanned += 1
            if max_files and scanned > max_files:
                break

            prev = signatures.get(path)
            size = int(st.st_size)
            mtime_ns = int(st.st_mtime_ns)
            if prev and prev[0] == mtime_ns and prev[1] == size:
                skipped_unchanged += 1
                conn.execute(
                    "UPDATE files SET last_seen_ms = ? WHERE path = ?",
                    (run_ms, path),
                )
                continue

            content = ""
            title = Path(path).name
            sha256 = None
            if _is_text_candidate(path):
                content = _safe_read_text(path, max_bytes=max_bytes)
                if content:
                    title = _title_for(path, content)
                    if full_hash or size <= max_bytes:
                        sha256 = _sha256_for(content)

            repo, branch = git.resolve(path)
            row = FileRow(
                path=path,
                root=root,
                kind=_kind_for_mode(st.st_mode),
                size=size,
                mtime_ns=mtime_ns,
                ctime_ns=int(st.st_ctime_ns),
                mode=int(st.st_mode),
                inode=int(st.st_ino),
                sha256=sha256,
                git_repo=repo,
                git_branch=branch,
                indexed_ms=run_ms,
                last_seen_ms=run_ms,
                title=title,
                content=content[:max_bytes],
            )
            _upsert_file(conn, row)
            indexed_rows.append(row)
            changed += 1

        deleted = _delete_stale(conn, roots=roots, seen_ms=run_ms)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    zvec_upserted = 0
    zvec_failed = 0
    if zvec_url:
        zvec_upserted, zvec_failed = _zvec_upsert(zvec_url, zvec_model, indexed_rows)

    return {
        "db_path": _expand(db_path),
        "roots": roots,
        "scanned": scanned,
        "changed": changed,
        "skipped_unchanged": skipped_unchanged,
        "deleted": deleted,
        "zvec": {
            "enabled": bool(zvec_url),
            "url": zvec_url,
            "model": zvec_model,
            "upserted": zvec_upserted,
            "failed": zvec_failed,
        },
    }


def _parse_flow_tasks(flow_path: str) -> list[dict]:
    try:
        import tomllib
    except Exception:
        return []
    try:
        with open(flow_path, "rb") as handle:
            data = tomllib.load(handle)
    except Exception:
        return []
    tasks = data.get("tasks", [])
    if isinstance(tasks, list):
        return [item for item in tasks if isinstance(item, dict)]
    return []


def _rg_mentions(patterns: list[str], sources: list[str], max_hits: int) -> list[tuple[str, int, str]]:
    cmd = ["rg", "-n", "--no-heading", "--fixed-strings"]
    for pattern in patterns:
        cmd.extend(["-e", pattern])
    cmd.extend(sources)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode not in {0, 1}:
        return []
    out: list[tuple[str, int, str]] = []
    for raw in proc.stdout.splitlines():
        if len(out) >= max_hits:
            break
        m = re.match(r"^(.*?):(\d+):(.*)$", raw)
        if not m:
            continue
        src = m.group(1)
        line_no = int(m.group(2))
        text = m.group(3).strip()
        out.append((src, line_no, text))
    return out


def infer_watchers(path: str, *, max_hits: int = 200) -> list[WatchLink]:
    abs_path = _expand(path)
    home = str(Path.home())
    basename = Path(abs_path).name
    tilde_path = abs_path.replace(home, "~", 1) if abs_path.startswith(home + "/") else abs_path

    links: list[WatchLink] = []

    flow_path = _expand("~/code/seq/flow.toml")
    tasks = _parse_flow_tasks(flow_path)
    for task in tasks:
        name = str(task.get("name", "")).strip()
        if not name:
            continue
        command = str(task.get("command", ""))
        desc = str(task.get("description", ""))
        hay = f"{name}\n{command}\n{desc}"

        if abs_path in hay or tilde_path in hay:
            links.append(
                WatchLink(
                    watcher=f"flow:{name}",
                    confidence=0.95,
                    reason="Task command/description references path directly",
                    source_file=flow_path,
                    source_line=None,
                )
            )
            continue

        if basename == "config.ts":
            name_lower = name.lower()
            if name_lower.startswith("mac-kg-"):
                continue
            if ("kar" in hay.lower() and "watch" in name_lower) or "gen_macros.py" in hay:
                links.append(
                    WatchLink(
                        watcher=f"flow:{name}",
                        confidence=0.74,
                        reason="Likely participates in kar config build/watch pipeline",
                        source_file=flow_path,
                        source_line=None,
                    )
                )

    sources = [
        _expand("~/code/seq/flow.toml"),
        _expand("~/code/seq/tools"),
        _expand("~/code/seq/docs"),
        _expand("~/config/i/kar/types"),
    ]
    patterns = [abs_path, tilde_path, basename]
    mentions = _rg_mentions(patterns, sources, max_hits=max_hits)
    for src, line_no, text in mentions:
        src_norm = src.replace("\\", "/")
        if src_norm.endswith("/tools/mac_knowledge.py") or src_norm.endswith("/docs/mac-knowledge.md"):
            continue
        confidence = 0.55
        if abs_path in text:
            confidence = 0.95
        elif tilde_path in text:
            confidence = 0.90
        links.append(
            WatchLink(
                watcher=f"src:{Path(src).name}",
                confidence=confidence,
                reason=text[:220],
                source_file=src,
                source_line=line_no,
            )
        )

    if abs_path in {
        _expand("~/config/i/kar/config.ts"),
        _expand("~/.config/kar/config.ts"),
    }:
        links.extend(
            [
                WatchLink(
                    watcher="kar:watch",
                    confidence=0.98,
                    reason="kar watch rebuilds Karabiner JSON from config.ts on save",
                    source_file=_expand("~/code/seq/docs/karabiner-setup.md"),
                    source_line=88,
                ),
                WatchLink(
                    watcher="kar:build",
                    confidence=0.97,
                    reason="kar build regenerates ~/.config/karabiner/karabiner.json",
                    source_file=_expand("~/code/seq/docs/karabiner-setup.md"),
                    source_line=87,
                ),
                WatchLink(
                    watcher="seq:gen-macros",
                    confidence=0.96,
                    reason="seq tools/gen_macros.py reads kar config.ts to generate seq macros",
                    source_file=_expand("~/code/seq/tools/gen_macros.py"),
                    source_line=9,
                ),
                WatchLink(
                    watcher="karabiner-elements",
                    confidence=0.88,
                    reason="Karabiner runtime consumes generated karabiner.json derived from config.ts",
                    source_file=_expand("~/code/seq/docs/karabiner-setup.md"),
                    source_line=16,
                ),
            ]
        )

    dedup: dict[tuple[str, str, str, int | None], WatchLink] = {}
    for link in links:
        key = (link.watcher, link.reason, link.source_file or "", link.source_line)
        prev = dedup.get(key)
        if prev is None or link.confidence > prev.confidence:
            dedup[key] = link

    ordered = sorted(dedup.values(), key=lambda item: item.confidence, reverse=True)
    return ordered[:max_hits]


def store_watchers(conn: sqlite3.Connection, path: str, links: list[WatchLink]) -> None:
    now_ms = _now_ms()
    abs_path = _expand(path)
    conn.execute("DELETE FROM watcher_links WHERE path = ?", (abs_path,))
    for link in links:
        conn.execute(
            """
            INSERT OR REPLACE INTO watcher_links(
              path, watcher, confidence, reason, source_file, source_line, updated_ms
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                abs_path,
                link.watcher,
                float(link.confidence),
                link.reason,
                link.source_file,
                link.source_line,
                now_ms,
            ),
        )
    conn.commit()


def get_watchers(conn: sqlite3.Connection, path: str) -> list[WatchLink]:
    abs_path = _expand(path)
    rows = conn.execute(
        """
        SELECT watcher, confidence, reason, source_file, source_line
        FROM watcher_links
        WHERE path = ?
        ORDER BY confidence DESC, watcher ASC
        """,
        (abs_path,),
    ).fetchall()
    return [
        WatchLink(
            watcher=str(row[0]),
            confidence=float(row[1]),
            reason=str(row[2]),
            source_file=str(row[3]) if row[3] else None,
            source_line=int(row[4]) if row[4] is not None else None,
        )
        for row in rows
    ]


def run_who_watches(db_path: str, path: str, refresh: bool) -> list[WatchLink]:
    conn = _connect(db_path)
    _init_db(conn)
    try:
        links = get_watchers(conn, path)
        if refresh or not links:
            links = infer_watchers(path)
            store_watchers(conn, path, links)
        return links
    finally:
        conn.close()


def run_search(
    *,
    db_path: str,
    query: str,
    limit: int,
    vector: bool,
    zvec_url: str,
    zvec_model: str,
) -> dict:
    conn = _connect(db_path)
    _init_db(conn)
    q = query.strip()
    if not q:
        return {"query": query, "results": []}

    raw_terms = re.findall(r"[A-Za-z0-9_]+", q)
    if raw_terms:
        fts_query = " OR ".join(term for term in raw_terms if term.strip())
    else:
        fts_query = q

    lexical_rows = conn.execute(
        """
        SELECT
          f.path,
          c.title,
          snippet(file_fts, 2, '[', ']', ' ... ', 12) AS snippet,
          bm25(file_fts) AS rank
        FROM file_fts
        JOIN files f ON f.path = file_fts.path
        LEFT JOIN file_content c ON c.path = file_fts.path
        WHERE file_fts MATCH ?
        ORDER BY rank ASC
        LIMIT ?
        """,
        (fts_query, max(1, limit * 3)),
    ).fetchall()

    results: dict[str, dict] = {}
    for idx, row in enumerate(lexical_rows):
        path = str(row[0])
        raw_rank = float(row[3]) if row[3] is not None else 0.0
        score = 1.0 / (1.0 + abs(raw_rank) + idx * 0.05)
        results[path] = {
            "path": path,
            "title": str(row[1]) if row[1] else Path(path).name,
            "snippet": str(row[2]) if row[2] else "",
            "score_lexical": score,
            "score_vector": 0.0,
            "score": score,
        }

    if raw_terms:
        where = " AND ".join("LOWER(f.path) LIKE ?" for _ in raw_terms)
        args = tuple(f"%{term.lower()}%" for term in raw_terms)
        path_rows = conn.execute(
            f"""
            SELECT f.path, c.title, c.content
            FROM files f
            LEFT JOIN file_content c ON c.path = f.path
            WHERE {where}
            LIMIT ?
            """,
            (*args, max(1, limit * 2)),
        ).fetchall()
        for idx, row in enumerate(path_rows):
            path = str(row[0])
            item = results.get(path)
            path_score = 0.95 - idx * 0.02
            title = str(row[1]) if row[1] else Path(path).name
            snippet = str(row[2])[:180] if row[2] else ""
            if item is None:
                item = {
                    "path": path,
                    "title": title,
                    "snippet": snippet,
                    "score_lexical": path_score,
                    "score_vector": 0.0,
                    "score": path_score,
                }
                results[path] = item
            else:
                item["score_lexical"] = max(float(item["score_lexical"]), path_score)
                item["score"] = max(float(item["score"]), float(item["score_lexical"]))

    if vector:
        vec_hits = _zvec_search(zvec_url, zvec_model, q, limit=max(1, limit * 3))
        for hit in vec_hits:
            path = str(hit.get("chunk_id") or hit.get("project_path") or "")
            if not path:
                continue
            vscore = float(hit.get("score", 0.0))
            item = results.get(path)
            if item is None:
                title_row = conn.execute(
                    "SELECT title, content FROM file_content WHERE path = ?",
                    (path,),
                ).fetchone()
                title = str(title_row[0]) if title_row else Path(path).name
                snippet = ""
                if title_row and title_row[1]:
                    snippet = str(title_row[1])[:180]
                item = {
                    "path": path,
                    "title": title,
                    "snippet": snippet,
                    "score_lexical": 0.0,
                    "score_vector": vscore,
                    "score": 0.0,
                }
                results[path] = item
            item["score_vector"] = max(float(item["score_vector"]), vscore)
            item["score"] = float(item["score_lexical"]) * 0.35 + float(item["score_vector"]) * 0.65

    ordered = sorted(results.values(), key=lambda item: item["score"], reverse=True)[:limit]
    conn.close()
    return {
        "query": q,
        "limit": limit,
        "vector": vector,
        "results": ordered,
    }


def run_stats(db_path: str) -> dict:
    conn = _connect(db_path)
    _init_db(conn)
    try:
        files = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        content = int(conn.execute("SELECT COUNT(*) FROM file_content").fetchone()[0])
        watchers = int(conn.execute("SELECT COUNT(*) FROM watcher_links").fetchone()[0])
        newest = conn.execute(
            "SELECT path, indexed_ms FROM files ORDER BY indexed_ms DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    newest_payload = None
    if newest:
        newest_payload = {"path": str(newest[0]), "indexed_ms": int(newest[1])}
    return {
        "db_path": _expand(db_path),
        "files": files,
        "content_rows": content,
        "watcher_links": watchers,
        "newest": newest_payload,
    }


def _json_print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Build and query local macOS knowledge index")
    ap.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite DB path")
    sub = ap.add_subparsers(dest="command", required=True)

    idx = sub.add_parser("index", help="Index files/folders into local knowledge DB")
    idx.add_argument("--root", action="append", default=[], help="Root path to index (repeatable)")
    idx.add_argument("--include-hidden", action="store_true", help="Include hidden files/directories")
    idx.add_argument("--max-bytes", type=int, default=128_000, help="Max bytes to read from text files")
    idx.add_argument("--max-files", type=int, default=None, help="Optional cap for scanned files")
    idx.add_argument("--full-hash", action="store_true", help="Hash all read text content")
    idx.add_argument("--zvec-url", default=None, help="Optional zvec server base URL for vector upsert")
    idx.add_argument("--zvec-model", default=DEFAULT_ZVEC_MODEL)

    watch = sub.add_parser("watch", help="Continuously re-index using fast incremental scans")
    watch.add_argument("--root", action="append", default=[], help="Root path to index (repeatable)")
    watch.add_argument("--include-hidden", action="store_true")
    watch.add_argument("--max-bytes", type=int, default=128_000)
    watch.add_argument("--max-files", type=int, default=None)
    watch.add_argument("--full-hash", action="store_true")
    watch.add_argument("--zvec-url", default=None)
    watch.add_argument("--zvec-model", default=DEFAULT_ZVEC_MODEL)
    watch.add_argument("--interval", type=float, default=3.0, help="Seconds between scans")

    who = sub.add_parser("who-watches", help="Show who watches/depends on a file path")
    who.add_argument("path")
    who.add_argument("--refresh", action="store_true", help="Recompute links from sources")

    search = sub.add_parser("search", help="Search indexed metadata/content")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--vector", action="store_true", help="Merge zvec vector results")
    search.add_argument("--zvec-url", default=DEFAULT_ZVEC_URL)
    search.add_argument("--zvec-model", default=DEFAULT_ZVEC_MODEL)

    stats = sub.add_parser("stats", help="Show DB stats")
    _ = stats
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.command == "index":
        payload = run_index(
            db_path=args.db,
            roots=args.root,
            include_hidden=bool(args.include_hidden),
            max_bytes=max(16_384, int(args.max_bytes)),
            max_files=int(args.max_files) if args.max_files else None,
            full_hash=bool(args.full_hash),
            zvec_url=args.zvec_url,
            zvec_model=args.zvec_model,
        )
        _json_print(payload)
        return 0

    if args.command == "watch":
        roots = _normalize_roots(args.root)
        print(json.dumps({"status": "watching", "roots": roots, "interval_s": args.interval}, indent=2))
        while True:
            payload = run_index(
                db_path=args.db,
                roots=roots,
                include_hidden=bool(args.include_hidden),
                max_bytes=max(16_384, int(args.max_bytes)),
                max_files=int(args.max_files) if args.max_files else None,
                full_hash=bool(args.full_hash),
                zvec_url=args.zvec_url,
                zvec_model=args.zvec_model,
            )
            _json_print(payload)
            time.sleep(max(0.2, float(args.interval)))

    if args.command == "who-watches":
        links = run_who_watches(args.db, args.path, refresh=bool(args.refresh))
        payload = {
            "path": _expand(args.path),
            "count": len(links),
            "watchers": [
                {
                    "watcher": item.watcher,
                    "confidence": item.confidence,
                    "reason": item.reason,
                    "source_file": item.source_file,
                    "source_line": item.source_line,
                }
                for item in links
            ],
        }
        _json_print(payload)
        return 0

    if args.command == "search":
        payload = run_search(
            db_path=args.db,
            query=args.query,
            limit=max(1, int(args.limit)),
            vector=bool(args.vector),
            zvec_url=args.zvec_url,
            zvec_model=args.zvec_model,
        )
        _json_print(payload)
        return 0

    if args.command == "stats":
        _json_print(run_stats(args.db))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
