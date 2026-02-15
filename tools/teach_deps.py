#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Iterable
import urllib.parse
import urllib.request
import uuid

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

DEFAULT_SCRAPER_BASE_URL = os.environ.get("SEQ_SCRAPER_BASE_URL", "http://127.0.0.1:7444")
DEFAULT_SCRAPER_API_KEY = os.environ.get("SEQ_SCRAPER_API_KEY", "")
DEFAULT_CACHE_PATH = Path(".ai/internal/teach-cache.json")
DEFAULT_OUTPUT_DIR = Path(".ai/skills/generated")
DEFAULT_MEM_EVENTS_PATH = Path(
    os.environ.get("SEQ_CH_MEM_PATH", str(Path.home() / "repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl"))
)


@dataclass(slots=True)
class TeachTarget:
    name: str
    ecosystem: str
    urls: list[str]


def now_epoch_ms() -> int:
    return int(time.time() * 1000)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str) -> str:
    base = value.strip().lower()
    base = re.sub(r"^https?://", "", base)
    base = re.sub(r"[^a-z0-9._-]+", "-", base)
    base = re.sub(r"-+", "-", base).strip("-")
    return base[:120] or "skill"


def http_json(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout_s: float = 10.0,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    body_bytes: bytes | None = None
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)

    if payload is not None:
        body_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    url = f"{base_url.rstrip('/')}{path}"
    req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw.strip() else {}


def http_text(url: str, timeout_s: float = 8.0, headers: dict[str, str] | None = None) -> str:
    req_headers = {
        "User-Agent": "seq-teach/0.1 (+https://github.com/nikivdev/seq)",
        "Accept": "application/json,text/html,text/plain;q=0.8,*/*;q=0.1",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _subject_value(value: Any, max_len: int = 240) -> str:
    text = str(value).replace("\t", " ").replace("\n", " ").strip()
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def make_subject(fields: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={_subject_value(value)}")
    return "\t".join(parts)


class TeachEventSink:
    def __init__(self, path: Path, enabled: bool = True):
        self.path = path
        self.enabled = enabled
        self.session_id = f"teach-{now_epoch_ms()}-{uuid.uuid4().hex[:8]}"
        self._index = 0
        self._fh: Any | None = None
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
        except Exception:
            self.enabled = False
            self._fh = None

    def emit(
        self,
        name: str,
        ok: bool = True,
        dur_us: int = 0,
        subject: str | None = None,
        ts_ms: int | None = None,
    ) -> None:
        if not self.enabled or self._fh is None:
            return
        self._index += 1
        row_ts = int(ts_ms if ts_ms is not None else now_epoch_ms())
        event_id = f"{self.session_id}:{self._index:06d}"
        row_subject = subject if subject else None
        content_src = f"{name}\t{row_subject or ''}"
        row = {
            "ts_ms": row_ts,
            "dur_us": int(max(0, dur_us)),
            "ok": bool(ok),
            "session_id": self.session_id,
            "event_id": event_id,
            "content_hash": hashlib.sha256(content_src.encode("utf-8")).hexdigest(),
            "name": name,
            "subject": row_subject,
        }
        try:
            self._fh.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False))
            self._fh.write("\n")
        except Exception:
            self.enabled = False

    def close(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass
        self._fh = None


class TeachCache:
    def __init__(self, path: Path, ttl_s: float):
        self.path = path
        self.ttl_s = ttl_s
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"entries": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"entries": {}}

    def _entry_key(self, request_payload: dict[str, Any]) -> str:
        raw = json.dumps(request_payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, request_payload: dict[str, Any]) -> dict[str, Any] | None:
        key = self._entry_key(request_payload)
        entry = dict(self._state.get("entries", {}).get(key, {}))
        if not entry:
            return None
        saved_at = float(entry.get("saved_at", 0.0))
        if (time.time() - saved_at) > self.ttl_s:
            return None
        result = entry.get("result")
        if not isinstance(result, dict):
            return None
        return result

    def put(self, request_payload: dict[str, Any], result: dict[str, Any]) -> None:
        key = self._entry_key(request_payload)
        entries = self._state.setdefault("entries", {})
        entries[key] = {
            "saved_at": time.time(),
            "result": result,
        }

    def flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)


class ScraperClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_s: float,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.headers: dict[str, str] = {}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

    def health(self) -> dict[str, Any]:
        return http_json(
            self.base_url,
            "GET",
            "/health",
            timeout_s=self.timeout_s,
            headers=self.headers,
        )

    def enqueue(self, request_payload: dict[str, Any]) -> int:
        data = http_json(
            self.base_url,
            "POST",
            "/jobs",
            payload=request_payload,
            timeout_s=self.timeout_s,
            headers=self.headers,
        )
        job_id = int(data.get("job_id", 0))
        if job_id <= 0:
            raise RuntimeError(f"invalid enqueue response: {data}")
        return job_id

    def job(self, job_id: int) -> dict[str, Any]:
        return http_json(
            self.base_url,
            "GET",
            f"/jobs/{job_id}",
            timeout_s=self.timeout_s,
            headers=self.headers,
        )

    def scrape_sync(self, request_payload: dict[str, Any]) -> dict[str, Any]:
        return http_json(
            self.base_url,
            "POST",
            "/scrape",
            payload=request_payload,
            timeout_s=max(20.0, self.timeout_s),
            headers=self.headers,
        )


