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

Pull mode: a project file «repo.<name>» (url=…, tags=<regex>, poll=<seconds>, credentials=<file with
user:password>) makes the runner poll that repository's tags and build every new tag matching the
regex, and lets clients ask for a build of any ref with POST /api/builds?recipe=…&ref=…; the
bundle upload keeps working alongside (branches, or no repository access yet).

Authentication: Bearer tokens from G9S_RUNNER_TOKENS, one «name=token» per line. The file is
re-read on every request, so rotating a token is a file write, no restart. Everything but
/api/health needs a token. A token lets its holder run arbitrary code inside the builder:
issue them per person and keep the list short.
"""

from __future__ import annotations

import hmac
import html
import resource
import statistics
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
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("G9S_RUNNER_PORT", "8181"))
DATA = Path(os.environ.get("G9S_RUNNER_DATA", "/data"))
RECIPES = Path(os.environ.get("G9S_RECIPES", "/opt/g9s/recipes"))
RECIPE_FILES = Path(os.environ.get("G9S_RECIPE_FILES", "/etc/g9s/files"))
TOKENS_FILE = Path(os.environ.get("G9S_RUNNER_TOKENS", "/etc/g9s/files/tokens"))
KEEP_BUILDS = int(os.environ.get("G9S_KEEP_BUILDS", "5"))
BUILD_TIMEOUT = int(os.environ.get("G9S_BUILD_TIMEOUT", "7200"))
MAX_UPLOAD = int(os.environ.get("G9S_MAX_UPLOAD_MB", "512")) * 1024 * 1024
SILENCE_WARN = int(os.environ.get("G9S_SILENCE_WARN", "600"))   # no output for this long → «stalled» flag
SILENCE_KILL = int(os.environ.get("G9S_SILENCE_KILL", "0"))     # … and for this long → kill (0 = never)
MONITOR_STEP = 5
ASKPASS = Path(__file__).resolve().parent / "askpass.sh"
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,200}$")

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
CURRENT: dict = {"id": None, "proc": None, "cancel": False, "stage": None, "stage_since": None,
                 "timeline": [], "log": None, "stalled": False, "last_output": None}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def git(repo: Path, *args: str, check: bool = True, **kw) -> subprocess.CompletedProcess:
    kw.setdefault("timeout", 300)
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


# ---------------------------------------------------------------- pull mode

def repo_config(recipe: str) -> dict | None:
    f = RECIPE_FILES / f"repo.{recipe}"
    if not f.is_file():
        return None
    cfg = {"poll": "60", "tags": "", "credentials": ""}
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    if not cfg.get("url"):
        return None
    cred = cfg["credentials"] or str(RECIPE_FILES / f"git_credentials.{recipe}")
    if not Path(cred).is_file():
        cred = str(RECIPE_FILES / "git_credentials")
    cfg["credentials"] = cred if Path(cred).is_file() else ""
    return cfg


def git_env(cfg: dict) -> dict:
    """Environment for git talking to the remote: askpass from the credentials file, no prompts."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if cfg.get("credentials"):
        user, _, password = Path(cfg["credentials"]).read_text().strip().partition(":")
        env.update(GIT_ASKPASS=str(ASKPASS), G9S_GIT_USER=user, G9S_GIT_PASSWORD=password)
    return env


def remote_tags(cfg: dict) -> dict[str, str]:
    out = subprocess.run(["git", "ls-remote", "--tags", "--refs", cfg["url"]], capture_output=True, text=True,
                         env=git_env(cfg), timeout=120)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "git ls-remote failed")
    tags = {}
    for line in out.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/tags/"):
            tags[ref[10:]] = sha
    return tags


