#!/usr/bin/env python3
"""g9s-builders runner: takes a git bundle, builds a commit by a recipe, serves the artifacts.

Standard library only. One build at a time (the boxes are small), a FIFO queue in front.

Recipes: directories under G9S_RECIPES (baked into the image) and files «recipe.<name>» under
G9S_RECIPE_FILES (delivered to the box as project files). Layout under G9S_RUNNER_DATA:
  repos/<recipe>.git          bare repository fed by uploaded bundles; every built commit
                              is kept under refs/builds/<sha> so clients can send deltas
  builds/<id>/meta.json       status, timing, who submitted, artifacts with sha256
  builds/<id>/build.log       runner header + recipe output + runner trailer
  builds/<id>/out/            artifacts the recipe wrote (served as-is)

Authentication: Bearer tokens from G9S_RUNNER_TOKENS, one «name=token» per line. The file is
re-read on every request, so rotating a token is a file write, no restart. Everything but
/api/health needs a token. A token lets its holder run arbitrary code inside the builder:
issue them per person and keep the list short.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("G9S_RUNNER_PORT", "8181"))
DATA = Path(os.environ.get("G9S_RUNNER_DATA", "/data"))
RECIPES = Path(os.environ.get("G9S_RECIPES", "/opt/g9s/recipes"))
RECIPE_FILES = Path(os.environ.get("G9S_RECIPE_FILES", "/etc/g9s/files"))
TOKENS_FILE = Path(os.environ.get("G9S_RUNNER_TOKENS", "/etc/g9s/files/tokens"))
KEEP_BUILDS = int(os.environ.get("G9S_KEEP_BUILDS", "5"))
BUILD_TIMEOUT = int(os.environ.get("G9S_BUILD_TIMEOUT", "7200"))
MAX_UPLOAD = int(os.environ.get("G9S_MAX_UPLOAD_MB", "512")) * 1024 * 1024

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RECIPE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
BUILD_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{7}-[0-9a-f]{4}$")
PARAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
UPLOAD_REF = "refs/g9s/upload"

log = logging.getLogger("g9s.runner")
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

QUEUE: "queue.Queue[str]" = queue.Queue()
STATE_LOCK = threading.Lock()  # guards meta writes and repo imports
CURRENT: dict = {"id": None, "proc": None, "cancel": False}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def git(repo: Path, *args: str, check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check, **kw)


# ---------------------------------------------------------------- storage

def build_dir(bid: str) -> Path:
    return DATA / "builds" / bid


def repo_dir(recipe: str) -> Path:
    return DATA / "repos" / f"{recipe}.git"


def ensure_repo(recipe: str) -> Path:
    repo = repo_dir(recipe)
    if not (repo / "HEAD").exists():
        repo.mkdir(parents=True, exist_ok=True)
        git(repo, "init", "-q", "--bare")
    return repo


def load_meta(bid: str) -> dict | None:
    p = build_dir(bid) / "meta.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def save_meta(meta: dict) -> None:
    p = build_dir(meta["id"]) / "meta.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    os.replace(tmp, p)


def list_build_ids() -> list[str]:
    root = DATA / "builds"
    if not root.exists():
        return []
    return sorted((d.name for d in root.iterdir() if BUILD_ID_RE.match(d.name)), reverse=True)


def recipes() -> dict[str, Path]:
    """name → script. Two sources: directories ``<G9S_RECIPES>/<name>/build.sh`` baked into the image
    (generic recipes) and flat files ``<G9S_RECIPE_FILES>/recipe.<name>`` delivered to the box as
    project files (customer recipes; they never enter the image or this repository)."""
    found: dict[str, Path] = {}
    if RECIPES.exists():
        for d in RECIPES.iterdir():
            if (d / "build.sh").is_file() and RECIPE_RE.match(d.name):
                found[d.name] = d / "build.sh"
    if RECIPE_FILES.exists():
        for f in RECIPE_FILES.iterdir():
            if f.is_file() and f.name.startswith("recipe.") and RECIPE_RE.match(f.name[7:]):
                found[f.name[7:]] = f
    return found


def recipe_names() -> list[str]:
    return sorted(recipes())


def repo_tips(recipe: str) -> list[str]:
    repo = repo_dir(recipe)
    if not (repo / "HEAD").exists():
        return []
    out = git(repo, "for-each-ref", "--sort=-committerdate", "--count=50", "--format=%(objectname)", "refs/builds/").stdout
    return out.split()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_artifacts(out: Path) -> list[dict]:
    if not out.exists():
        return []
    items = []
    for p in sorted(out.iterdir()):
        if p.is_file() and NAME_RE.match(p.name):
            items.append({"name": p.name, "size": p.stat().st_size, "sha256": sha256_of(p)})
    return items


# ---------------------------------------------------------------- tokens

def authenticate(header: str | None) -> str | None:
    """Return the token's name, or None. Missing tokens file → None for everyone."""
    if not header or not header.startswith("Bearer "):
        return None
    given = header[7:].strip()
    if not given:
        return None
    try:
        lines = TOKENS_FILE.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, token = line.partition("=")
        if not sep:
            name, _, token = line.partition(" ")
        name, token = name.strip(), token.strip()
        if name and token and hmac.compare_digest(token, given):
            return name
    return None