def extract_requirement_name(raw: str) -> str | None:
    cleaned = raw.split(";", 1)[0].strip()
    match = re.match(r"^([A-Za-z0-9_.-]+)", cleaned)
    return match.group(1) if match else None


def discover_dependencies(repo_root: Path) -> dict[str, set[str]]:
    discovered: dict[str, set[str]] = {
        "npm": set(),
        "pypi": set(),
        "cargo": set(),
        "swift": set(),
    }

    package_json = repo_root / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                for dep_name in dict(data.get(key, {})).keys():
                    discovered["npm"].add(str(dep_name))
        except Exception:
            pass

    if tomllib is not None:
        pyproject = repo_root / "pyproject.toml"
        if pyproject.exists():
            try:
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                project = dict(data.get("project", {}))
                for raw in list(project.get("dependencies", [])):
                    dep = extract_requirement_name(str(raw))
                    if dep:
                        discovered["pypi"].add(dep)
                optional = dict(project.get("optional-dependencies", {}))
                for values in optional.values():
                    for raw in list(values):
                        dep = extract_requirement_name(str(raw))
                        if dep:
                            discovered["pypi"].add(dep)
            except Exception:
                pass

        cargo = repo_root / "Cargo.toml"
        if cargo.exists():
            try:
                data = tomllib.loads(cargo.read_text(encoding="utf-8"))
                for key in ("dependencies", "dev-dependencies", "build-dependencies"):
                    for dep_name in dict(data.get(key, {})).keys():
                        discovered["cargo"].add(str(dep_name))
            except Exception:
                pass

    package_swift = repo_root / "Package.swift"
    if package_swift.exists():
        try:
            text = package_swift.read_text(encoding="utf-8")
            for match in re.finditer(r"\.package\([^\)]*url\s*:\s*\"([^\"]+)\"", text):
                url = match.group(1)
                tail = url.rstrip("/").split("/")[-1]
                dep_name = tail.replace(".git", "")
                if dep_name:
                    discovered["swift"].add(dep_name)
        except Exception:
            pass

    return discovered


def normalize_repo_url(value: str) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    raw = raw.removeprefix("git+")
    if raw.startswith("git://"):
        raw = "https://" + raw[len("git://") :]
    if raw.startswith("github:"):
        tail = raw.split(":", 1)[1]
        raw = f"https://github.com/{tail}"
    raw = raw.removesuffix(".git")
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return None


def dependency_urls(name: str, ecosystem: str) -> list[str]:
    urls: list[str] = []

    def add_url(raw: str | None) -> None:
        if not raw:
            return
        normalized = raw.strip()
        if not normalized:
            return
        if normalized not in urls:
            urls.append(normalized)

    if ecosystem == "npm":
        encoded = urllib.parse.quote(name, safe="@/")
        add_url(f"https://www.npmjs.com/package/{encoded}")
        try:
            metadata = json.loads(http_text(f"https://registry.npmjs.org/{encoded}", timeout_s=6.0))
            latest_tag = dict(metadata.get("dist-tags", {})).get("latest")
            versions = dict(metadata.get("versions", {}))
            latest = dict(versions.get(latest_tag, {})) if latest_tag else {}
            add_url(latest.get("homepage"))
            repo = latest.get("repository")
            if isinstance(repo, dict):
                add_url(normalize_repo_url(str(repo.get("url", ""))))
            elif isinstance(repo, str):
                add_url(normalize_repo_url(repo))
        except Exception:
            pass

    elif ecosystem == "pypi":
        encoded = urllib.parse.quote(name)
        add_url(f"https://pypi.org/project/{encoded}/")
        try:
            metadata = json.loads(http_text(f"https://pypi.org/pypi/{encoded}/json", timeout_s=6.0))
            info = dict(metadata.get("info", {}))
            add_url(info.get("home_page"))
            for value in dict(info.get("project_urls", {})).values():
                add_url(str(value))
        except Exception:
            pass

    elif ecosystem == "cargo":
        encoded = urllib.parse.quote(name)
        add_url(f"https://crates.io/crates/{encoded}")
        try:
            metadata = json.loads(http_text(f"https://crates.io/api/v1/crates/{encoded}", timeout_s=6.0))
            crate = dict(metadata.get("crate", {}))
            add_url(crate.get("documentation"))
            add_url(crate.get("homepage"))
            add_url(normalize_repo_url(str(crate.get("repository", ""))))
        except Exception:
            pass

    elif ecosystem == "swift":
        add_url(f"https://swiftpackageindex.com/search?query={urllib.parse.quote(name)}")
        add_url(f"https://github.com/search?q={urllib.parse.quote(name)}+language%3ASwift&type=repositories")

    else:
        add_url(f"https://duckduckgo.com/?q={urllib.parse.quote(name + ' docs')}")

    return urls[:8]