def fetch_ref(recipe: str, cfg: dict, ref: str) -> str:
    """Fetch `ref` (tag, branch, sha) from the remote into the recipe's bare repo; return its commit."""
    repo = ensure_repo(recipe)
    with STATE_LOCK:
        f = git(repo, "fetch", "-q", "--no-tags", cfg["url"], ref, check=False, env=git_env(cfg), timeout=900)
        if f.returncode != 0:
            raise ImportError_("fetch_failed", f.stderr.strip()[-300:])
        sha = git(repo, "rev-parse", "--verify", "-q", "FETCH_HEAD^{commit}").stdout.strip()
        git(repo, "update-ref", f"refs/builds/{sha}", sha)
    return sha


def enqueue(recipe: str, commit: str, params: dict, who: str, trigger: str, bundle_bytes: int = 0) -> dict:
    bid = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{commit[:7]}-{uuid.uuid4().hex[:4]}"
    build_dir(bid).mkdir(parents=True)
    meta = {"id": bid, "recipe": recipe, "commit": commit, "sha7": commit[:7], "params": params, "trigger": trigger,
            "submitted_by": who, "submitted_at": now_iso(), "bundle_bytes": bundle_bytes, "status": "queued",
            "started_at": None, "finished_at": None, "duration_s": None, "exit_code": None, "artifacts": []}
    with STATE_LOCK:
        save_meta(meta)
    QUEUE.put(bid)
    log.info("build %s queued: recipe=%s commit=%s by=%s trigger=%s params=%s", bid, recipe, commit, who, trigger, params)
    return meta


POLL_STATE: dict[str, dict] = {}  # recipe → {url, last_poll_at, last_ok_at, error, tags_seen}


def poll_once(recipe: str, cfg: dict) -> None:
    state_file = DATA / "repos" / f"{recipe}.tags.json"
    st = POLL_STATE.setdefault(recipe, {"url": cfg["url"], "last_poll_at": None, "last_ok_at": None, "error": None, "tags_seen": 0})
    st["last_poll_at"] = now_iso()
    try:
        tags = remote_tags(cfg)
    except Exception as e:  # noqa: BLE001
        msg = str(e)[:300]
        if st["error"] != msg:
            log.warning("poll %s: %s", recipe, msg)
        st["error"] = msg
        return
    if st["error"]:
        log.info("poll %s: remote reachable again", recipe)
    st.update(error=None, last_ok_at=now_iso(), tags_seen=len(tags))
    seen = json.loads(state_file.read_text()) if state_file.exists() else None
    if seen is None:
        # first sight of the remote: remember what exists, build nothing from the past
        state_file.write_text(json.dumps(tags, indent=0))
        log.info("poll %s: %d existing tags recorded, new ones will be built (pattern %r)", recipe, len(tags), cfg["tags"])
        return
    pattern = re.compile(cfg["tags"]) if cfg["tags"] else None
    for tag, sha in sorted(tags.items()):
        if tag in seen:
            continue
        seen[tag] = sha
        if pattern is None or not pattern.search(tag):
            continue
        try:
            commit = fetch_ref(recipe, cfg, f"refs/tags/{tag}")
            enqueue(recipe, commit, {}, "tag-poll", f"tag:{tag}")
        except ImportError_ as e:
            log.warning("poll %s: tag %s not fetched: %s", recipe, tag, e.detail)
    state_file.write_text(json.dumps(seen, indent=0))


def poller() -> None:
    last: dict[str, float] = {}
    while True:
        for recipe in recipe_names():
            cfg = repo_config(recipe)
            if not cfg:
                POLL_STATE.pop(recipe, None)
                continue
            every = max(15, int(cfg.get("poll") or 60))
            if time.monotonic() - last.get(recipe, 0) >= every:
                last[recipe] = time.monotonic()
                try:
                    poll_once(recipe, cfg)
                except Exception:  # noqa: BLE001
                    log.exception("poll %s crashed", recipe)
        time.sleep(5)


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


def close_stage(now_mono: float) -> None:
    tl = CURRENT["timeline"]
    if tl and tl[-1].get("duration_s") is None:
        tl[-1]["duration_s"] = round(now_mono - tl[-1]["_t0"])