# ---------------------------------------------------------------- bundle import

class ImportError_(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail = code, detail


def import_bundle(recipe: str, commit: str, bundle: Path | None) -> None:
    """Make `commit` available in the recipe's bare repo, from the bundle if given."""
    repo = ensure_repo(recipe)
    with STATE_LOCK:
        if bundle is not None:
            v = git(repo, "bundle", "verify", str(bundle), check=False)
            if v.returncode != 0:
                raise ImportError_("bundle_invalid", v.stderr.strip())
            f = git(repo, "fetch", "-q", "--no-tags", str(bundle), f"+{UPLOAD_REF}:{UPLOAD_REF}", check=False)
            if f.returncode != 0:
                raise ImportError_("bundle_fetch_failed", f.stderr.strip())
            git(repo, "update-ref", "-d", UPLOAD_REF, check=False)
        probe = git(repo, "rev-parse", "--verify", "-q", f"{commit}^{{commit}}", check=False)
        if probe.returncode != 0:
            raise ImportError_("commit_unknown", "the builder has no such commit; send a bundle carrying it under " + UPLOAD_REF)
        git(repo, "update-ref", f"refs/builds/{commit}", commit)


# ---------------------------------------------------------------- build worker

def prune_recipe(recipe: str) -> None:
    kept = 0
    for bid in list_build_ids():
        meta = load_meta(bid)
        if not meta or meta.get("recipe") != recipe:
            continue
        if meta.get("status") in ("queued", "running"):
            continue
        kept += 1
        if kept <= KEEP_BUILDS or meta.get("pruned"):
            continue
        shutil.rmtree(build_dir(bid) / "out", ignore_errors=True)
        shutil.rmtree(build_dir(bid) / "src", ignore_errors=True)
        meta["pruned"] = True
        meta["artifacts"] = []
        save_meta(meta)
        log.info("pruned artifacts of %s", bid)


def run_build(bid: str) -> None:
    meta = load_meta(bid)
    if not meta or meta["status"] != "queued":
        return
    bdir, src, out = build_dir(bid), build_dir(bid) / "src", build_dir(bid) / "out"
    out.mkdir(exist_ok=True)
    meta.update(status="running", started_at=now_iso())
    save_meta(meta)
    started = time.monotonic()
    status, rc = "failed", None
    with (bdir / "build.log").open("ab") as logf:
        def say(line: str) -> None:
            logf.write(f"[g9s {datetime.now(timezone.utc).strftime('%H:%M:%S')}] {line}\n".encode())
            logf.flush()

        say(f"build {bid}: recipe={meta['recipe']} commit={meta['commit']} by={meta['submitted_by']} params={meta['params']}")
        try:
            shutil.rmtree(src, ignore_errors=True)
            # init + fetch of one commit rather than clone: the bare repo has no HEAD branch,
            # and a clone of it would print «cloned an empty repository» into every log.
            for cmd in (["git", "init", "-q", str(src)],
                        ["git", "-C", str(src), "fetch", "-q", "--no-tags", str(repo_dir(meta["recipe"])), meta["commit"]],
                        ["git", "-C", str(src), "checkout", "-q", "--detach", meta["commit"]]):
                subprocess.run(cmd, check=True, stdout=logf, stderr=subprocess.STDOUT)
            env = dict(os.environ)
            env.update(G9S_BUILD_ID=bid, G9S_RECIPE=meta["recipe"], G9S_COMMIT=meta["commit"],
                       G9S_SHA7=meta["commit"][:7], G9S_SRC=str(src), G9S_OUT=str(out),
                       G9S_SUBMITTED_BY=meta["submitted_by"])
            for k, v in meta["params"].items():
                env[f"G9S_PARAM_{k.upper()}"] = v
            say("recipe start")
            script = recipes().get(meta["recipe"])
            if script is None:
                raise RuntimeError(f"recipe {meta['recipe']} is no longer available")
            proc = subprocess.Popen(["bash", str(script)], cwd=str(src), env=env,
                                    stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
            CURRENT.update(id=bid, proc=proc, cancel=False)
            try:
                rc = proc.wait(timeout=BUILD_TIMEOUT)
                status = "cancelled" if CURRENT["cancel"] else ("succeeded" if rc == 0 else "failed")
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                status = "timeout"
                say(f"recipe killed after {BUILD_TIMEOUT}s")
        except Exception as e:  # noqa: BLE001 - the log is the place for it
            say(f"runner error: {e!r}")
            status = "failed"
        finally:
            CURRENT.update(id=None, proc=None, cancel=False)
            shutil.rmtree(src, ignore_errors=True)
            duration = round(time.monotonic() - started)
            say(f"finished: status={status} exit={rc} duration={duration}s")
    meta.update(status=status, exit_code=rc, finished_at=now_iso(), duration_s=duration, artifacts=scan_artifacts(out))
    with STATE_LOCK:
        save_meta(meta)
    log.info("build %s %s (%ss, %d artifacts)", bid, status, duration, len(meta["artifacts"]))
    prune_recipe(meta["recipe"])


def worker() -> None:
    while True:
        bid = QUEUE.get()
        try:
            run_build(bid)
        except Exception:  # noqa: BLE001
            log.exception("build %s crashed the worker loop", bid)


def recover_on_start() -> None:
    """Builds left «running» by a restart are failed; queued ones go back to the queue."""
    for bid in reversed(list_build_ids()):
        meta = load_meta(bid)
        if not meta:
            continue
        if meta["status"] == "running":
            meta.update(status="failed", finished_at=now_iso(), note="runner restarted during the build")
            save_meta(meta)
            shutil.rmtree(build_dir(bid) / "src", ignore_errors=True)
        elif meta["status"] == "queued":
            QUEUE.put(bid)


# ---------------------------------------------------------------- HTTP

class HttpError(Exception):
    def __init__(self, status: int, code: str, detail: str = ""):
        super().__init__(code)
        self.status, self.code, self.detail = status, code, detail


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "g9s-runner/0.1"

    def log_message(self, fmt, *args):  # quieter than the default, same information
        log.info("%s %s", self.address_string(), fmt % args)

    # -- helpers

    def send_json(self, status: int, payload, headers: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=1).encode() + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_file(self, path: Path, ctype: str, start: int = 0, headers: dict | None = None) -> None:
        size = path.stat().st_size
        start = min(max(start, 0), size)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size - start))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as f:
            f.seek(start)
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def read_body_to(self, path: Path) -> int:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise HttpError(413, "upload_too_large", f"limit {MAX_UPLOAD} bytes")
        got = 0
        with path.open("wb") as f:
            while got < length:
                chunk = self.rfile.read(min(1 << 20, length - got))
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
        if got != length:
            raise HttpError(400, "body_truncated")
        return got

    def require_auth(self) -> str:
        who = authenticate(self.headers.get("Authorization"))
        if who is None:
            if not TOKENS_FILE.exists():
                raise HttpError(503, "tokens_unavailable", f"{TOKENS_FILE} is missing on the builder")
            raise HttpError(401, "unauthorized")
        return who

    # -- routing

    def do_GET(self):
        self.dispatch()

    def do_HEAD(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        url = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            self.route(url.path, q)
        except HttpError as e:
            self.send_json(e.status, {"error": e.code, "detail": e.detail})
        except Exception as e:  # noqa: BLE001
            log.exception("request failed")
            self.send_json(500, {"error": "internal", "detail": repr(e)})

    def route(self, path: str, q: dict) -> None:
        m = self.command
        if path in ("/", "/api/health") and m in ("GET", "HEAD"):
            return self.send_json(200, {"status": "ok", "running": CURRENT["id"], "queued": QUEUE.qsize(),
                                        "recipes": recipe_names(), "tokens_file_present": TOKENS_FILE.exists()})
        who = self.require_auth()

        if path == "/api/recipes" and m == "GET":
            return self.send_json(200, recipe_names())

        mt = re.fullmatch(r"/api/repos/([^/]+)/tips", path)
        if mt and m == "GET":
            recipe = mt.group(1)
            if not RECIPE_RE.match(recipe):
                raise HttpError(400, "bad_recipe")
            return self.send_json(200, repo_tips(recipe))

        if path == "/api/builds" and m == "GET":
            limit = int(q.get("limit", "20"))
            items = []
            for bid in list_build_ids():
                meta = load_meta(bid)
                if meta and (not q.get("recipe") or meta["recipe"] == q["recipe"]):
                    items.append({k: meta.get(k) for k in ("id", "recipe", "commit", "status", "submitted_by",
                                                            "submitted_at", "finished_at", "duration_s")})
                if len(items) >= limit:
                    break
            return self.send_json(200, items)

        if path == "/api/builds" and m == "POST":
            return self.submit(q, who)

        mb = re.fullmatch(r"/api/builds/([^/]+)(/log|/cancel|/artifacts/([^/]+))?", path)
        if not mb:
            raise HttpError(404, "not_found")
        bid, sub, name = mb.group(1), mb.group(2) or "", mb.group(3)
        if not BUILD_ID_RE.match(bid):
            raise HttpError(400, "bad_build_id")
        meta = load_meta(bid)
        if not meta:
            raise HttpError(404, "build_not_found")
        if sub == "" and m == "GET":
            return self.send_json(200, meta)
        if sub == "/log" and m in ("GET", "HEAD"):
            p = build_dir(bid) / "build.log"
            if not p.exists():
                p.touch()
            return self.send_file(p, "text/plain; charset=utf-8", int(q.get("from", "0")),
                                  {"X-Log-Size": str(p.stat().st_size), "X-Build-Status": meta["status"]})
        if sub == "/cancel" and m == "POST":
            if meta["status"] == "running" and CURRENT["id"] == bid and CURRENT["proc"] is not None:
                CURRENT["cancel"] = True
                os.killpg(CURRENT["proc"].pid, signal.SIGTERM)
                log.info("build %s cancel requested by %s", bid, who)
                return self.send_json(202, {"id": bid, "status": "cancelling"})
            if meta["status"] == "queued":
                with STATE_LOCK:
                    meta.update(status="cancelled", finished_at=now_iso())
                    save_meta(meta)
                return self.send_json(200, {"id": bid, "status": "cancelled"})
            raise HttpError(409, "not_cancellable", meta["status"])
        if name is not None and m in ("GET", "HEAD"):
            if not NAME_RE.match(name):
                raise HttpError(400, "bad_artifact_name")
            p = build_dir(bid) / "out" / name
            if not p.is_file():
                raise HttpError(404, "artifact_not_found")
            return self.send_file(p, "application/octet-stream",
                                  headers={"Content-Disposition": f'attachment; filename="{name}"'})
        raise HttpError(405, "method_not_allowed")

    def submit(self, q: dict, who: str) -> None:
        recipe, commit = q.get("recipe", ""), q.get("commit", "").lower()
        if recipe not in recipe_names():
            raise HttpError(400, "unknown_recipe", f"known: {', '.join(recipe_names())}")
        if not SHA_RE.match(commit):
            raise HttpError(400, "bad_commit", "a full 40-hex commit id is required")
        params = {}
        for k, v in q.items():
            if k.startswith("p_"):
                key = k[2:]
                if not PARAM_RE.match(key) or len(v) > 256 or any(c in v for c in "\n\r\0"):
                    raise HttpError(400, "bad_param", k)
                params[key] = v
        bid = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{commit[:7]}-{uuid.uuid4().hex[:4]}"
        bdir = build_dir(bid)
        bdir.mkdir(parents=True)
        bundle = bdir / "upload.bundle"
        size = self.read_body_to(bundle)
        try:
            import_bundle(recipe, commit, bundle if size else None)
        except ImportError_ as e:
            shutil.rmtree(bdir, ignore_errors=True)
            raise HttpError(422, e.code, e.detail)
        finally:
            bundle.unlink(missing_ok=True)
        meta = {"id": bid, "recipe": recipe, "commit": commit, "sha7": commit[:7], "params": params,
                "submitted_by": who, "submitted_at": now_iso(), "bundle_bytes": size, "status": "queued",
                "started_at": None, "finished_at": None, "duration_s": None, "exit_code": None, "artifacts": []}
        with STATE_LOCK:
            save_meta(meta)
        QUEUE.put(bid)
        log.info("build %s queued: recipe=%s commit=%s by=%s bundle=%dB params=%s", bid, recipe, commit, who, size, params)
        self.send_json(202, {"id": bid, "status": "queued", "position": QUEUE.qsize()})


def main() -> None:
    (DATA / "builds").mkdir(parents=True, exist_ok=True)
    (DATA / "repos").mkdir(parents=True, exist_ok=True)
    recover_on_start()
    threading.Thread(target=worker, name="builder", daemon=True).start()
    log.info("g9s runner on :%d, data=%s, recipes=%s, tokens=%s (%s)", PORT, DATA, recipe_names(), TOKENS_FILE,
             "present" if TOKENS_FILE.exists() else "MISSING")
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
