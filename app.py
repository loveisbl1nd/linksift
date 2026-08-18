import contextlib
import glob
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_file

import output_pipeline as pipeline

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
DEFAULT_DOWNLOAD_TIMEOUT = 3600
DEFAULT_MAX_CONCURRENT_DOWNLOADS = 3
MAX_CONCURRENT_DOWNLOADS = 16
DEFAULT_MAX_QUEUED_DOWNLOADS = 200
DEFAULT_CONCURRENT_FRAGMENTS = 4
MAX_CONCURRENT_FRAGMENTS = 16
DEFAULT_JOB_TTL = 86400
DEFAULT_MAX_PLAYLIST_ITEMS = 200
DEFAULT_JOB_RETRIES = 2
MAX_JOB_RETRIES = 5
DEFAULT_RETRY_BASE_DELAY = 2
MAX_RETRY_BACKOFF_SECONDS = 15
CLEANUP_INTERVAL_SECONDS = 600
PROGRESS_PREFIX = "__LINKSIFT_PROGRESS__"
POSTPROCESS_PREFIX = "__LINKSIFT_POSTPROCESS__"
TERMINAL_STATUSES = frozenset({"done", "error", "cancelled", "timed_out", "partial"})

# Artifact phases that describe an artifact still doing work. A parent job
# mirrors these onto its own `phase` so the UI can distinguish "starting" from
# "downloading" from "retrying". "postprocessing" is accepted for forward
# compatibility; the current pipeline reports that stage as "processing".
ACTIVE_ARTIFACT_PHASES = frozenset(
    {"starting", "downloading", "retrying", "processing", "postprocessing"}
)
ACTIVE_STATUSES = frozenset({"downloading", "cancelling"})
JOB_FILE_NAME_PATTERN = re.compile(r"^[0-9a-f]{10}(\.a[0-9]+)?\.")

def artifact_paths(job_id, artifact_id):
    """Get all file paths for a specific artifact."""
    pattern = os.path.join(DOWNLOAD_DIR, f"{job_id}.{artifact_id}*")
    return glob.glob(pattern)

def cleanup_artifact_files(job_id, artifact_id, include_final=False):
    """Remove files belonging to a specific artifact."""
    for path in artifact_paths(job_id, artifact_id):
        if not include_final and not is_intermediate_file(path):
            continue
        try:
            os.remove(path)
        except OSError:
            pass

def job_paths(job_id):
    """Every file on disk belonging to a job: artifacts and intermediates alike."""
    return glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))


# Single shared implementation, re-exported so existing call sites and tests can
# keep using app.is_intermediate_file. See output_pipeline.is_intermediate_file.
is_intermediate_file = pipeline.is_intermediate_file

jobs = {}
processes = {}
# client_request_id -> {"job_id", "fingerprint"}. Guarded by jobs_lock, and
# entered in the SAME critical section that registers the job, so two concurrent
# retries of one user action can never both reach the scheduler. Entries are
# removed by run_cleanup together with the job they point at, so this map can
# never outlive the jobs dict.
request_ids = {}
jobs_lock = threading.Lock()
cleanup_lock = threading.Lock()
last_cleanup_monotonic = None
scheduler = None
scheduler_guard = threading.Lock()


def get_missing_runtime_tools(required_tools):
    return [tool for tool in required_tools if shutil.which(tool) is None]


def runtime_unavailable_response(required_tools):
    missing_tools = get_missing_runtime_tools(required_tools)
    if not missing_tools:
        return None
    tool_list = ", ".join(missing_tools)
    return jsonify({
        "error": f"Server downloader is unavailable. Start LinkSift with Docker Compose or install: {tool_list}.",
        "missing_tools": missing_tools,
    }), 503


def subprocess_unavailable_response(required_tools):
    tool_list = ", ".join(required_tools)
    return jsonify({
        "error": f"Server downloader is unavailable. Start LinkSift with Docker Compose or install: {tool_list}.",
        "missing_tools": list(required_tools),
    }), 503


def get_positive_int(name, default):
    """Return a positive integer environment setting or its default."""
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def get_download_timeout():
    return get_positive_int("LINKSIFT_DOWNLOAD_TIMEOUT", DEFAULT_DOWNLOAD_TIMEOUT)


def get_max_concurrent_downloads():
    """Download worker slots, clamped to 1..16 so an environment typo cannot
    ask the scheduler for thousands of threads."""
    value = get_positive_int("LINKSIFT_MAX_CONCURRENT_DOWNLOADS", DEFAULT_MAX_CONCURRENT_DOWNLOADS)
    return min(value, MAX_CONCURRENT_DOWNLOADS)


def get_max_queued_downloads():
    return get_positive_int("LINKSIFT_MAX_QUEUED_DOWNLOADS", DEFAULT_MAX_QUEUED_DOWNLOADS)


def get_concurrent_fragments():
    """Fragment parallelism inside one yt-dlp download, clamped to 1..16."""
    value = get_positive_int("LINKSIFT_CONCURRENT_FRAGMENTS", DEFAULT_CONCURRENT_FRAGMENTS)
    return min(value, MAX_CONCURRENT_FRAGMENTS)


def get_job_retries():
    """Extra fresh-extraction attempts after the first one (0..5)."""
    raw = os.environ.get("LINKSIFT_JOB_RETRIES")
    if raw is None:
        return DEFAULT_JOB_RETRIES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_JOB_RETRIES
    if value < 0 or value > MAX_JOB_RETRIES:
        return DEFAULT_JOB_RETRIES
    return value


def get_retry_base_delay():
    """Seconds before the first retry; doubles per retry, capped elsewhere."""
    raw = os.environ.get("LINKSIFT_RETRY_BASE_DELAY")
    if raw is None:
        return DEFAULT_RETRY_BASE_DELAY
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_BASE_DELAY
    if not math.isfinite(value) or value < 0:
        return DEFAULT_RETRY_BASE_DELAY
    return value


def retry_backoff_delay(retry_index, base_delay):
    """Exponential backoff before the Nth retry (1-based), capped."""
    return min(base_delay * (2 ** (retry_index - 1)), MAX_RETRY_BACKOFF_SECONDS)


def get_po_token_provider_url():
    """Validated PO Token provider base URL from the environment, or None."""
    raw = os.environ.get("LINKSIFT_PO_TOKEN_PROVIDER_URL", "").strip()
    if not raw:
        return None
    if any(char in raw for char in ";, \t\r\n"):
        app.logger.warning("Ignoring LINKSIFT_PO_TOKEN_PROVIDER_URL: value contains unsafe characters")
        return None
    try:
        parsed = urlparse(raw)
    except ValueError:
        parsed = None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.hostname:
        app.logger.warning("Ignoring LINKSIFT_PO_TOKEN_PROVIDER_URL: expected an http(s) URL with a hostname")
        return None
    if parsed.username or parsed.password:
        # Credentials would leak into yt-dlp argv and diagnostics output.
        app.logger.warning("Ignoring LINKSIFT_PO_TOKEN_PROVIDER_URL: credentials in the URL are not supported")
        return None
    return raw


def get_pot_plugin_version():
    """Installed bgutil PO token plugin version, or None. Metadata lookup
    only: the plugin is never imported and no network request is made."""
    try:
        return importlib.metadata.version("bgutil-ytdlp-pot-provider")
    except importlib.metadata.PackageNotFoundError:
        return None


def has_pot_plugin():
    return get_pot_plugin_version() is not None


_pot_plugin_warning_emitted = False


def ytdlp_runtime_args():
    """Extractor/runtime arguments shared by every yt-dlp invocation.

    The provider arguments are only emitted when the URL is valid AND the
    plugin is actually installed; otherwise they would be dead options."""
    global _pot_plugin_warning_emitted
    provider_url = get_po_token_provider_url()
    if not provider_url:
        return []
    if not has_pot_plugin():
        if not _pot_plugin_warning_emitted:
            _pot_plugin_warning_emitted = True
            app.logger.warning(
                "LINKSIFT_PO_TOKEN_PROVIDER_URL is set but the bgutil-ytdlp-pot-provider "
                "plugin is not installed — ignoring the provider. This usually means the "
                "default image is running; build the youtube-robust target instead."
            )
        return []
    return ["--extractor-args", f"youtubepot-bgutilhttp:base_url={provider_url}"]


def has_js_runtime():
    return shutil.which("deno") is not None


def has_ejs_support():
    return importlib.util.find_spec("yt_dlp_ejs") is not None


def get_job_ttl():
    return get_positive_int("LINKSIFT_JOB_TTL", DEFAULT_JOB_TTL)


def get_max_playlist_items():
    return get_positive_int("LINKSIFT_MAX_PLAYLIST_ITEMS", DEFAULT_MAX_PLAYLIST_ITEMS)


def active_download_count():
    """Count jobs still holding a slot: cancelling keeps its slot until the
    download thread finalizes the job, because the process may still run."""
    return sum(job.get("status") in ACTIVE_STATUSES for job in jobs.values())