def monitor(proc: subprocess.Popen, stage_file: Path, log_path: Path, say) -> int | None:
    """Wait for the recipe while tracking its stage, output and silence. None = killed on time."""
    t0 = time.monotonic()
    last_size = log_path.stat().st_size if log_path.exists() else 0
    CURRENT["last_output"] = time.time()
    while True:
        rc = proc.poll()
        now = time.monotonic()
        try:
            stage = stage_file.read_text().strip().splitlines()[0][:120]
        except (OSError, IndexError):
            stage = None
        if stage and stage != CURRENT["stage"]:
            close_stage(now)
            CURRENT["timeline"].append({"stage": stage, "started_at": now_iso(), "duration_s": None, "_t0": now})
            CURRENT.update(stage=stage, stage_since=now_iso())
        if rc is not None:
            return rc
        size = log_path.stat().st_size if log_path.exists() else 0
        if size != last_size:
            last_size, CURRENT["last_output"] = size, time.time()
        silence = time.time() - CURRENT["last_output"]
        stalled = silence > SILENCE_WARN
        if stalled and not CURRENT["stalled"]:
            log.warning("build %s: no output for %ds (stage %s)", CURRENT["id"], silence, CURRENT["stage"])
        CURRENT["stalled"] = stalled
        if SILENCE_KILL and silence > SILENCE_KILL:
            say(f"no output for {int(silence)}s at stage «{CURRENT['stage']}» — killed (G9S_SILENCE_KILL={SILENCE_KILL})")
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            return None
        if now - t0 > BUILD_TIMEOUT:
            say(f"recipe killed after {BUILD_TIMEOUT}s")
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            return None
        time.sleep(MONITOR_STEP)


def live(bid: str) -> dict:
    """What is known about the running build right now (empty for any other build)."""
    if CURRENT["id"] != bid or CURRENT["log"] is None:
        return {}
    size = CURRENT["log"].stat().st_size if CURRENT["log"].exists() else 0
    silence = round(time.time() - (CURRENT["last_output"] or time.time()))
    return {"stage": CURRENT["stage"], "stage_since": CURRENT["stage_since"], "log_bytes": size,
            "silence_s": silence, "stalled": CURRENT["stalled"],
            "timeline": [{k: v for k, v in e.items() if k != "_t0"} for e in CURRENT["timeline"]]}