def install_hint(name: str, ecosystem: str) -> str:
    if ecosystem == "npm":
        return f"pnpm add {name}"
    if ecosystem == "pypi":
        return f"python -m pip install {name}"
    if ecosystem == "cargo":
        return f"cargo add {name}"
    if ecosystem == "swift":
        return f".package(url: \"https://github.com/.../{name}.git\", from: \"x.y.z\")"
    return f"install {name}"


def build_scrape_payload(url: str) -> dict[str, Any]:
    return {
        "url": url,
        "mode": "balanced",
        "timeout_s": 20.0,
        "max_bytes": 1_500_000,
        "queries": [
            {"name": "title", "query": "css:title", "index": -1},
            {"name": "h1", "query": "css:h1", "index": -1},
            {"name": "h2", "query": "css:h2", "index": -1},
            {"name": "h3", "query": "css:h3", "index": -1},
        ],
    }


def direct_fallback_scrape(url: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        text = http_text(url, timeout_s=12.0)
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return {
            "success": False,
            "url": url,
            "final_url": url,
            "status_code": 0,
            "content_type": "",
            "kind": "unknown",
            "title": "",
            "text_excerpt": "",
            "query_results": {},
            "fetched_bytes": 0,
            "timings_ms": {"fetch": elapsed_ms, "extract": 0.0, "total": elapsed_ms},
            "error": str(exc),
        }

    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
    excerpt = re.sub(r"<[^>]+>", " ", text)
    excerpt = re.sub(r"\s+", " ", excerpt).strip()[:2000]
    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "success": True,
        "url": url,
        "final_url": url,
        "status_code": 200,
        "content_type": "text/html",
        "kind": "html",
        "title": title,
        "text_excerpt": excerpt,
        "query_results": {},
        "fetched_bytes": len(text.encode("utf-8", errors="ignore")),
        "timings_ms": {"fetch": elapsed_ms, "extract": 0.0, "total": elapsed_ms},
        "error": None,
    }