def get_request_data():
    """Return a JSON object request body or a stable validation error."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({"error": "Request body must be a JSON object"}), 400)
    return data, None


def get_url(data):
    url = data.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    return url.strip()


# A UUID with room to spare for other client-side id schemes, restricted to
# characters that are safe to place in a log line unescaped.
CLIENT_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

# Free-form values are rejected rather than logged: the launch source is a
# closed set the frontend owns, and an unknown value means a client bug or a
# forged request, not a new launch mode.
LAUNCH_SOURCES = frozenset({"single-card", "download-all"})


def get_client_request_id(data):
    """Validate the optional ``client_request_id``.

    Returns ``(value_or_None, error_message_or_None)``. A request that omits the
    field is valid and gets no idempotency protection, which is exactly how
    clients predating this field keep working.
    """
    if "client_request_id" not in data:
        return None, None
    value = data.get("client_request_id")
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "client_request_id must be a string"
    value = value.strip()
    if not CLIENT_REQUEST_ID_PATTERN.match(value):
        return None, (
            "client_request_id must be 8-128 characters of letters, digits, '-' or '_'"
        )
    return value, None


def get_launch_source(data):
    """Validate the optional ``launch_source``; returns (value, error)."""
    if "launch_source" not in data:
        return None, None
    value = data.get("launch_source")
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "launch_source must be a string"
    value = value.strip()
    if value not in LAUNCH_SOURCES:
        return None, "launch_source is not a recognized value"
    return value, None


def download_request_fingerprint(url, title, outputs):
    """Stable digest of what a download request actually asks for.

    Two requests carrying the same ``client_request_id`` are the same launch
    only if they ask for the same thing; a reused id with a different payload is
    a client bug worth reporting rather than silently answering with the wrong
    job. Hashed rather than stored raw so the URL and title never sit in a
    second in-memory structure.
    """
    canonical = json.dumps(
        {
            "url": url,
            "title": title,
            "outputs": [
                {"type": o.get("type"), "format_id": o.get("format_id")} for o in outputs
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_ytdlp_json(stdout):
    """Return the first valid JSON object emitted by yt-dlp."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("yt-dlp returned no JSON object")


def finite_number(value, default=None):
    if isinstance(value, bool):
        return default
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def parse_progress_payload(line, prefix):
    """Return the JSON object carried by a prefixed progress line, else None."""
    if not line.startswith(prefix):
        return None
    try:
        payload = json.loads(line[len(prefix):])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def update_job_progress(job, line):
    """Parse one yt-dlp download progress line and update a job in place."""
    progress = parse_progress_payload(line, PROGRESS_PREFIX)
    if progress is None:
        return False

    downloaded = max(0, finite_number(progress.get("downloaded_bytes"), 0))
    total = finite_number(progress.get("total_bytes"))
    if total is None:
        total = finite_number(progress.get("total_bytes_estimate"))
    if total is not None:
        total = max(0, total)

    speed = finite_number(progress.get("speed"))
    eta = finite_number(progress.get("eta"))
    percent = round(min(100, downloaded * 100 / total), 1) if total else None
    with jobs_lock:
        # Terminal states are sticky: a late progress line is consumed but ignored.
        if job.get("status") in TERMINAL_STATUSES:
            return True
        job.update({
            "phase": "downloading",
            "downloaded_bytes": downloaded,
            "total_bytes": total,
            "speed": speed,
            "eta": eta,
            "percent": percent,
        })
    return True


def update_job_postprocess(job, line):
    """Parse one yt-dlp postprocess progress line and mark the job as processing."""
    if parse_progress_payload(line, POSTPROCESS_PREFIX) is None:
        return False
    with jobs_lock:
        if job.get("status") in TERMINAL_STATUSES:
            return True
        job.update({
            "phase": "processing",
            "speed": None,
            "eta": None,
        })
    return True


def read_ffmpeg_progress(job_id, artifact, stream, diagnostics):
    """Consume ffmpeg's merged output, publishing packaging progress live.

    Runs on a daemon thread while ffmpeg works, so the artifact's percent moves
    during the packaging step instead of jumping from nothing to 100 when the
    process finally exits.

    The stream is drained all the way to EOF even after the job goes terminal:
    an unread pipe blocks ffmpeg's next write, and a blocked ffmpeg cannot be
    reaped. Once terminal, lines are consumed and discarded rather than applied,
    which is the same sticky-terminal rule ``update_job_progress`` follows.

    Duration comes from ffmpeg's own input dump (``Duration: HH:MM:SS.ss``) on
    the merged stream, so no ffprobe is spawned and there is no second process
    to gate, register, reap or race against.
    """
    state = {"duration": None, "processed": None, "speed": None, "percent": None}

    def publish():
        percent = pipeline.compute_processing_percent(
            state["processed"], state["duration"], state["percent"]
        )
        # Remembered so a later block can never publish a lower number.
        state["percent"] = percent
        eta = pipeline.compute_processing_eta(
            state["processed"], state["duration"], state["speed"]
        )
        with jobs_lock:
            job = jobs.get(job_id)
            if job is None or job.get("status") in TERMINAL_STATUSES:
                return
            if artifact.get("status") in pipeline.ARTIFACT_TERMINAL:
                return
            artifact.update({
                "status": "processing",
                "phase": "processing",
                "duration_seconds": state["duration"],
                "processed_seconds": state["processed"],
                "percent": percent,
                "eta": eta,
                "processing_speed": state["speed"],
            })

    try:
        for raw_line in stream:
            line = raw_line.rstrip("\r\n")
            if state["duration"] is None:
                duration = pipeline.parse_ffmpeg_duration(line)
                if duration is not None:
                    state["duration"] = duration
                    # Published immediately so the UI can leave the
                    # indeterminate state as soon as the length is known.
                    publish()
                    continue
            parsed = pipeline.parse_ffmpeg_progress_line(line)
            if parsed is None:
                text = line.strip()
                if text:
                    diagnostics.append(text)
                continue
            key, value = parsed
            if key in pipeline.FFMPEG_TIME_KEYS:
                seconds = pipeline.ffmpeg_progress_seconds(key, value)
                if seconds is not None and (
                    state["processed"] is None or seconds >= state["processed"]
                ):
                    state["processed"] = seconds
            elif key == "speed":
                speed = pipeline.parse_ffmpeg_speed(value)
                if speed is not None:
                    state["speed"] = speed
            elif key == "progress":
                # ffmpeg terminates every progress block with this key, so one
                # publish per block keeps the lock traffic bounded.
                publish()
    except (OSError, ValueError):
        # A pipe torn down by terminate_and_reap is an expected end, not a
        # failure: the waiting thread owns the outcome, this thread just stops.
        pass


def terminate_process_tree(process):
    """Best-effort termination of a subprocess and its children (ffmpeg)."""
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.kill()
    except OSError:
        pass