def stats(recipe: str | None) -> dict:
    """Medians over succeeded builds: whole build and each stage — «usually N minutes»."""
    durations, stages, n = [], {}, 0
    for bid in list_build_ids():
        meta = load_meta(bid)
        if not meta or meta.get("status") != "succeeded" or (recipe and meta["recipe"] != recipe):
            continue
        n += 1
        if meta.get("duration_s") is not None:
            durations.append(meta["duration_s"])
        for e in meta.get("timeline") or []:
            if e.get("duration_s") is not None:
                stages.setdefault(e["stage"], []).append(e["duration_s"])
        if n >= 30:
            break
    return {"recipe": recipe, "succeeded_builds": n,
            "median_duration_s": round(statistics.median(durations)) if durations else None,
            "stages": {k: {"median_s": round(statistics.median(v)), "n": len(v)} for k, v in stages.items()}}


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
                       G9S_SUBMITTED_BY=meta["submitted_by"], G9S_STAGE=str(bdir / "stage"))
            for k, v in meta["params"].items():
                env[f"G9S_PARAM_{k.upper()}"] = v
            say("recipe start")
            script = recipes().get(meta["recipe"])
            if script is None:
                raise RuntimeError(f"recipe {meta['recipe']} is no longer available")
            proc = subprocess.Popen(["bash", str(script)], cwd=str(src), env=env,
                                    stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
            ru0 = resource.getrusage(resource.RUSAGE_CHILDREN)
            CURRENT.update(id=bid, proc=proc, cancel=False, stage="start", stage_since=now_iso(), timeline=[],
                           log=bdir / "build.log", stalled=False, last_output=time.time())
            stage_file, stage_t0 = bdir / "stage", time.monotonic()
            rc = monitor(proc, stage_file, bdir / "build.log", say)
            if rc is None:
                status = "timeout"
            else:
                status = "cancelled" if CURRENT["cancel"] else ("succeeded" if rc == 0 else "failed")
            close_stage(time.monotonic())
            ru1 = resource.getrusage(resource.RUSAGE_CHILDREN)
            meta["cpu_s"] = round((ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime))
            meta["timeline"] = list(CURRENT["timeline"])
        except Exception as e:  # noqa: BLE001 - the log is the place for it
            say(f"runner error: {e!r}")
            status = "failed"
        finally:
            CURRENT.update(id=None, proc=None, cancel=False, stage=None, stage_since=None, timeline=[], log=None,
                           stalled=False, last_output=None)
            shutil.rmtree(src, ignore_errors=True)
            duration = round(time.monotonic() - started)
            say(f"finished: status={status} exit={rc} duration={duration}s")
    meta.update(status=status, exit_code=rc, finished_at=now_iso(), duration_s=duration, artifacts=scan_artifacts(out))
    for e in meta.get("timeline") or []:
        e.pop("_t0", None)
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

    def token_from_cookie(self) -> str | None:
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return c["g9s_token"].value if "g9s_token" in c else None

    def require_auth(self) -> str:
        who = authenticate(self.headers.get("Authorization"))
        if who is None and self.token_from_cookie():
            who = authenticate("Bearer " + self.token_from_cookie())
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
            running = None
            if CURRENT["id"]:
                running = {"id": CURRENT["id"], **{k: v for k, v in live(CURRENT["id"]).items() if k != "timeline"}}
            return self.send_json(200, {"status": "ok", "running": running, "queued": QUEUE.qsize(),
                                        "recipes": recipe_names(), "tokens_file_present": TOKENS_FILE.exists(),
                                        "repos": POLL_STATE})
        if path.startswith("/ui"):
            return self.ui(path, q)
        who = self.require_auth()

        if path == "/api/stats" and m == "GET":
            return self.send_json(200, stats(q.get("recipe")))

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
            return self.send_json(200, {**meta, **live(bid)})
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

    def send_html(self, status: int, body: str, headers: dict | None = None) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def ui(self, path: str, q: dict) -> None:
        if path == "/ui/login" and self.command == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(min(length, 4096)).decode(errors="replace"))
            token = (form.get("token") or [""])[0].strip()
            if authenticate("Bearer " + token) is None:
                return self.send_html(401, LOGIN_HTML.format(css=UI_CSS, msg="<p class=bad>Токен не принят.</p>"))
            return self.send_html(303, "", {"Location": "/ui",
                                            "Set-Cookie": f"g9s_token={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"})
        if path == "/ui/logout":
            return self.send_html(303, "", {"Location": "/ui", "Set-Cookie": "g9s_token=; Path=/; Max-Age=0"})
        who = authenticate("Bearer " + (self.token_from_cookie() or ""))
        if who is None:
            return self.send_html(200, LOGIN_HTML.format(css=UI_CSS, msg=""))
        mb = re.fullmatch(r"/ui/builds/([^/]+)", path)
        if mb:
            bid = mb.group(1)
            if not BUILD_ID_RE.match(bid) or not load_meta(bid):
                return self.send_html(404, "<p>Нет такой сборки.</p>")
            return self.send_html(200, BUILD_HTML.format(css=UI_CSS, id=html.escape(bid), idjs=json.dumps(bid)))
        if path == "/ui":
            rows = []
            for bid in list_build_ids()[:50]:
                meta = load_meta(bid)
                if not meta:
                    continue
                lv = live(bid)
                stage = lv.get("stage") or ""
                if lv.get("stalled"):
                    stage += f" <span class=bad>(нет вывода {lv['silence_s']} с)</span>"
                cls = "ok" if meta["status"] == "succeeded" else ("run" if meta["status"] in ("running", "queued") else "bad")
                rows.append(f"<tr><td><a href=/ui/builds/{bid}>{bid}</a></td><td>{html.escape(meta['recipe'])}</td>"
                            f"<td>{meta['commit'][:12]}</td><td>{html.escape(meta['submitted_by'])}</td>"
                            f"<td class={cls}>{meta['status']}</td><td>{stage}</td><td>{fmt_dur(meta.get('duration_s'))}</td></tr>")
            running = "Сейчас: ничего не собирается." if not CURRENT["id"] else \
                f"Сейчас: <a href=/ui/builds/{CURRENT['id']}>{CURRENT['id']}</a>, стадия «{html.escape(CURRENT['stage'] or '')}»; в очереди {QUEUE.qsize()}."
            st = []
            for r in recipe_names():
                s = stats(r)
                if s["median_duration_s"]:
                    st.append(f"{html.escape(r)}: обычно {fmt_dur(s['median_duration_s'])} (по {s['succeeded_builds']} сборкам)")
            return self.send_html(200, LIST_HTML.format(css=UI_CSS, who=html.escape(who), running=running,
                                                        rows="".join(rows), stats="; ".join(st)))
        return self.send_html(404, "<p>Нет такой страницы.</p>")

    def submit(self, q: dict, who: str) -> None:
        recipe, commit = q.get("recipe", ""), q.get("commit", "").lower()
        if recipe not in recipe_names():
            raise HttpError(400, "unknown_recipe", f"known: {', '.join(recipe_names())}")
        if not q.get("ref") and not SHA_RE.match(commit):
            raise HttpError(400, "bad_commit", "a full 40-hex commit id is required (or ref=… in pull mode)")
        params = {}
        for k, v in q.items():
            if k.startswith("p_"):
                key = k[2:]
                if not PARAM_RE.match(key) or len(v) > 256 or any(c in v for c in "\n\r\0"):
                    raise HttpError(400, "bad_param", k)
                params[key] = v
        ref = q.get("ref", "")
        if ref:
            # pull mode: the runner fetches the ref itself from the configured remote
            cfg = repo_config(recipe)
            if not cfg:
                raise HttpError(422, "repo_not_configured", f"no repo.{recipe} file on the builder")
            if not REF_RE.match(ref) or ".." in ref:
                raise HttpError(400, "bad_ref")
            try:
                commit = fetch_ref(recipe, cfg, ref)
            except ImportError_ as e:
                raise HttpError(422, e.code, e.detail)
            meta = enqueue(recipe, commit, params, who, f"ref:{ref}")
            return self.send_json(202, {"id": meta["id"], "commit": commit, "status": "queued", "position": QUEUE.qsize()})
        tmp = DATA / "builds" / f"upload-{uuid.uuid4().hex}.bundle"
        size = self.read_body_to(tmp)
        try:
            import_bundle(recipe, commit, tmp if size else None)
        except ImportError_ as e:
            raise HttpError(422, e.code, e.detail)
        finally:
            tmp.unlink(missing_ok=True)
        meta = enqueue(recipe, commit, params, who, "bundle", size)
        self.send_json(202, {"id": meta["id"], "status": "queued", "position": QUEUE.qsize()})