def collect_results(
    client: ScraperClient,
    payloads_by_url: dict[str, dict[str, Any]],
    cache: TeachCache,
    force: bool,
    poll_timeout_s: float,
    events: TeachEventSink | None = None,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    pending_jobs: dict[int, str] = {}

    for url, payload in payloads_by_url.items():
        if not force:
            cached = cache.get(payload)
            if cached is not None:
                copy = dict(cached)
                copy["cache_hit"] = True
                results[url] = copy
                timings = dict(copy.get("timings_ms", {}))
                total_us = int(max(0.0, float(timings.get("total", 0.0))) * 1000)
                events and events.emit(
                    "teach.scrape.done",
                    ok=bool(copy.get("success")),
                    dur_us=total_us,
                    subject=make_subject(
                        {
                            "url": url,
                            "cache_hit": 1,
                            "source": "cache",
                            "status_code": copy.get("status_code", 0),
                            "error": copy.get("error"),
                        }
                    ),
                )
                if not bool(copy.get("success")):
                    events and events.emit(
                        "teach.scrape.error",
                        ok=False,
                        subject=make_subject(
                            {
                                "url": url,
                                "source": "cache",
                                "error": copy.get("error") or "cached_failure",
                            }
                        ),
                    )
                continue
        job_id = client.enqueue(payload)
        pending_jobs[job_id] = url
        events and events.emit(
            "teach.scrape.enqueue",
            ok=True,
            subject=make_subject(
                {
                    "url": url,
                    "job_id": job_id,
                    "mode": payload.get("mode"),
                }
            ),
        )

    sleep_s = 0.05
    deadline = time.monotonic() + poll_timeout_s
    while pending_jobs:
        completed_any = False
        for job_id in list(pending_jobs.keys()):
            job = client.job(job_id)
            status = str(job.get("status", ""))
            if status in {"done", "failed"}:
                url = pending_jobs.pop(job_id)
                result = dict(job.get("result", {})) if isinstance(job.get("result"), dict) else {}
                if not result:
                    result = {
                        "success": False,
                        "url": url,
                        "final_url": url,
                        "status_code": 0,
                        "kind": "unknown",
                        "title": "",
                        "text_excerpt": "",
                        "query_results": {},
                        "error": job.get("error") or "job failed without result",
                    }
                result.setdefault("cache_hit", False)
                results[url] = result
                cache.put(payloads_by_url[url], result)
                timings = dict(result.get("timings_ms", {}))
                total_us = int(max(0.0, float(timings.get("total", 0.0))) * 1000)
                events and events.emit(
                    "teach.scrape.done",
                    ok=bool(result.get("success")),
                    dur_us=total_us,
                    subject=make_subject(
                        {
                            "url": url,
                            "job_id": job_id,
                            "cache_hit": 0,
                            "source": "queue",
                            "status_code": result.get("status_code", 0),
                            "error": result.get("error"),
                        }
                    ),
                )
                if not bool(result.get("success")):
                    events and events.emit(
                        "teach.scrape.error",
                        ok=False,
                        subject=make_subject(
                            {
                                "url": url,
                                "job_id": job_id,
                                "source": "queue",
                                "error": result.get("error") or job.get("error") or "scrape_failed",
                            }
                        ),
                    )
                completed_any = True

        if not pending_jobs:
            break

        if time.monotonic() > deadline:
            events and events.emit(
                "teach.scrape.error",
                ok=False,
                subject=make_subject(
                    {
                        "source": "queue",
                        "error": f"poll_timeout:{len(pending_jobs)}",
                    }
                ),
            )
            raise TimeoutError(f"timed out while waiting for {len(pending_jobs)} scrape jobs")

        if completed_any:
            sleep_s = 0.05
        else:
            sleep_s = min(0.5, sleep_s * 1.5)
        time.sleep(sleep_s)

    return results


def extract_headings(result: dict[str, Any], limit: int = 20) -> list[str]:
    query_results = dict(result.get("query_results", {}))
    headings: list[str] = []
    for key in ("h1", "h2", "h3"):
        for value in list(query_results.get(key, [])):
            text = re.sub(r"\s+", " ", str(value)).strip()
            if text and text not in headings:
                headings.append(text)
                if len(headings) >= limit:
                    return headings
    return headings


def pick_summary_lines(result: dict[str, Any], limit: int = 3) -> list[str]:
    excerpt = str(result.get("text_excerpt", "")).strip()
    if not excerpt:
        return []
    parts = [segment.strip() for segment in re.split(r"(?<=[.!?])\s+", excerpt) if segment.strip()]
    lines: list[str] = []
    for part in parts:
        text = part[:220]
        if looks_noisy_summary(text):
            continue
        if text and text not in lines:
            lines.append(text)
        if len(lines) >= limit:
            break
    return lines


def looks_noisy_summary(text: str) -> bool:
    lowered = text.lower()
    if "function(" in lowered or "window." in lowered or "document." in lowered:
        return True
    if lowered.count("{") + lowered.count("}") >= 3 and lowered.count(";") >= 2:
        return True
    if "gtag(" in lowered or "datalayer" in lowered:
        return True

    letters = sum(1 for ch in text if ch.isalpha())
    punctuation = sum(1 for ch in text if ch in "{}[]();<>|")
    if letters == 0:
        return True
    if punctuation > 12 and punctuation > letters:
        return True
    return False


def render_skill(target: TeachTarget, docs: list[dict[str, Any]]) -> str:
    generated_at = utc_now_iso()
    usable = [doc for doc in docs if bool(doc.get("success"))]

    headings: list[str] = []
    summaries: list[str] = []
    for doc in usable:
        for heading in extract_headings(doc):
            if heading not in headings:
                headings.append(heading)
        for summary in pick_summary_lines(doc):
            if summary not in summaries:
                summaries.append(summary)

    description = (
        f"On-demand reference for {target.name} ({target.ecosystem}) built from scraped official sources."
    )

    out: list[str] = []
    out.append("---")
    out.append(f"name: dep-{slugify(target.name)}")
    out.append(f"description: {description}")
    out.append("---")
    out.append("")
    out.append("# Dependency Skill")
    out.append("")
    out.append(f"- Dependency: `{target.name}`")
    out.append(f"- Ecosystem: `{target.ecosystem}`")
    out.append(f"- Generated: `{generated_at}`")
    out.append(f"- Sources scraped: `{len(docs)}` (successful: `{len(usable)}`)")
    out.append("")
    out.append("## Install")
    out.append("")
    out.append(f"`{install_hint(target.name, target.ecosystem)}`")
    out.append("")

    if summaries:
        out.append("## Quick Notes")
        out.append("")
        for line in summaries[:8]:
            out.append(f"- {line}")
        out.append("")

    if headings:
        out.append("## Likely Important Topics")
        out.append("")
        for heading in headings[:20]:
            out.append(f"- {heading}")
        out.append("")

    out.append("## Sources")
    out.append("")
    for doc in docs:
        final_url = str(doc.get("final_url") or doc.get("url") or "")
        title = str(doc.get("title") or "(untitled)")
        ok = bool(doc.get("success"))
        cache_hit = bool(doc.get("cache_hit"))
        fetch_ms = float(dict(doc.get("timings_ms", {})).get("fetch", 0.0))
        total_ms = float(dict(doc.get("timings_ms", {})).get("total", 0.0))
        status = "ok" if ok else "failed"
        out.append(
            f"- [{title}]({final_url}) | `{status}` | `cache={cache_hit}` | "
            f"`fetch_ms={fetch_ms:.1f}` | `total_ms={total_ms:.1f}`"
        )

    if not usable:
        out.append("")
        out.append("## Fallback")
        out.append("")
        out.append("- No source scraped successfully. Re-run once scraper daemon is healthy.")

    return "\n".join(out) + "\n"


def write_skill_files(
    repo_root: Path,
    out_dir: Path,
    target: TeachTarget,
    docs: list[dict[str, Any]],
) -> Path:
    destination = repo_root / out_dir / slugify(target.name)
    destination.mkdir(parents=True, exist_ok=True)

    skill_path = destination / "SKILL.md"
    sources_path = destination / "sources.json"

    skill_path.write_text(render_skill(target, docs), encoding="utf-8")
    sources_path.write_text(
        json.dumps(
            {
                "name": target.name,
                "ecosystem": target.ecosystem,
                "generated_at": utc_now_iso(),
                "sources": docs,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return skill_path


def gather_targets_from_deps(
    deps: list[str],
    ecosystem: str | None,
    discovered: dict[str, set[str]],
) -> list[TeachTarget]:
    targets: list[TeachTarget] = []
    for dep in deps:
        dep_clean = dep.strip()
        if not dep_clean:
            continue
        eco = ecosystem
        if eco is None:
            for candidate, names in discovered.items():
                if dep_clean in names:
                    eco = candidate
                    break
        if eco is None:
            eco = "npm"
        targets.append(TeachTarget(name=dep_clean, ecosystem=eco, urls=dependency_urls(dep_clean, eco)))
    return targets


def gather_auto_targets(discovered: dict[str, set[str]], top: int, ecosystems: set[str]) -> list[TeachTarget]:
    targets: list[TeachTarget] = []
    for ecosystem in ("npm", "pypi", "cargo", "swift"):
        if ecosystems and ecosystem not in ecosystems:
            continue
        names = sorted(discovered.get(ecosystem, set()))
        for name in names[:top]:
            targets.append(TeachTarget(name=name, ecosystem=ecosystem, urls=dependency_urls(name, ecosystem)))
    return targets


def scrape_targets(
    client: ScraperClient,
    targets: list[TeachTarget],
    cache: TeachCache,
    force: bool,
    poll_timeout_s: float,
    allow_direct_fallback: bool,
    events: TeachEventSink | None = None,
) -> dict[str, list[dict[str, Any]]]:
    payloads_by_url: dict[str, dict[str, Any]] = {}
    owners_by_url: dict[str, list[str]] = {}
    for target in targets:
        for url in target.urls:
            if url not in payloads_by_url:
                payloads_by_url[url] = build_scrape_payload(url)
            owners_by_url.setdefault(url, []).append(target.name)

    results_by_url: dict[str, dict[str, Any]]
    try:
        client.health()
        results_by_url = collect_results(client, payloads_by_url, cache, force, poll_timeout_s, events=events)
    except Exception as exc:
        events and events.emit(
            "teach.scrape.error",
            ok=False,
            subject=make_subject(
                {
                    "source": "queue",
                    "error": str(exc),
                    "allow_direct_fallback": int(bool(allow_direct_fallback)),
                }
            ),
        )
        if not allow_direct_fallback:
            raise
        print(f"[teach] scraper unavailable, using direct fallback: {exc}")
        results_by_url = {}
        for url, payload in payloads_by_url.items():
            if not force:
                cached = cache.get(payload)
                if cached is not None:
                    copy = dict(cached)
                    copy["cache_hit"] = True
                    results_by_url[url] = copy
                    timings = dict(copy.get("timings_ms", {}))
                    total_us = int(max(0.0, float(timings.get("total", 0.0))) * 1000)
                    events and events.emit(
                        "teach.scrape.done",
                        ok=bool(copy.get("success")),
                        dur_us=total_us,
                        subject=make_subject(
                            {
                                "url": url,
                                "cache_hit": 1,
                                "source": "cache",
                                "status_code": copy.get("status_code", 0),
                                "error": copy.get("error"),
                            }
                        ),
                    )
                    if not bool(copy.get("success")):
                        events and events.emit(
                            "teach.scrape.error",
                            ok=False,
                            subject=make_subject(
                                {
                                    "url": url,
                                    "source": "cache",
                                    "error": copy.get("error") or "cached_failure",
                                }
                            ),
                        )
                    continue
            enqueue_started = time.perf_counter()
            events and events.emit(
                "teach.scrape.enqueue",
                ok=True,
                subject=make_subject(
                    {
                        "url": url,
                        "mode": payload.get("mode"),
                        "source": "direct_fallback",
                    }
                ),
            )
            result = direct_fallback_scrape(url)
            result["cache_hit"] = False
            results_by_url[url] = result
            cache.put(payload, result)
            elapsed_us = int((time.perf_counter() - enqueue_started) * 1_000_000)
            timings = dict(result.get("timings_ms", {}))
            total_us = int(max(float(timings.get("total", 0.0)) * 1000, elapsed_us))
            events and events.emit(
                "teach.scrape.done",
                ok=bool(result.get("success")),
                dur_us=total_us,
                subject=make_subject(
                    {
                        "url": url,
                        "cache_hit": 0,
                        "source": "direct_fallback",
                        "status_code": result.get("status_code", 0),
                        "error": result.get("error"),
                    }
                ),
            )
            if not bool(result.get("success")):
                events and events.emit(
                    "teach.scrape.error",
                    ok=False,
                    subject=make_subject(
                        {
                            "url": url,
                            "source": "direct_fallback",
                            "error": result.get("error") or "direct_fallback_failed",
                        }
                    ),
                )

    if allow_direct_fallback:
        for url, result in list(results_by_url.items()):
            if bool(result.get("success")):
                continue
            events and events.emit(
                "teach.scrape.error",
                ok=False,
                subject=make_subject(
                    {
                        "url": url,
                        "source": "queue",
                        "error": result.get("error") or "queue_result_failed",
                        "action": "retry_direct",
                    }
                ),
            )
            fallback = direct_fallback_scrape(url)
            if bool(fallback.get("success")):
                fallback["cache_hit"] = False
                fallback["fallback_from_scraper_error"] = result.get("error")
                results_by_url[url] = fallback
                cache.put(payloads_by_url[url], fallback)
                timings = dict(fallback.get("timings_ms", {}))
                total_us = int(max(0.0, float(timings.get("total", 0.0))) * 1000)
                events and events.emit(
                    "teach.scrape.done",
                    ok=True,
                    dur_us=total_us,
                    subject=make_subject(
                        {
                            "url": url,
                            "cache_hit": 0,
                            "source": "direct_retry",
                            "status_code": fallback.get("status_code", 0),
                            "error": fallback.get("fallback_from_scraper_error"),
                        }
                    ),
                )
            else:
                events and events.emit(
                    "teach.scrape.error",
                    ok=False,
                    subject=make_subject(
                        {
                            "url": url,
                            "source": "direct_retry",
                            "error": fallback.get("error") or "direct_retry_failed",
                        }
                    ),
                )

    result_map: dict[str, list[dict[str, Any]]] = {target.name: [] for target in targets}
    for url, doc in results_by_url.items():
        for owner in owners_by_url.get(url, []):
            result_map.setdefault(owner, []).append(doc)

    return result_map


def build_event_sink(args: argparse.Namespace, repo_root: Path) -> TeachEventSink:
    mem_path_raw = str(args.mem_events_path).strip()
    if args.no_mem_events or not mem_path_raw:
        return TeachEventSink(repo_root / ".ai/internal/disabled", enabled=False)
    path = Path(mem_path_raw).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return TeachEventSink(path=path, enabled=True)


def docs_stats(docs_by_target: dict[str, list[dict[str, Any]]]) -> tuple[int, int, int]:
    total = 0
    ok = 0
    cache_hit = 0
    for docs in docs_by_target.values():
        for doc in docs:
            total += 1
            if bool(doc.get("success")):
                ok += 1
            if bool(doc.get("cache_hit")):
                cache_hit += 1
    return total, ok, cache_hit


def run_dep(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    discovered = discover_dependencies(repo_root)
    targets = gather_targets_from_deps(args.deps, args.ecosystem, discovered)

    if not targets:
        print("[teach] no dependency targets")
        return 1

    client = ScraperClient(args.scraper_base_url, args.scraper_api_key, args.scraper_timeout_s)
    cache = TeachCache(repo_root / args.cache_path, ttl_s=args.cache_ttl_hours * 3600)
    events = build_event_sink(args, repo_root)

    started = time.perf_counter()
    events.emit(
        "teach.run.start",
        subject=make_subject(
            {
                "mode": "dep",
                "targets": len(targets),
                "force": int(bool(args.force)),
                "allow_direct_fallback": int(bool(args.allow_direct_fallback)),
            }
        ),
    )
    try:
        docs_by_target = scrape_targets(
            client=client,
            targets=targets,
            cache=cache,
            force=args.force,
            poll_timeout_s=args.poll_timeout_s,
            allow_direct_fallback=args.allow_direct_fallback,
            events=events,
        )

        created_paths: list[Path] = []
        for target in targets:
            docs = docs_by_target.get(target.name, [])
            skill_path = write_skill_files(repo_root, Path(args.out_dir), target, docs)
            created_paths.append(skill_path)
            ok_docs = sum(1 for doc in docs if bool(doc.get("success")))
            fail_docs = max(0, len(docs) - ok_docs)
            events.emit(
                "teach.skill.generated",
                ok=ok_docs > 0,
                subject=make_subject(
                    {
                        "dependency": target.name,
                        "ecosystem": target.ecosystem,
                        "docs_total": len(docs),
                        "docs_ok": ok_docs,
                        "docs_fail": fail_docs,
                        "path": str(skill_path.relative_to(repo_root)),
                    }
                ),
            )

        cache.flush()

        total_docs, ok_docs, cache_hits = docs_stats(docs_by_target)
        elapsed_us = int((time.perf_counter() - started) * 1_000_000)
        events.emit(
            "teach.run.done",
            ok=True,
            dur_us=elapsed_us,
            subject=make_subject(
                {
                    "mode": "dep",
                    "targets": len(targets),
                    "skills_generated": len(created_paths),
                    "docs_total": total_docs,
                    "docs_ok": ok_docs,
                    "cache_hits": cache_hits,
                }
            ),
        )

        elapsed_ms = elapsed_us / 1000.0
        print(f"[teach] generated {len(created_paths)} skills in {elapsed_ms:.1f}ms")
        for path in created_paths:
            print(f"[teach] skill: {path}")
        return 0
    except Exception as exc:
        elapsed_us = int((time.perf_counter() - started) * 1_000_000)
        events.emit(
            "teach.run.done",
            ok=False,
            dur_us=elapsed_us,
            subject=make_subject(
                {
                    "mode": "dep",
                    "targets": len(targets),
                    "error": str(exc),
                }
            ),
        )
        raise
    finally:
        events.close()


def run_auto(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()
    discovered = discover_dependencies(repo_root)
    ecosystems = {token.strip() for token in args.ecosystems.split(",") if token.strip()}
    targets = gather_auto_targets(discovered, top=args.top, ecosystems=ecosystems)

    if not targets:
        print("[teach] no dependencies discovered for auto mode")
        return 1

    client = ScraperClient(args.scraper_base_url, args.scraper_api_key, args.scraper_timeout_s)
    cache = TeachCache(repo_root / args.cache_path, ttl_s=args.cache_ttl_hours * 3600)
    events = build_event_sink(args, repo_root)

    started = time.perf_counter()
    events.emit(
        "teach.run.start",
        subject=make_subject(
            {
                "mode": "auto",
                "targets": len(targets),
                "force": int(bool(args.force)),
                "allow_direct_fallback": int(bool(args.allow_direct_fallback)),
            }
        ),
    )
    try:
        docs_by_target = scrape_targets(
            client=client,
            targets=targets,
            cache=cache,
            force=args.force,
            poll_timeout_s=args.poll_timeout_s,
            allow_direct_fallback=args.allow_direct_fallback,
            events=events,
        )

        created_paths: list[Path] = []
        for target in targets:
            docs = docs_by_target.get(target.name, [])
            skill_path = write_skill_files(repo_root, Path(args.out_dir), target, docs)
            created_paths.append(skill_path)
            ok_docs = sum(1 for doc in docs if bool(doc.get("success")))
            fail_docs = max(0, len(docs) - ok_docs)
            events.emit(
                "teach.skill.generated",
                ok=ok_docs > 0,
                subject=make_subject(
                    {
                        "dependency": target.name,
                        "ecosystem": target.ecosystem,
                        "docs_total": len(docs),
                        "docs_ok": ok_docs,
                        "docs_fail": fail_docs,
                        "path": str(skill_path.relative_to(repo_root)),
                    }
                ),
            )

        cache.flush()

        total_docs, ok_docs, cache_hits = docs_stats(docs_by_target)
        elapsed_us = int((time.perf_counter() - started) * 1_000_000)
        events.emit(
            "teach.run.done",
            ok=True,
            dur_us=elapsed_us,
            subject=make_subject(
                {
                    "mode": "auto",
                    "targets": len(targets),
                    "skills_generated": len(created_paths),
                    "docs_total": total_docs,
                    "docs_ok": ok_docs,
                    "cache_hits": cache_hits,
                }
            ),
        )

        elapsed_ms = elapsed_us / 1000.0
        print(f"[teach] auto-generated {len(created_paths)} skills in {elapsed_ms:.1f}ms")
        for path in created_paths:
            print(f"[teach] skill: {path}")
        return 0
    except Exception as exc:
        elapsed_us = int((time.perf_counter() - started) * 1_000_000)
        events.emit(
            "teach.run.done",
            ok=False,
            dur_us=elapsed_us,
            subject=make_subject(
                {
                    "mode": "auto",
                    "targets": len(targets),
                    "error": str(exc),
                }
            ),
        )
        raise
    finally:
        events.close()


def run_url(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve()

    cleaned_urls = [raw.strip() for raw in args.urls if raw.strip()]
    targets: list[TeachTarget] = []
    if args.name:
        if cleaned_urls:
            targets.append(TeachTarget(name=args.name, ecosystem="url", urls=cleaned_urls))
    else:
        for url in cleaned_urls:
            targets.append(TeachTarget(name=slugify(url), ecosystem="url", urls=[url]))

    if not targets:
        print("[teach] no urls provided")
        return 1

    client = ScraperClient(args.scraper_base_url, args.scraper_api_key, args.scraper_timeout_s)
    cache = TeachCache(repo_root / args.cache_path, ttl_s=args.cache_ttl_hours * 3600)
    events = build_event_sink(args, repo_root)

    started = time.perf_counter()
    events.emit(
        "teach.run.start",
        subject=make_subject(
            {
                "mode": "url",
                "targets": len(targets),
                "force": int(bool(args.force)),
                "allow_direct_fallback": int(bool(args.allow_direct_fallback)),
            }
        ),
    )
    try:
        docs_by_target = scrape_targets(
            client=client,
            targets=targets,
            cache=cache,
            force=args.force,
            poll_timeout_s=args.poll_timeout_s,
            allow_direct_fallback=args.allow_direct_fallback,
            events=events,
        )

        created_paths: list[Path] = []
        for target in targets:
            docs = docs_by_target.get(target.name, [])
            skill_path = write_skill_files(repo_root, Path(args.out_dir), target, docs)
            created_paths.append(skill_path)
            ok_docs = sum(1 for doc in docs if bool(doc.get("success")))
            fail_docs = max(0, len(docs) - ok_docs)
            events.emit(
                "teach.skill.generated",
                ok=ok_docs > 0,
                subject=make_subject(
                    {
                        "dependency": target.name,
                        "ecosystem": target.ecosystem,
                        "docs_total": len(docs),
                        "docs_ok": ok_docs,
                        "docs_fail": fail_docs,
                        "path": str(skill_path.relative_to(repo_root)),
                    }
                ),
            )

        cache.flush()

        total_docs, ok_docs, cache_hits = docs_stats(docs_by_target)
        elapsed_us = int((time.perf_counter() - started) * 1_000_000)
        events.emit(
            "teach.run.done",
            ok=True,
            dur_us=elapsed_us,
            subject=make_subject(
                {
                    "mode": "url",
                    "targets": len(targets),
                    "skills_generated": len(created_paths),
                    "docs_total": total_docs,
                    "docs_ok": ok_docs,
                    "cache_hits": cache_hits,
                }
            ),
        )

        elapsed_ms = elapsed_us / 1000.0
        print(f"[teach] generated {len(created_paths)} URL skills in {elapsed_ms:.1f}ms")
        for path in created_paths:
            print(f"[teach] skill: {path}")
        return 0
    except Exception as exc:
        elapsed_us = int((time.perf_counter() - started) * 1_000_000)
        events.emit(
            "teach.run.done",
            ok=False,
            dur_us=elapsed_us,
            subject=make_subject(
                {
                    "mode": "url",
                    "targets": len(targets),
                    "error": str(exc),
                }
            ),
        )
        raise
    finally:
        events.close()


def add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=".", help="repo root for dependency discovery and output")
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="relative output directory for generated skills",
    )
    parser.add_argument(
        "--cache-path",
        default=str(DEFAULT_CACHE_PATH),
        help="relative path for scrape result cache",
    )
    parser.add_argument("--cache-ttl-hours", type=float, default=24.0, help="cache TTL in hours")
    parser.add_argument("--force", action="store_true", help="ignore cache and scrape again")
    parser.add_argument(
        "--scraper-base-url",
        default=DEFAULT_SCRAPER_BASE_URL,
        help="scraper daemon/api base URL",
    )
    parser.add_argument(
        "--scraper-api-key",
        default=DEFAULT_SCRAPER_API_KEY,
        help="optional bearer token for scraper API",
    )
    parser.add_argument("--scraper-timeout-s", type=float, default=8.0, help="request timeout for scraper calls")
    parser.add_argument("--poll-timeout-s", type=float, default=120.0, help="max seconds to wait for queued jobs")
    parser.add_argument(
        "--allow-direct-fallback",
        action="store_true",
        help="if scraper is unavailable, fetch pages directly as fallback",
    )
    parser.add_argument(
        "--mem-events-path",
        default=str(DEFAULT_MEM_EVENTS_PATH),
        help="path to seq.mem JSONEachRow output file for teach.* events",
    )
    parser.add_argument(
        "--no-mem-events",
        action="store_true",
        help="disable teach.* event emission to seq_mem.jsonl",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate dependency skills from scraped docs")
    sub = parser.add_subparsers(dest="command", required=True)

    dep = sub.add_parser("dep", help="generate skill for one or more dependencies")
    dep.add_argument("deps", nargs="+", help="dependency names")
    dep.add_argument("--ecosystem", choices=["npm", "pypi", "cargo", "swift"], help="force ecosystem")
    add_common_flags(dep)

    auto = sub.add_parser("auto", help="discover dependencies from repo and generate skills")
    auto.add_argument("--top", type=int, default=3, help="max dependencies per ecosystem")
    auto.add_argument(
        "--ecosystems",
        default="npm,pypi,cargo,swift",
        help="comma-separated ecosystems to include",
    )
    add_common_flags(auto)

    url = sub.add_parser("url", help="generate skill from one or more direct URLs")
    url.add_argument("urls", nargs="+", help="URL list")
    url.add_argument("--name", help="skill name override")
    add_common_flags(url)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "dep":
        return run_dep(args)
    if args.command == "auto":
        return run_auto(args)
    if args.command == "url":
        return run_url(args)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