def cancel_requested(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        return bool(job and job.get("cancel_requested"))


# A fresh yt-dlp run performs a new extraction (new signed media URLs and PO
# tokens), which is exactly what these transient failures need.
TRANSIENT_DOWNLOAD_ERROR_MARKERS = (
    "http error 403",
    "http error 429",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "connection reset",
    "remote end closed connection",
    "temporary failure in name resolution",
    "getaddrinfo failed",
    "timed out",
)

# Failures a re-run cannot fix; checked before the transient markers.
PERMANENT_DOWNLOAD_ERROR_MARKERS = (
    "unsupported url",
    "is not a valid url",
    "video unavailable",
    "private video",
    "this video is private",
    "has been removed",
    "requested format is not available",
    "sign in to confirm",
    "login required",
    "--cookies",
    "members",
)


def is_transient_download_error(stderr_text):
    """Decide whether a failed yt-dlp attempt deserves a fresh extraction."""
    text = (stderr_text or "").lower()
    if not text:
        return False
    if any(marker in text for marker in PERMANENT_DOWNLOAD_ERROR_MARKERS):
        return False
    return any(marker in text for marker in TRANSIENT_DOWNLOAD_ERROR_MARKERS)


# YouTube's default player client hands out media URLs that its CDN then rejects
# with 403 for the whole stream, while the embedded client's URLs download fine.
# Only used as a *fallback*: some videos disable embedding, so forcing it on the
# first attempt would break downloads that currently work.
YOUTUBE_FALLBACK_CLIENT = "web_embedded"

_YOUTUBE_HOSTS = frozenset({"youtube.com", "youtu.be"})


def is_youtube_url(url):
    """True only when the URL's *hostname* is YouTube.

    The hostname is parsed with urllib.parse rather than substring-matched, so
    ``https://youtube.com.evil.example/x`` and ``https://notyoutube.com/x`` are
    correctly rejected: an attacker-controlled host must never be able to steer
    our extractor arguments. Subdomains (``www.``, ``m.``, ``music.``) are
    accepted, malformed input returns False instead of raising.
    """
    try:
        hostname = urlparse(url or "").hostname
    except ValueError:
        return False
    if not hostname:
        return False
    hostname = hostname.lower().rstrip(".")
    for base in _YOUTUBE_HOSTS:
        if hostname == base or hostname.endswith("." + base):
            return True
    return False


def is_http_403_error(stderr_text):
    """True when yt-dlp's output genuinely reports an HTTP 403.

    Deliberately narrower than is_transient_download_error: only a real 403
    justifies switching the player client. 429/503/connection resets are still
    handled by the ordinary same-client retry.
    """
    text = (stderr_text or "").lower()
    if not text:
        return False
    return "http error 403" in text or "403: forbidden" in text


def should_retry_with_youtube_fallback(url, stderr_text, job_id, deadline, already_used):
    """Decide whether the *next* attempt should force the embedded client.

    Every guard the ordinary retry path honours is re-checked here, because this
    decision is made after a subprocess returned and the world may have moved
    on: the job may have been cancelled, reached a terminal status, or run out
    of shared deadline while yt-dlp was running. Returning False leaves the
    existing retry semantics completely untouched.
    """
    if already_used:
        return False
    if not is_youtube_url(url):
        return False
    if not is_http_403_error(stderr_text):
        return False
    if cancel_requested(job_id):
        return False
    if time.monotonic() >= deadline:
        return False
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None or job.get("status") in TERMINAL_STATUSES:
            return False
    return True


def wait_before_retry(job, delay, deadline):
    """Interruptible backoff wait between download attempts.

    Returns False when the job is cancelled or the total deadline expires
    before the delay elapses; True when the next attempt may start."""
    cancel_event = job.get("cancel_event")
    wait_until = time.monotonic() + delay
    while True:
        if job.get("cancel_requested"):
            return False
        now = time.monotonic()
        if now >= deadline:
            return False
        if now >= wait_until:
            return True
        slice_seconds = min(wait_until, deadline) - now
        if cancel_event is not None:
            if cancel_event.wait(timeout=slice_seconds):
                return False
        else:
            time.sleep(min(slice_seconds, 0.05))


class DownloadScheduler:
    """Fixed pool of daemon workers draining a bounded FIFO queue.

    All workers are started eagerly by start(), which rolls back completely
    (shuts down and joins any workers it already started) if a thread cannot
    be created, so a half-initialized pool is never left running. submit()
    is a pure queue operation and cannot raise.

    Lock ordering: jobs_lock -> scheduler_guard -> self._condition. Callers
    may hold jobs_lock while calling scheduler methods; scheduler methods
    never take jobs_lock themselves. When claim_lock is set (jobs_lock in
    production), workers acquire it BEFORE self._condition to pop a task and
    run the on_claim hook in one atomic step, so a reader holding claim_lock
    can never observe a job that is neither queued nor claimed. Both locks
    are released before the task itself runs.
    """

    def __init__(self, worker_count, queue_limit, thread_factory=None, claim_lock=None, on_claim=None, on_claim_error=None):
        self.worker_count = min(max(1, worker_count), MAX_CONCURRENT_DOWNLOADS)
        self.queue_limit = max(1, queue_limit)
        self._thread_factory = thread_factory or threading.Thread
        self._claim_lock = claim_lock
        self._on_claim = on_claim
        self._on_claim_error = on_claim_error
        self._condition = threading.Condition()
        self._queue = deque()
        self._workers = []
        self._shutdown = False

    def start(self):
        """Start every worker up front; on any failure roll back and re-raise."""
        if self._workers:
            return self
        try:
            for index in range(self.worker_count):
                worker = self._thread_factory(
                    target=self._worker_loop,
                    name=f"linksift-download-worker-{index + 1}",
                    daemon=True,
                )
                worker.start()
                self._workers.append(worker)
        except Exception:
            self.shutdown()
            raise
        return self

    def submit(self, job_id, task):
        """Queue a task; returns False when the queue is full or shut down."""
        with self._condition:
            if self._shutdown or len(self._queue) >= self.queue_limit:
                return False
            self._queue.append((job_id, task))
            self._condition.notify()
            return True

    def cancel_queued(self, job_id):
        """Remove a still-queued task; returns False once a worker owns it."""
        with self._condition:
            for index, (queued_id, _) in enumerate(self._queue):
                if queued_id == job_id:
                    del self._queue[index]
                    return True
            return False

    def queue_position(self, job_id):
        """1-based position among waiting tasks, or None when not queued."""
        with self._condition:
            for index, (queued_id, _) in enumerate(self._queue):
                if queued_id == job_id:
                    return index + 1
            return None

    def queued_count(self):
        with self._condition:
            return len(self._queue)

    def _claim_next(self):
        """Atomically pop the next task and run the claim hook.

        Holding claim_lock across the pop and the hook is what removes the
        dequeue/status race: under that lock a job is either still in the
        queue (status queued, position >= 1) or already claimed (status no
        longer queued). A raising claim hook is handled here too: the
        on_claim_error hook finalizes the job under the SAME locks, so the
        invariant survives hook exceptions; logging happens only after the
        locks are released. Returns None when there is nothing to run.
        """
        outer = self._claim_lock if self._claim_lock is not None else contextlib.nullcontext()
        claim_error = None
        error_hook_failure = None
        job_id = None
        with outer:
            with self._condition:
                if self._shutdown or not self._queue:
                    return None
                job_id, task = self._queue.popleft()
                if self._on_claim is not None:
                    try:
                        if not self._on_claim(job_id):
                            return None
                    except Exception as exc:
                        claim_error = exc
                        if self._on_claim_error is not None:
                            try:
                                self._on_claim_error(job_id, exc)
                            except Exception as hook_exc:
                                error_hook_failure = hook_exc
        if claim_error is not None:
            app.logger.error(
                "Claim hook failed; job %s was marked as failed without starting",
                job_id,
                exc_info=claim_error,
            )
            if error_hook_failure is not None:
                app.logger.error(
                    "Claim-error hook itself failed for job %s",
                    job_id,
                    exc_info=error_hook_failure,
                )
            return None
        return job_id, task

    def _worker_loop(self):
        while True:
            with self._condition:
                while not self._queue and not self._shutdown:
                    self._condition.wait()
                if self._shutdown:
                    return
            try:
                claimed = self._claim_next()
            except Exception:
                # Last line of defense: a worker must never die.
                app.logger.exception("Download worker failed while claiming a task")
                continue
            if claimed is None:
                continue
            _, task = claimed
            try:
                task()
            except Exception:
                app.logger.exception("Download worker task failed")

    def shutdown(self, wait_seconds=5):
        """Stop workers and drop queued tasks. Never call under jobs_lock."""
        with self._condition:
            self._shutdown = True
            self._queue.clear()
            self._condition.notify_all()
        for worker in self._workers:
            worker.join(timeout=wait_seconds)


def claim_download_job(job_id):
    """Scheduler claim hook; runs under jobs_lock and the scheduler lock.

    Returns False to drop the task without running it. Must never leave a
    popped job in status "queued": /api/status treats queued as "still in
    the scheduler queue" when computing queue_position.
    """
    job = jobs.get(job_id)
    if job is None or job.get("status") in TERMINAL_STATUSES:
        return False
    if job.get("cancel_requested"):
        # run_download finalizes the cancellation without spawning yt-dlp.
        if job.get("status") == "queued":
            job.update({"status": "cancelling", "phase": "cancelling"})
        return True
    job.update({
        "status": "downloading",
        "phase": "starting",
        "started_at": time.time(),
    })
    return True


def fail_claimed_job(job_id, exc):
    """Claim-error hook; runs under jobs_lock and the scheduler lock.

    Must not re-acquire jobs_lock (it is not reentrant), log, or do I/O.
    The exception stays out of the job: clients get a stable message."""
    job = jobs.get(job_id)
    if job is None or job.get("status") in TERMINAL_STATUSES:
        return
    job.update({
        "status": "error",
        "phase": "error",
        "error": "Download could not be started",
        "speed": None,
        "eta": None,
        "percent": None,
        "finished_at": time.time(),
    })


def get_scheduler():
    """Return the shared scheduler, creating and starting it from current
    settings. The global is only published after start() succeeds, so a
    failed startup leaves it None and the next request retries."""
    global scheduler
    with scheduler_guard:
        if scheduler is None:
            scheduler = DownloadScheduler(
                get_max_concurrent_downloads(),
                get_max_queued_downloads(),
                claim_lock=jobs_lock,
                on_claim=claim_download_job,
                on_claim_error=fail_claimed_job,
            ).start()
        return scheduler


def reset_scheduler():
    """Shut down and drop the scheduler; intended for tests. Never call under jobs_lock."""
    global scheduler
    with scheduler_guard:
        current, scheduler = scheduler, None
    if current is not None:
        current.shutdown()


class PipelineCancelled(Exception):
    """Raised when a parent job was cancelled while an artifact was running.

    Distinct from an ordinary failure on purpose: an ordinary failure may fall
    back to another strategy, a cancellation may not. Falling back would spawn a
    brand new subprocess for a job the user already stopped.
    """


def remove_quietly(path):
    """Best-effort unlink; a missing file is success, not an error."""
    try:
        os.remove(path)
    except OSError:
        pass


def close_quietly(stream):
    """Best-effort close of a subprocess pipe.

    ``Popen.wait()`` does not close the pipes ``communicate()`` would have, so
    without this every packaged artifact leaks one file descriptor.
    """
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def unregister_process_if_current(parent_job_id, process):
    """Drop the registry entry only while it still points at `process`.

    An unconditional pop is a use-after-free in disguise: a slow `finally` from
    a finished artifact would evict the process the NEXT artifact just
    registered, and DELETE would then find nothing to signal.
    """
    with jobs_lock:
        if processes.get(parent_job_id) is process:
            processes.pop(parent_job_id, None)


def terminate_and_reap(process, timeout=5):
    """Kill the whole process tree and reap it, so no pid outlives the artifact.

    Without the reap the next artifact can start while the previous ffmpeg or
    yt-dlp is still holding its output file open.
    """
    if process is None:
        return
    terminate_process_tree(process)
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def gate_and_spawn(parent_job_id, cmd, progress_target=None, deadline=None, **popen_kwargs):
    """Decide, spawn and register in ONE jobs_lock critical section.

    Returns the Popen, or None when this job must not spawn at all. Raises
    RuntimeError when the parent already owns a live subprocess, because
    overwriting the registry would orphan it beyond DELETE's reach.

    The lock is released before the process is waited on: it guards the
    decision and the bookkeeping, never the download itself.
    """
    with jobs_lock:
        job = jobs.get(parent_job_id)
        if job is None:
            return None
        if job.get("cancel_requested"):
            return None
        if job.get("status") in TERMINAL_STATUSES:
            return None
        if (
            progress_target is not None
            and progress_target is not job
            and progress_target.get("status") in pipeline.ARTIFACT_TERMINAL
        ):
            # The artifact was cancelled or timed out while awaiting its turn.
            return None
        if deadline is not None and deadline - time.monotonic() <= 0:
            return None
        existing = processes.get(parent_job_id)
        if existing is not None and existing.poll() is None:
            raise RuntimeError(
                f"parent job {parent_job_id} already has an active subprocess "
                f"(pid {existing.pid}); refusing to overwrite the registry"
            )
        process = subprocess.Popen(cmd, **popen_kwargs)
        processes[parent_job_id] = process
        return process


def run_download_command(cmd, parent_job_id, progress_target, timeout):
    """Run yt-dlp while streaming machine-readable progress into progress_target.

    Args:
        cmd: Command to execute
        parent_job_id: Parent job ID for process registry and cancellation
        progress_target: Job or artifact dict to update with progress
        timeout: Timeout in seconds

    Returns None without spawning anything when the job was already
    cancelled. The cancellation check, process creation and registry insert
    happen in ONE jobs_lock critical section, so a concurrent DELETE either
    runs first (no process is ever spawned) or runs after (it sees the
    registered process and terminates it). The lock is never held while the
    process runs or its output is read."""
    popen_kwargs = {}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    process = gate_and_spawn(
        parent_job_id,
        cmd,
        progress_target=progress_target,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
        **popen_kwargs,
    )
    if process is None:
        return None
    diagnostics = deque(maxlen=200)

    def read_output():
        for raw_line in process.stdout:
            line = raw_line.rstrip()
            if update_job_progress(progress_target, line) or update_job_postprocess(progress_target, line):
                continue
            diagnostics.append(line)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_and_reap(process)
        raise
    finally:
        reader.join(timeout=5)
        unregister_process_if_current(parent_job_id, process)

    return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="\n".join(diagnostics))