# ---------------------------------------------------------------- read-only UI

UI_CSS = """body{font:14px/1.4 system-ui,sans-serif;margin:1.5rem;color:#222;background:#fafafa}
table{border-collapse:collapse;width:100%}td,th{padding:.3rem .6rem;border-bottom:1px solid #ddd;text-align:left;
white-space:nowrap}th{background:#eee}a{color:#0645ad}pre{background:#111;color:#ddd;padding:1rem;overflow:auto;
max-height:70vh;font:12px/1.35 ui-monospace,monospace}.ok{color:#1a7f37}.bad{color:#b42318}.run{color:#9a6700}
.muted{color:#777}dl{display:grid;grid-template-columns:max-content 1fr;gap:.2rem 1rem}dt{color:#777}"""

LOGIN_HTML = """<!doctype html><meta charset=utf-8><title>g9s builder</title><style>{css}</style>
<h2>g9s builder</h2><p>Токен разработчика (хранится в cookie этого браузера):</p>
<form method=post action=/ui/login><input name=token size=56 autofocus> <button>Войти</button></form>{msg}"""

LIST_HTML = """<!doctype html><meta charset=utf-8><title>g9s builder</title><style>{css}</style>
<h2>g9s builder <span class=muted>— {who}</span> <a href=/ui/logout class=muted style="font-size:.8em">выйти</a></h2>
<p>{running}</p><table><tr><th>сборка</th><th>рецепт</th><th>коммит</th><th>кто</th><th>статус</th><th>стадия</th>
<th>длительность</th></tr>{rows}</table><p class=muted>{stats}</p>"""

BUILD_HTML = """<!doctype html><meta charset=utf-8><title>{id}</title><style>{css}</style>
<p><a href=/ui>← сборки</a></p><h2>{id}</h2><dl id=meta></dl><h3>Артефакты</h3><ul id=art></ul>
<h3>Журнал <span id=tail class=muted></span></h3><pre id=log></pre>
<script>
const id={idjs};let off=0,done=false;
function cls(s){{return s=='succeeded'?'ok':(s=='running'||s=='queued')?'run':'bad'}}
async function tick(){{
  const m=await (await fetch('/api/builds/'+id)).json();
  const tl=(m.timeline||[]).map(e=>e.stage+(e.duration_s!=null?' ('+e.duration_s+' с)':' …')).join(' → ');
  document.getElementById('meta').innerHTML=
   `<dt>статус</dt><dd class=${{cls(m.status)}}>${{m.status}}${{m.stalled?' — нет вывода '+m.silence_s+' с':''}}</dd>`+
   `<dt>рецепт</dt><dd>${{m.recipe}}</dd><dt>коммит</dt><dd>${{m.commit}}</dd><dt>кто</dt><dd>${{m.submitted_by}}</dd>`+
   `<dt>стадия</dt><dd>${{m.stage||'—'}}</dd><dt>стадии</dt><dd>${{tl||'—'}}</dd>`+
   `<dt>начало</dt><dd>${{m.started_at||'—'}}</dd><dt>длительность</dt><dd>${{m.duration_s!=null?m.duration_s+' с':'—'}}</dd>`;
  document.getElementById('art').innerHTML=(m.artifacts||[]).map(a=>`<li><a href="/api/builds/${{id}}/artifacts/${{a.name}}">${{a.name}}</a> <span class=muted>${{a.size}} B · ${{a.sha256.slice(0,12)}}</span></li>`).join('')||'<li class=muted>нет</li>';
  const r=await fetch('/api/builds/'+id+'/log?from='+off);const txt=await r.text();
  if(txt){{const pre=document.getElementById('log');const stick=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-20;
    pre.textContent+=txt;off=parseInt(r.headers.get('X-Log-Size')||off);if(stick)pre.scrollTop=pre.scrollHeight;}}
  document.getElementById('tail').textContent=off+' B';
  done=['succeeded','failed','cancelled','timeout'].includes(m.status);
  if(!done)setTimeout(tick,3000);
}}
tick();
</script>"""


def fmt_dur(s) -> str:
    if s is None:
        return "—"
    return f"{s // 60} мин {s % 60} с" if s >= 60 else f"{s} с"


def main() -> None:
    (DATA / "builds").mkdir(parents=True, exist_ok=True)
    (DATA / "repos").mkdir(parents=True, exist_ok=True)
    recover_on_start()
    threading.Thread(target=worker, name="builder", daemon=True).start()
    threading.Thread(target=poller, name="poller", daemon=True).start()
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