def cleanup_job_files(job_id, include_final=False):
    """Remove only intermediate files belonging to a failed job."""
    for path in job_paths(job_id):
        if not include_final and not is_intermediate_file(path):
            continue
        try:
            os.remove(path)
        except OSError:
            pass


def select_output_file(job_id, format_choice):
    extension = ".mp3" if format_choice == "audio" else ".mp4"
    exact_output = os.path.join(DOWNLOAD_DIR, f"{job_id}{extension}")
    if os.path.isfile(exact_output):
        return exact_output

    candidates = [
        path
        for path in job_paths(job_id)
        if os.path.isfile(path)
        and not is_intermediate_file(path)
        and path.lower().endswith(extension)
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def safe_download_name(title, path):
    extension = Path(path).suffix.lower()
    if not isinstance(title, str):
        title = ""
    safe_title = "".join(
        char for char in title
        if char.isprintable() and char not in r'\\/:*?"<>|'
    ).strip(". ")[:100]
    return f"{safe_title}{extension}" if safe_title else os.path.basename(path)


def finish_job_cancelled(job_id):
    """Remove every file of a cancelled job and mark it terminal."""
    cleanup_job_files(job_id, include_final=True)
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return
        job.update({
            "status": "cancelled",
            "phase": "cancelled",
            "speed": None,
            "eta": None,
            "percent": None,
            "error": None,
            "finished_at": time.time(),
        })


def set_error(job_id, message, status="error"):
    """Finalize a failed job; a pending cancellation always wins."""
    was_cancelled = False
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return
        was_cancelled = bool(job.get("cancel_requested"))
        if not was_cancelled:
            job.update({
                "status": status,
                "phase": status,
                "speed": None,
                "eta": None,
                "error": message,
                "finished_at": time.time(),
            })
    if was_cancelled:
        finish_job_cancelled(job_id)


def run_pipeline(job_id, url, title):
    """Execute multi-artifact download pipeline sequentially.

    Each artifact runs with the same deadline and cancellation checks.
    Videos run before audio to enable MP4->MP3 reuse.
    """
    timeout = get_download_timeout()
    deadline = time.monotonic() + timeout

    with jobs_lock:
        job = jobs.get(job_id)
        if job is None or job.get("status") in TERMINAL_STATUSES:
            return
        cancelled_before_start = bool(job.get("cancel_requested"))
        if not cancelled_before_start:
            job.update({
                "status": "downloading",
                "phase": "starting",
                "started_at": time.time(),
            })

    if cancelled_before_start:
        finish_pipeline_cancelled(job_id)
        return

    try:
        execute_artifacts_sequentially(job_id, url, title, deadline)
    except subprocess.TimeoutExpired:
        if cancel_requested(job_id):
            finish_pipeline_cancelled(job_id)
            return
        set_pipeline_error(job_id, f"Download timed out after {timeout} seconds", status="timed_out")
    except PipelineCancelled:
        # A cancellation surfaced from inside a step (e.g. ffmpeg reuse). This
        # is fatal and must never fall back; finalize as cancelled.
        finish_pipeline_cancelled(job_id)
    except FileNotFoundError:
        set_pipeline_error(job_id, "Server downloader became unavailable. Restart LinkSift with Docker Compose.")
    except Exception:
        app.logger.exception("Pipeline failed unexpectedly for job %s", job_id)
        if cancel_requested(job_id):
            finish_pipeline_cancelled(job_id)
            return
        set_pipeline_error(job_id, "Download failed unexpectedly")


def execute_artifacts_sequentially(job_id, url, title, deadline):
    """Run each artifact in execution order, checking cancellation and deadline."""
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return
        execution_order = job.get("execution_order", [])
        artifacts_map = {art["id"]: art for art in job.get("artifacts", [])}
        job_title = job.get("title", "")

    # Track completed videos for MP3 reuse
    completed_videos = []

    for artifact in execution_order:
        if cancel_requested(job_id):
            finish_pipeline_cancelled(job_id)
            return

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            set_pipeline_error(job_id, f"Download timed out after {get_download_timeout()} seconds", status="timed_out")
            return

        with jobs_lock:
            job = jobs.get(job_id)
            if job is None or job.get("status") in TERMINAL_STATUSES:
                return
            job["current_artifact_id"] = artifact["id"]
            artifact.update({
                "status": "downloading",
                "phase": "starting",
                "attempt": 1,
            })

        # Try MP4->MP3 reuse for audio artifacts
        if artifact["type"] == "audio" and completed_videos:
            if try_ffmpeg_reuse(job_id, artifact, completed_videos, deadline, job_title):
                continue

        # Download via yt-dlp
        success = execute_single_artifact(job_id, url, artifact, deadline, job_title)

        if success and artifact["type"] == "video":
            completed_videos.append(artifact)

    # Finalize pipeline
    finalize_pipeline(job_id, title)


def execute_single_artifact(job_id, url, artifact, deadline, title):
    """Download one artifact with retry logic. Returns True on success."""
    max_attempts = artifact.get("max_attempts", get_job_retries() + 1)
    base_delay = get_retry_base_delay()

    # None = yt-dlp's default client. Only ever set by the 403 fallback below,
    # and only once, so the retry budget is unchanged.
    youtube_client = None
    youtube_fallback_used = False

    def build_command():
        # Rebuilt from scratch each attempt so yt-dlp performs a fresh
        # extraction and no previously spawned argv list is ever mutated.
        return pipeline.build_ytdlp_command(
            url, job_id, artifact, DOWNLOAD_DIR,
            PROGRESS_PREFIX, POSTPROCESS_PREFIX,
            get_concurrent_fragments(), ytdlp_runtime_args(),
            youtube_client=youtube_client,
        )

    cmd = build_command()

    for attempt in range(1, max_attempts + 1):
        if cancel_requested(job_id):
            return False

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(cmd, get_download_timeout())

        result = run_download_command(cmd, job_id, artifact, remaining)

        if result is None:  # Cancelled during spawn
            return False

        if result.returncode == 0:
            # Success: select output file
            chosen = pipeline.select_artifact_output_file(job_id, artifact["id"], artifact["type"], DOWNLOAD_DIR)
            if not chosen:
                cleanup_artifact_files(job_id, artifact["id"])
                artifact.update({
                    "status": "error",
                    "phase": "error",
                    "error": "Download completed but no final file was found",
                })
                return False

            final_size = os.path.getsize(chosen)
            filename = pipeline.make_artifact_filename(title, artifact)
            artifact.update({
                "status": "done",
                "phase": "done",
                "file": chosen,
                "filename": filename,
                "downloaded_bytes": final_size,
                "total_bytes": final_size,
                "speed": None,
                "eta": None,
                "percent": 100.0,
            })
            return True

        # Failure: check if retryable
        errors = result.stderr.strip().splitlines()
        message = errors[-1] if errors else "Download failed"

        use_youtube_fallback = should_retry_with_youtube_fallback(
            url, result.stderr, job_id, deadline, youtube_fallback_used
        )

        if attempt >= max_attempts or not is_transient_download_error(result.stderr):
            cleanup_artifact_files(job_id, artifact["id"])
            artifact.update({
                "status": "error",
                "phase": "error",
                "error": message,
            })
            return False

        # Retryable: wait and retry
        delay = retry_backoff_delay(attempt, base_delay)
        with jobs_lock:
            job = jobs.get(job_id)
            if job is None or job.get("status") in TERMINAL_STATUSES:
                return False
            artifact.update({
                "phase": "retrying",
                "error": None,
                "attempt": attempt + 1,
            })

        app.logger.warning(
            "Transient failure for job %s artifact %s; retrying (attempt %d/%d): %s",
            job_id, artifact["id"], attempt + 1, max_attempts, message
        )

        if use_youtube_fallback:
            youtube_client = YOUTUBE_FALLBACK_CLIENT
            youtube_fallback_used = True
            # Job id only: never the URL, query string or provider token.
            app.logger.warning(
                "Retrying YouTube download with embedded client after HTTP 403 "
                "(job %s artifact %s)",
                job_id, artifact["id"]
            )

        # Fresh argv for the next attempt; picks up the fallback client when set.
        cmd = build_command()

        if not wait_before_retry(job, delay, deadline):
            if cancel_requested(job_id):
                return False
            raise subprocess.TimeoutExpired(cmd, get_download_timeout())

    return False


def try_ffmpeg_reuse(job_id, artifact, completed_videos, deadline, title):
    """Derive audio from an already-downloaded video via ffmpeg.

    Exactly three outcomes, kept strictly distinct:
      * returns True  - success, the artifact is published and done
      * returns False - ORDINARY failure, the caller may fall back to yt-dlp
      * raises        - FATAL (cancellation or deadline); the caller must NOT
                        fall back, because spawning a fresh download for a
                        stopped job is exactly the bug this separation prevents

    The previous version wrapped everything in `except Exception: return False`,
    which swallowed the re-raised TimeoutExpired and silently downgraded a
    cancellation into an ordinary fallback.
    """
    source_video = pipeline.select_video_for_reuse(completed_videos)
    if not source_video:
        return False

    temp_output = os.path.join(DOWNLOAD_DIR, f"{job_id}.{artifact['id']}.temp.mp3")
    final_output = os.path.join(DOWNLOAD_DIR, f"{job_id}.{artifact['id']}.mp3")

    cmd = pipeline.build_ffmpeg_extract_command(source_video["file"], temp_output)

    # A leftover temp file from an earlier attempt must never be published as if
    # ffmpeg had just produced it.
    remove_quietly(temp_output)

    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return False
        artifact.update({
            "status": "processing",
            "phase": "processing",
            # Cleared, not carried over: whatever the download step left behind
            # describes bytes, not packaging, and would otherwise be shown as
            # the packaging speed/ETA until the first ffmpeg block arrives.
            "speed": None,
            "eta": None,
            "percent": None,
            "processed_seconds": None,
            "duration_seconds": None,
            "processing_speed": None,
        })

    popen_kwargs = {}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True

    # One critical section decides, spawns and registers under the PARENT id.
    process = gate_and_spawn(
        job_id,
        cmd,
        progress_target=artifact,
        deadline=deadline,
        stdout=subprocess.PIPE,
        # Merged so ONE reader drains both pipes. ffmpeg writes -progress
        # blocks to stdout and its input dump (the Duration line) to stderr;
        # two pipes with one reader deadlocks as soon as the unread one fills.
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
        **popen_kwargs,
    )
    if process is None:
        remove_quietly(temp_output)
        # Distinguish the two fatal reasons: the parent's shared budget running
        # out is a timeout, anything else here is a cancellation.
        if deadline - time.monotonic() <= 0:
            raise subprocess.TimeoutExpired(cmd, get_download_timeout())
        raise PipelineCancelled(f"job {job_id} stopped before ffmpeg reuse")

    diagnostics = deque(maxlen=200)
    reader = threading.Thread(
        target=read_ffmpeg_progress,
        args=(job_id, artifact, process.stdout, diagnostics),
        daemon=True,
    )
    reader.start()

    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(cmd, get_download_timeout())
        # wait(), not communicate(): the reader owns the pipe, so the output is
        # consumed as it is produced rather than buffered until exit.
        process.wait(timeout=remaining)
    except BaseException:
        # Covers TimeoutExpired AND anything unexpected out of wait()
        # (OSError, ValueError, KeyboardInterrupt...). Whatever went wrong, the
        # ffmpeg process may still be alive: reap it before unwinding, or the
        # next artifact starts while a stale ffmpeg still holds its output file.
        # Re-raising unchanged keeps an unexpected error OUT of the ordinary
        # `return False` fallback path - a broken pipe must not silently trigger
        # a fresh yt-dlp audio download.
        terminate_and_reap(process)
        remove_quietly(temp_output)
        raise
    finally:
        # Reaping the tree closes the pipe, so the reader reaches EOF and this
        # join cannot hold the pipeline behind a process that is already gone.
        reader.join(timeout=5)
        close_quietly(process.stdout)
        # Identity-checked: a slow unwind here must never evict the entry that
        # the NEXT artifact has already registered under the same parent id.
        unregister_process_if_current(job_id, process)

    if cancel_requested(job_id):
        remove_quietly(temp_output)
        raise PipelineCancelled(f"job {job_id} cancelled during ffmpeg reuse")

    if process.returncode != 0 or not os.path.isfile(temp_output):
        # The ONLY outcome permitted to fall back to a yt-dlp audio download.
        remove_quietly(temp_output)
        app.logger.warning(
            "FFmpeg reuse failed for job %s artifact %s; falling back to yt-dlp",
            job_id, artifact["id"]
        )
        return False

    # Re-check right before publishing: a job stopped in the window between
    # ffmpeg exiting and the rename must not acquire a finished artifact.
    #
    # Cancellation and deadline expiry are checked SEPARATELY and in that order.
    # Folding them into one PipelineCancelled reported a timed-out parent as
    # `cancelled`, which is a different user-visible outcome and a different
    # cleanup path. Cancellation is tested first so that when both are true the
    # established "cancellation wins" rule still holds.
    if cancel_requested(job_id):
        remove_quietly(temp_output)
        raise PipelineCancelled(f"job {job_id} cancelled before publishing ffmpeg output")

    if deadline - time.monotonic() <= 0:
        remove_quietly(temp_output)
        raise subprocess.TimeoutExpired(cmd, get_download_timeout())

    try:
        # os.replace is atomic and overwrites an existing destination;
        # os.rename raises on Windows when the destination exists.
        os.replace(temp_output, final_output)
        final_size = os.path.getsize(final_output)
    except OSError:
        remove_quietly(temp_output)
        app.logger.warning(
            "FFmpeg reuse could not publish output for job %s artifact %s; falling back to yt-dlp",
            job_id, artifact["id"]
        )
        return False

    filename = pipeline.make_artifact_filename(title, artifact)
    artifact.update({
        "status": "done",
        "phase": "done",
        "file": final_output,
        "filename": filename,
        "downloaded_bytes": final_size,
        "total_bytes": final_size,
        "speed": None,
        "eta": None,
        "percent": 100.0,
        # Live-only fields. ffmpeg's last block routinely stops a few frames
        # short of the duration, so leaving them set would make a finished
        # artifact read "1:22 / 1:23 processed" forever. duration_seconds is
        # kept: it describes the media, not the run.
        "processed_seconds": None,
        "processing_speed": None,
    })
    return True


def finalize_pipeline(job_id, title):
    """Compute final pipeline status from artifact results."""
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return

        cancelled = bool(job.get("cancel_requested"))
        if cancelled:
            finish_pipeline_cancelled_locked(job_id, job)
            return

        artifacts = job.get("artifacts", [])
        status, phase = pipeline.compute_aggregate_status(artifacts)
        percent = pipeline.compute_aggregate_progress(artifacts, None)

        job.update({
            "status": status,
            "phase": phase,
            "percent": percent,
            "current_artifact_id": None,
            "finished_at": time.time(),
        })


def set_pipeline_error(job_id, message, status="error"):
    """Mark pipeline as failed; cancellation wins, timeout cleans the parent."""
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return

        if job.get("cancel_requested"):
            finish_pipeline_cancelled_locked(job_id, job)
            cancelled = True
        else:
            cancelled = False
            # Mark all non-terminal artifacts with the terminal status.
            for art in job.get("artifacts", []):
                if art["status"] not in pipeline.ARTIFACT_TERMINAL:
                    art.update({
                        "status": status,
                        "phase": status,
                        "error": message,
                    })
            job.update({
                "status": status,
                "phase": status,
                "error": message,
                "current_artifact_id": None,
                "finished_at": time.time(),
            })

    # On a timeout, every artifact — done ones included — is worthless: the
    # parent ran out of budget. Reap the live process and delete every file.
    # On cancellation, the same cleanup applies. Done OUTSIDE jobs_lock.
    if status == "timed_out" or cancelled:
        reap_active_parent_process(job_id)
        cleanup_parent_files(job_id, job)


def finish_pipeline_cancelled(job_id):
    """Cancel the whole parent: mark state under lock, clean files outside it."""
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return
        finish_pipeline_cancelled_locked(job_id, job)
    cleanup_parent_files(job_id, job)


def finish_pipeline_cancelled_locked(job_id, job):
    """Mark the parent and its non-terminal artifacts cancelled.

    Filesystem cleanup is the caller's responsibility (see cleanup_parent_files)
    so that no I/O happens while jobs_lock is held.
    """
    for art in job.get("artifacts", []):
        if art["status"] not in pipeline.ARTIFACT_TERMINAL:
            art.update({
                "status": "cancelled",
                "phase": "cancelled",
                "error": None,
            })
    job.update({
        "status": "cancelled",
        "phase": "cancelled",
        "current_artifact_id": None,
        "error": None,
        "finished_at": time.time(),
    })


def cleanup_parent_files(job_id, job):
    """Delete every final and intermediate file of ALL artifacts of a parent.

    Called OUTSIDE jobs_lock. Never touches files belonging to another job:
    artifact-scoped globs are keyed by ``{job_id}.{artifact_id}``.
    """
    for art in job.get("artifacts", []):
        cleanup_artifact_files(job_id, art["id"], include_final=True)
    # Sweep any orphaned intermediates for this parent that no artifact dict
    # recorded (partial .part, stale .temp.*), then release the queue slot.
    for path in job_paths(job_id):
        if is_intermediate_file(path):
            remove_quietly(path)


def reap_active_parent_process(parent_job_id):
    """Terminate and reap whatever subprocess (if any) a parent is running."""
    with jobs_lock:
        process = processes.get(parent_job_id)
    if process is not None:
        terminate_and_reap(process)
        unregister_process_if_current(parent_job_id, process)


def run_download(job_id, url, format_choice, format_id):
    max_attempts = get_job_retries() + 1
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None or job.get("status") in TERMINAL_STATUSES:
            # A cancelled queued job may still be seen by a waking worker.
            return
        job["id"] = job_id
        cancelled_before_start = bool(job.get("cancel_requested"))
        if not cancelled_before_start:
            job.update({
                "status": "downloading",
                "phase": "starting",
                "started_at": time.time(),
                "attempt": 1,
                "max_attempts": max_attempts,
            })
    if cancelled_before_start:
        finish_job_cancelled(job_id)
        return
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    # Shares one builder with the multi-output pipeline so the 403 fallback
    # cannot drift between the two paths. The legacy filename contract is kept
    # by passing this path's own output template.
    legacy_artifact = {
        "id": job_id,
        "type": "audio" if format_choice == "audio" else "video",
        "format_id": None if format_choice == "audio" else format_id,
    }
    youtube_client = None
    youtube_fallback_used = False

    def build_command():
        return pipeline.build_ytdlp_command(
            url, job_id, legacy_artifact, DOWNLOAD_DIR,
            PROGRESS_PREFIX, POSTPROCESS_PREFIX,
            get_concurrent_fragments(), ytdlp_runtime_args(),
            youtube_client=youtube_client,
            output_template=out_template,
        )

    cmd = build_command()
    timeout = get_download_timeout()
    base_delay = get_retry_base_delay()
    deadline = time.monotonic() + timeout
    attempt = 1
    attempt_timeout = timeout

    try:
        while True:
            # Each attempt re-runs the full yt-dlp command, forcing a fresh
            # extraction (new signed media URLs) with the same format choice.
            # None means the pre-spawn gate refused a cancelled job.
            result = run_download_command(cmd, job_id, job, attempt_timeout)
            if result is None or cancel_requested(job_id):
                finish_job_cancelled(job_id)
                return
            if result.returncode == 0:
                break
            errors = result.stderr.strip().splitlines()
            message = errors[-1] if errors else "Download failed"
            use_youtube_fallback = should_retry_with_youtube_fallback(
                url, result.stderr, job_id, deadline, youtube_fallback_used
            )
            if attempt >= max_attempts or not is_transient_download_error(result.stderr):
                cleanup_job_files(job_id)
                set_error(job_id, message)
                return
            delay = retry_backoff_delay(attempt, base_delay)
            attempt += 1
            with jobs_lock:
                current = jobs.get(job_id)
                if current is None or current.get("status") in TERMINAL_STATUSES:
                    return
                current.update({
                    "phase": "retrying",
                    "error": None,
                    "speed": None,
                    "eta": None,
                    "attempt": attempt,
                })
            app.logger.warning(
                "Transient download failure for job %s; retrying with a fresh extraction (attempt %d/%d): %s",
                job_id,
                attempt,
                max_attempts,
                message,
            )
            if use_youtube_fallback:
                youtube_client = YOUTUBE_FALLBACK_CLIENT
                youtube_fallback_used = True
                # Job id only: never the URL, query string or provider token.
                app.logger.warning(
                    "Retrying YouTube download with embedded client after HTTP 403 (job %s)",
                    job_id,
                )
            # Fresh argv for the next attempt; picks up the fallback client when set.
            cmd = build_command()
            # Intermediate .part files are intentionally kept so the next
            # attempt can resume; the total deadline keeps ticking.
            if not wait_before_retry(job, delay, deadline):
                if cancel_requested(job_id):
                    finish_job_cancelled(job_id)
                    return
                cleanup_job_files(job_id)
                set_error(job_id, f"Download timed out after {timeout} seconds", status="timed_out")
                return
            if cancel_requested(job_id):
                finish_job_cancelled(job_id)
                return
            attempt_timeout = deadline - time.monotonic()
            if attempt_timeout <= 0:
                cleanup_job_files(job_id)
                set_error(job_id, f"Download timed out after {timeout} seconds", status="timed_out")
                return
            with jobs_lock:
                current = jobs.get(job_id)
                if current is None or current.get("status") in TERMINAL_STATUSES:
                    return
                # A cancelling job must never flip back to "starting".
                cancelled_before_attempt = bool(current.get("cancel_requested"))
                if not cancelled_before_attempt:
                    current.update({"phase": "starting"})
            if cancelled_before_attempt:
                finish_job_cancelled(job_id)
                return

        chosen = select_output_file(job_id, format_choice)
        if not chosen:
            cleanup_job_files(job_id)
            set_error(job_id, "Download completed but no final file was found")
            return

        final_size = os.path.getsize(chosen)
        filename = safe_download_name(job.get("title"), chosen)
        for path in job_paths(job_id):
            if path != chosen:
                try:
                    os.remove(path)
                except OSError:
                    pass

        with jobs_lock:
            cancelled_at_finish = bool(job.get("cancel_requested"))
            if not cancelled_at_finish:
                job.update({
                    "phase": "done",
                    "file": chosen,
                    "filename": filename,
                    "downloaded_bytes": final_size,
                    "total_bytes": final_size,
                    "speed": None,
                    "eta": None,
                    "percent": 100.0,
                    "status": "done",
                    "finished_at": time.time(),
                })
        if cancelled_at_finish:
            finish_job_cancelled(job_id)
    except subprocess.TimeoutExpired:
        if cancel_requested(job_id):
            finish_job_cancelled(job_id)
            return
        cleanup_job_files(job_id)
        set_error(job_id, f"Download timed out after {timeout} seconds", status="timed_out")
    except FileNotFoundError:
        cleanup_job_files(job_id)
        set_error(job_id, "Server downloader became unavailable. Restart LinkSift with Docker Compose.")
    except Exception:
        app.logger.exception("Download failed unexpectedly for job %s", job_id)
        if cancel_requested(job_id):
            finish_job_cancelled(job_id)
            return
        cleanup_job_files(job_id)
        set_error(job_id, "Download failed unexpectedly")


def cleanup_orphan_files(now=None, ttl=None):
    """Delete stale LinkSift-named files that no known job owns."""
    if now is None:
        now = time.time()
    if ttl is None:
        ttl = get_job_ttl()
    with jobs_lock:
        known_jobs = set(jobs)
    try:
        names = os.listdir(DOWNLOAD_DIR)
    except OSError:
        app.logger.warning("Could not scan downloads directory for cleanup", exc_info=True)
        return
    for name in names:
        if not JOB_FILE_NAME_PATTERN.match(name) or name[:10] in known_jobs:
            continue
        path = os.path.join(DOWNLOAD_DIR, name)
        try:
            if not os.path.isfile(path) or now - os.path.getmtime(path) < ttl:
                continue
            os.remove(path)
        except OSError:
            app.logger.warning("Could not remove orphan file %s", name, exc_info=True)


def run_cleanup(now=None):
    """Expire terminal jobs past their TTL, then sweep orphan files."""
    if now is None:
        now = time.time()
    ttl = get_job_ttl()
    expired = []
    with jobs_lock:
        for job_id, job in list(jobs.items()):
            if job.get("status") not in TERMINAL_STATUSES:
                continue
            finished_at = job.get("finished_at") or job.get("created_at")
            if finished_at is None or now - finished_at < ttl:
                continue
            expired.append(job_id)
            jobs.pop(job_id, None)
            processes.pop(job_id, None)
            # Idempotency data dies with the job it protects. Identity-checked
            # so a request id already reassigned to a newer job survives.
            request_id = job.get("client_request_id")
            if request_id is not None:
                entry = request_ids.get(request_id)
                if entry is not None and entry.get("job_id") == job_id:
                    request_ids.pop(request_id, None)
    active_scheduler = scheduler
    if active_scheduler is not None:
        for job_id in expired:
            active_scheduler.cancel_queued(job_id)
    for job_id in expired:
        cleanup_job_files(job_id, include_final=True)
    cleanup_orphan_files(now=now, ttl=ttl)


@app.before_request
def opportunistic_cleanup():
    """Run cleanup at most once per interval, without failing the request."""
    global last_cleanup_monotonic
    if not cleanup_lock.acquire(blocking=False):
        return
    try:
        now = time.monotonic()
        if last_cleanup_monotonic is not None and now - last_cleanup_monotonic < CLEANUP_INTERVAL_SECONDS:
            return
        last_cleanup_monotonic = now
        run_cleanup()
    except Exception:
        app.logger.exception("LinkSift cleanup failed")
    finally:
        cleanup_lock.release()


def startup_cleanup():
    global last_cleanup_monotonic
    try:
        with cleanup_lock:
            last_cleanup_monotonic = time.monotonic()
            run_cleanup()
    except Exception:
        app.logger.exception("LinkSift startup cleanup failed")


startup_cleanup()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    missing_tools = get_missing_runtime_tools(("yt-dlp", "ffmpeg"))
    provider_configured = get_po_token_provider_url() is not None
    return jsonify({
        "status": "ok" if not missing_tools else "degraded",
        "missing_tools": missing_tools,
        # Additive, informational only: a missing capability degrades YouTube
        # reliability but is never fatal on its own. "configured" means the
        # environment URL is valid; "po_token_provider" additionally requires
        # the plugin to be installed. No live probe of the provider is made.
        "capabilities": {
            "youtube_js_runtime": has_js_runtime(),
            "youtube_ejs": has_ejs_support(),
            "po_token_provider_configured": provider_configured,
            "po_token_provider": provider_configured and has_pot_plugin(),
        },
    })


@app.route("/api/info", methods=["POST"])
def get_info():
    data, error = get_request_data()
    if error:
        return error
    url = get_url(data)
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    unavailable = runtime_unavailable_response(("yt-dlp",))
    if unavailable:
        return unavailable

    cmd = ["yt-dlp", "--no-playlist", "-j", *ytdlp_runtime_args(), "--", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = parse_ytdlp_json(result.stdout)
        best_by_height = {}
        for item in info.get("formats", []):
            if not isinstance(item, dict):
                continue
            height = item.get("height")
            format_id = item.get("format_id")
            if isinstance(height, int) and isinstance(format_id, str) and item.get("vcodec", "none") != "none":
                bitrate = finite_number(item.get("tbr"), 0)
                if height not in best_by_height or bitrate > finite_number(best_by_height[height].get("tbr"), 0):
                    best_by_height[height] = item

        formats = [
            {"id": item["format_id"], "label": f"{height}p", "height": height}
            for height, item in best_by_height.items()
        ]
        formats.sort(key=lambda item: item["height"], reverse=True)
        return jsonify({
            "title": info.get("title", "") if isinstance(info.get("title"), str) else "",
            "thumbnail": info.get("thumbnail", "") if isinstance(info.get("thumbnail"), str) else "",
            "duration": finite_number(info.get("duration")),
            "uploader": info.get("uploader", "") if isinstance(info.get("uploader"), str) else "",
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except FileNotFoundError:
        return subprocess_unavailable_response(("yt-dlp",))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/playlist", methods=["POST"])
def get_playlist_info():
    data, error = get_request_data()
    if error:
        return error
    url = get_url(data)
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    unavailable = runtime_unavailable_response(("yt-dlp",))
    if unavailable:
        return unavailable

    limit = get_max_playlist_items()
    cmd = ["yt-dlp", "--flat-playlist", "--playlist-items", f"1:{limit + 1}", "-J", *ytdlp_runtime_args(), "--", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)
        entries = info.get("entries") if isinstance(info, dict) else []
        if not isinstance(entries, list):
            entries = []
        urls = [
            entry["url"]
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("url"), str) and entry["url"].strip()
        ]
        # Truncation is judged on the raw entry count so unavailable or
        # malformed entries cannot mask an oversized playlist.
        return jsonify({"urls": urls[:limit], "truncated": len(entries) > limit, "limit": limit})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching playlist info"}), 400
    except FileNotFoundError:
        return subprocess_unavailable_response(("yt-dlp",))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data, error = get_request_data()
    if error:
        return error
    url = get_url(data)
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    title = data.get("title", "")
    if not isinstance(title, str):
        return jsonify({"error": "Title must be a string"}), 400

    client_request_id, id_error = get_client_request_id(data)
    if id_error:
        return jsonify({"error": id_error}), 400

    launch_source, source_error = get_launch_source(data)
    if source_error:
        return jsonify({"error": source_error}), 400

    unavailable = runtime_unavailable_response(("yt-dlp", "ffmpeg"))
    if unavailable:
        return unavailable

    # Normalize outputs: support both legacy format and new multi-output
    outputs, error_msg = pipeline.normalize_outputs(data)
    if error_msg:
        return jsonify({"error": error_msg}), 400

    fingerprint = (
        download_request_fingerprint(url, title, outputs)
        if client_request_id is not None
        else None
    )

    # Acquire the scheduler before creating the job and outside jobs_lock:
    # a worker-startup failure then leaves nothing behind to roll back.
    try:
        download_scheduler = get_scheduler()
    except Exception:
        app.logger.exception("Download scheduler failed to start")
        return jsonify({"error": "Download scheduler is unavailable"}), 503

    job_id = uuid.uuid4().hex[:10]

    # Plan artifacts from normalized outputs
    execution_order, display_order = pipeline.plan_artifacts(outputs)

    # Initialize artifact states
    for art in execution_order:
        art["status"] = "pending"
        art["phase"] = "pending"
        art["downloaded_bytes"] = 0
        art["total_bytes"] = None
        art["speed"] = None
        art["eta"] = None
        art["percent"] = None
        art["error"] = None
        art["attempt"] = 0
        art["max_attempts"] = get_job_retries() + 1

    accepted = False
    duplicate_hit = None
    duplicate_conflict = None

    with jobs_lock:
        # The duplicate check, the job insert and the scheduler submit all live
        # in ONE critical section. Two concurrent retries of the same launch
        # therefore serialize: the first registers the id and submits, the
        # second sees the entry and returns the first one's job_id without ever
        # reaching the scheduler.
        if client_request_id is not None:
            existing = request_ids.get(client_request_id)
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    duplicate_conflict = existing
                else:
                    duplicate_hit = existing["job_id"]

        if duplicate_hit is None and duplicate_conflict is None:
            jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "phase": "queued",
                "url": url,
                "title": title,
                "artifacts": display_order,
                "execution_order": execution_order,
                "current_artifact_id": None,
                "downloaded_bytes": 0,
                "total_bytes": None,
                "speed": None,
                "eta": None,
                "percent": None,
                "cancel_requested": False,
                "cancel_event": threading.Event(),
                "created_at": time.time(),
                "started_at": None,
                "finished_at": None,
                # Kept on the job so run_cleanup can drop the matching
                # request_ids entry when the job expires, leaving no map that
                # grows for the lifetime of the process.
                "client_request_id": client_request_id,
            }
            if client_request_id is not None:
                request_ids[client_request_id] = {
                    "job_id": job_id,
                    "fingerprint": fingerprint,
                }
            # The job must exist before a worker can pick it up; if the queue is
            # full it is removed in the same critical section, leaving no orphan.
            accepted = download_scheduler.submit(
                job_id,
                lambda: run_pipeline(job_id, url, title),
            )
            if not accepted:
                jobs.pop(job_id, None)
                if client_request_id is not None:
                    request_ids.pop(client_request_id, None)

    if duplicate_conflict is not None:
        # Same id, different ask: answering with the other job's id would hand
        # the client progress for a download it did not request.
        app.logger.warning(
            "Rejected reused client_request_id with a different payload "
            "(existing job %s, launch_source %s)",
            duplicate_conflict["job_id"], launch_source or "unspecified",
        )
        return jsonify({
            "error": "client_request_id was already used for a different download",
        }), 409

    if duplicate_hit is not None:
        # Ids and states only: never the URL, title, or request body.
        app.logger.info(
            "Download launch deduplicated (job %s, launch_source %s, deduplicated true)",
            duplicate_hit, launch_source or "unspecified",
        )
        return jsonify({"job_id": duplicate_hit, "deduplicated": True})

    if not accepted:
        return jsonify({"error": "Download queue is full"}), 429

    app.logger.info(
        "Download launch accepted (job %s, launch_source %s, deduplicated false)",
        job_id, launch_source or "unspecified",
    )
    return jsonify({"job_id": job_id})


@app.route("/api/download/<job_id>", methods=["DELETE"])
def cancel_download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404
        status = job.get("status")
        if status in TERMINAL_STATUSES:
            return jsonify({"status": status})
        job["cancel_requested"] = True
        cancel_event = job.get("cancel_event")
        if cancel_event is not None:
            cancel_event.set()
        if status == "queued" and get_scheduler().cancel_queued(job_id):
            # Removed before any worker owned it: no subprocess ever exists.
            # Still mark every artifact cancelled so /api/status stays consistent.
            for art in job.get("artifacts", []):
                if art["status"] not in pipeline.ARTIFACT_TERMINAL:
                    art.update({
                        "status": "cancelled",
                        "phase": "cancelled",
                        "error": None,
                    })
            job.update({
                "status": "cancelled",
                "phase": "cancelled",
                "current_artifact_id": None,
                "speed": None,
                "eta": None,
                "percent": None,
                "error": None,
                "finished_at": time.time(),
            })
            cancelled_job = job
            break_outside_lock = True
        else:
            # Running, or lost the dequeue race: the owning worker observes
            # cancel_requested and finalizes the job as cancelled.
            job.update({
                "status": "cancelling",
                "phase": "cancelling",
                "speed": None,
                "eta": None,
            })
            process = processes.get(job_id)
            cancelled_job = None
            break_outside_lock = False

    if break_outside_lock:
        # A queued job had no worker and no files yet; clean any intermediates
        # a pre-spawn race might have left, outside the lock.
        cleanup_parent_files(job_id, cancelled_job)
        return jsonify({"status": "cancelled"})
    terminate_process_tree(process)
    return jsonify({"status": "cancelling"})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        status = job["status"]
        artifacts = job.get("artifacts", []) if "artifacts" in job else None
        current_artifact_id = job.get("current_artifact_id")

        # Build the artifact list once.
        artifacts_payload = None
        current_artifact = None
        if artifacts is not None:
            artifacts_payload = []
            for art in artifacts:
                artifacts_payload.append({
                    "id": art["id"],
                    "type": art["type"],
                    "format_id": art.get("format_id"),
                    "status": art["status"],
                    "phase": art["phase"],
                    "filename": art.get("filename"),
                    "downloaded_bytes": art.get("downloaded_bytes", 0),
                    "total_bytes": art.get("total_bytes"),
                    "speed": art.get("speed"),
                    "eta": art.get("eta"),
                    "percent": art.get("percent"),
                    "error": art.get("error"),
                    "attempt": art.get("attempt"),
                    "max_attempts": art.get("max_attempts"),
                    # Additive packaging fields. Always present (null when the
                    # artifact never reached ffmpeg), so an old client that
                    # ignores them is unaffected and a new one never has to
                    # distinguish "absent" from "unknown".
                    "processed_seconds": art.get("processed_seconds"),
                    "duration_seconds": art.get("duration_seconds"),
                    "processing_speed": art.get("processing_speed"),
                })
                if art["id"] == current_artifact_id:
                    current_artifact = art

        # Backend-authoritative aggregate. While artifacts are present and the
        # parent is active, derive the parent's progress fields from them so the
        # client sees one consistent number regardless of where the progress
        # came from. Root-level fields (status, finished_at, error) keep coming
        # straight from the job dict; they are the source of truth there.
        parent_status = status
        parent_percent = job.get("percent")
        parent_bytes = job.get("downloaded_bytes")
        parent_total = job.get("total_bytes")
        parent_speed = job.get("speed")
        parent_eta = job.get("eta")
        parent_attempt = job.get("attempt")
        parent_max_attempts = job.get("max_attempts")
        parent_filename = job.get("filename")
        parent_processed_seconds = None
        parent_duration_seconds = None
        parent_processing_speed = None

        # Aggregate progress only applies to real multi-output jobs (artifacts
        # exist and at least one has started). A legacy single-output parent, a
        # claim-error parent, and a no-artifacts (plan-artifacts always yields
        # >=1, so this covers racy/legacy states) parent all keep reading their
        # own root fields straight from the job dict: the percent set (or
        # cleared) by the pipeline is the only truth there.
        has_running_artifacts = artifacts is not None and len(artifacts) > 0 and (
            current_artifact is not None
            or any(a["status"] in ("downloading", "retrying", "processing") for a in artifacts)
        )

        if has_running_artifacts:
            agg = pipeline.compute_aggregate_progress(
                artifacts, current_artifact_id if current_artifact is not None else None
            )
            if agg is not None:
                parent_percent = agg
            if current_artifact is not None:
                parent_bytes = current_artifact.get("downloaded_bytes")
                parent_total = current_artifact.get("total_bytes")
                parent_speed = current_artifact.get("speed")
                # The current artifact's ETA is the parent's ETA in every
                # phase, packaging included: while ffmpeg runs, that estimate
                # is the only remaining-time figure the job has.
                parent_eta = current_artifact.get("eta")
                parent_attempt = current_artifact.get("attempt")
                parent_max_attempts = current_artifact.get("max_attempts")
                parent_processed_seconds = current_artifact.get("processed_seconds")
                parent_duration_seconds = current_artifact.get("duration_seconds")
                parent_processing_speed = current_artifact.get("processing_speed")
                if parent_status not in TERMINAL_STATUSES:
                    parent_filename = None

        payload = {
            "status": parent_status,
            "phase": job.get("phase"),
            "downloaded_bytes": parent_bytes,
            "total_bytes": parent_total,
            "speed": parent_speed,
            "eta": parent_eta,
            "percent": parent_percent,
            "error": job.get("error"),
            "filename": parent_filename,
            "queue_position": get_scheduler().queue_position(job_id) if status == "queued" else None,
            "started_at": job.get("started_at"),
            "attempt": parent_attempt,
            "max_attempts": parent_max_attempts,
            # Mirrors of the current artifact's packaging progress; null for
            # every job that is not packaging right now.
            "processed_seconds": parent_processed_seconds,
            "duration_seconds": parent_duration_seconds,
            "processing_speed": parent_processing_speed,
        }

        # A terminal single-output parent surfaces its one artifact's filename
        # for legacy clients that read /api/status, not /api/file.
        if artifacts is not None and len(artifacts) == 1 and parent_status in ("done", "partial"):
            payload["filename"] = artifacts[0].get("filename")

        # While active with multiple artifacts, the queue position is reported
        # only when truly queued.
        if artifacts is not None and status == "queued":
            payload["artifacts"] = artifacts_payload
            payload["current_artifact_id"] = current_artifact_id
            return jsonify(payload)

        if artifacts is not None:
            payload["artifacts"] = artifacts_payload
            payload["current_artifact_id"] = current_artifact_id

        # For multi-output jobs the parent phase mirrors the CURRENT artifact's
        # phase for every active phase, not just a hand-picked subset.
        #
        # The old subset was ("retrying", "processing"), so an artifact that was
        # plainly `downloading` left the parent reporting whatever it was set to
        # at spawn - normally `starting`. The parent card therefore sat on
        # "Starting..." for the entire download of every artifact.
        #
        # Terminal parent phases are never overwritten: once a parent is done /
        # error / cancelled / timed_out / partial, a stale artifact phase must
        # not resurrect it as active.
        if (
            current_artifact is not None
            and parent_status not in TERMINAL_STATUSES
            and current_artifact.get("phase") in ACTIVE_ARTIFACT_PHASES
        ):
            payload["phase"] = current_artifact["phase"]

    return jsonify(payload)


@app.route("/api/file/<job_id>/<artifact_id>")
def download_artifact_file(job_id, artifact_id):
    """Download a specific artifact from a multi-output job.

    A done artifact may be retrieved only while the PARENT is in a terminal
    state that permits serving output (done or partial). If the parent was
    cancelled, timed out, or errored, the artifact's files have been cleaned
    and must not be served even if a stray file lingered.
    """
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404

        if job.get("status") not in ("done", "partial"):
            return jsonify({
                "error": "Job is not ready",
                "status": job.get("status"),
            }), 404

        artifact = None
        for art in job.get("artifacts", []):
            if art["id"] == artifact_id:
                artifact = art
                break

        if not artifact:
            return jsonify({"error": "Artifact not found"}), 404

        if artifact["status"] != "done":
            return jsonify({"error": "Artifact not ready"}), 404

        path = artifact.get("file")
        filename = artifact.get("filename")

    download_root = os.path.realpath(DOWNLOAD_DIR)
    if (
        not isinstance(path, str)
        or not isinstance(filename, str)
        or not os.path.isfile(path)
        or os.path.commonpath([download_root, os.path.realpath(path)]) != download_root
    ):
        return jsonify({"error": "File not ready"}), 404

    return send_file(path, as_attachment=True, download_name=filename)


@app.route("/api/file/<job_id>")
def download_file(job_id):
    """Download file for legacy single-output jobs.

    For backward compatibility with existing clients.
    Returns 409 Conflict if job has multiple outputs.
    """
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404

        # Check if this is a multi-output job
        artifacts = job.get("artifacts", [])
        if len(artifacts) > 1:
            return jsonify({
                "error": "Job has multiple outputs",
                "hint": "Use /api/file/<job_id>/<artifact_id> instead",
                "artifacts": [art["id"] for art in artifacts]
            }), 409

        # Single-output job: get file/filename from the single artifact
        if len(artifacts) == 1:
            artifact = artifacts[0]
            if artifact.get("status") != "done":
                return jsonify({"error": "Artifact not ready"}), 404
            path = artifact.get("file")
            filename = artifact.get("filename")
        else:
            # Legacy job without artifacts (shouldn't happen but handle gracefully)
            path = job.get("file")
            filename = job.get("filename")

        # Mirror the artifact-endpoint gate: a done artifact is only retrievable
        # while the parent itself is done or partial.
        if job.get("status") not in ("done", "partial"):
            return jsonify({
                "error": "Job is not ready",
                "status": job.get("status"),
            }), 404

    download_root = os.path.realpath(DOWNLOAD_DIR)
    if (
        not job
        or job.get("status") not in ("done", "partial")
        or not isinstance(path, str)
        or not isinstance(filename, str)
        or not os.path.isfile(path)
        or os.path.commonpath([download_root, os.path.realpath(path)]) != download_root
    ):
        return jsonify({"error": "File not ready"}), 404
    return send_file(path, as_attachment=True, download_name=filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
