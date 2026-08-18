"""Multi-output download pipeline logic for LinkSift.

Pure logic module: request normalization, output validation, artifact planning,
command construction, aggregate status/progress computation.

No Flask, no threading, no subprocess calls — only argv list construction.
"""

import os
import re

DEFAULT_MAX_OUTPUTS_PER_JOB = 4
MAX_OUTPUTS_PER_JOB_LIMIT = 8
FORMAT_ID_PATTERN = re.compile(r"^[0-9a-zA-Z_+\-/]{1,128}$")

ARTIFACT_TERMINAL = frozenset({"done", "error", "cancelled", "timed_out"})

PARENT_TERMINAL = frozenset({"done", "error", "cancelled", "timed_out", "partial"})


def get_max_outputs_per_job(env_getter=None):
    """Parse LINKSIFT_MAX_OUTPUTS_PER_JOB from environment.

    Valid range: 1..8. Values >8 clamp to 8. Missing, malformed, zero,
    or negative values fall back to the default (4).
    """
    getter = env_getter or os.environ.get
    raw = getter("LINKSIFT_MAX_OUTPUTS_PER_JOB")
    if raw is None:
        return DEFAULT_MAX_OUTPUTS_PER_JOB
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_OUTPUTS_PER_JOB
    if value <= 0:
        return DEFAULT_MAX_OUTPUTS_PER_JOB
    return min(value, MAX_OUTPUTS_PER_JOB_LIMIT)


def normalize_outputs(data):
    """Normalize a download request into a validated list of output specs.

    Returns (outputs_list, error_message).
    When error_message is not None, the request is invalid.
    """
    has_outputs = "outputs" in data
    has_legacy_format = "format" in data
    has_legacy_format_id = "format_id" in data
    has_legacy = has_legacy_format or has_legacy_format_id

    if has_outputs and has_legacy:
        return None, "Cannot use both 'outputs' and legacy format fields in the same request"

    if has_outputs:
        return _normalize_explicit_outputs(data["outputs"])

    return _normalize_legacy_outputs(data)


def _normalize_explicit_outputs(outputs):
    if not isinstance(outputs, list):
        return None, "'outputs' must be an array"
    if len(outputs) == 0:
        return None, "'outputs' must not be empty"

    limit = get_max_outputs_per_job()
    if len(outputs) > limit:
        return None, f"Too many outputs ({len(outputs)}); maximum is {limit}"

    normalized = []
    for i, output in enumerate(outputs):
        if not isinstance(output, dict):
            return None, f"Output at index {i} must be an object"

        out_type = output.get("type")
        if out_type not in ("video", "audio"):
            return None, f"Output at index {i} has invalid type (must be 'video' or 'audio')"

        format_id = output.get("format_id")
        if out_type == "video":
            if format_id is None:
                return None, f"Video output at index {i} requires a 'format_id'"
            if not isinstance(format_id, str) or not format_id.strip():
                return None, f"Video output at index {i} has invalid 'format_id' (must be a non-empty string)"
            if not FORMAT_ID_PATTERN.match(format_id):
                return None, f"Video output at index {i} has unsafe 'format_id'"
            format_id = format_id.strip()
        else:
            format_id = None

        normalized.append({"type": out_type, "format_id": format_id})

    seen = set()
    for output in normalized:
        key = (output["type"], output.get("format_id"))
        if key in seen:
            label = f"{output['type']} {output.get('format_id', '')}".strip()
            return None, f"Duplicate output: {label}"
        seen.add(key)

    return normalized, None


def _normalize_legacy_formats(data):
    format_choice = data.get("format", "video")
    if format_choice not in ("audio", "video"):
        return None, None, "Format must be audio or video"

    format_id = data.get("format_id")
    if format_choice == "video" and format_id is not None:
        if not isinstance(format_id, str):
            return None, None, "Format ID must be a string"
    elif format_choice == "audio":
        format_id = None

    return format_choice, format_id, None


def _normalize_legacy_outputs(data):
    format_choice, format_id, error = _normalize_legacy_formats(data)
    if error:
        return None, error
    return [{"type": format_choice, "format_id": format_id}], None


def plan_artifacts(outputs):
    """Create artifact dicts from validated output specs.

    Returns (execution_order, display_order).
    execution_order puts videos before audio so the pipeline can attempt
    MP4→MP3 reuse. display_order preserves the original request order.
    """
    artifacts = []
    for i, output in enumerate(outputs):
        art_id = f"a{i:03d}"
        if output["type"] == "video":
            label = f"MP4 {output['format_id']}"
        else:
            label = "MP3 audio"

        artifact = {
            "id": art_id,
            "type": output["type"],
            "format_id": output.get("format_id"),
            "label": label,
            "status": "pending",
            "phase": "pending",
            "filename": None,
            "file": None,
            "downloaded_bytes": 0,
            "total_bytes": None,
            "speed": None,
            "eta": None,
            "percent": None,
            "error": None,
            "attempt": None,
            "max_attempts": None,
            "source": None,
            # Packaging (ffmpeg) progress. Always present so /api/status can
            # report the same shape for every artifact, whether or not it ever
            # reaches the packaging step.
            "processed_seconds": None,
            "duration_seconds": None,
            "processing_speed": None,
        }
        artifacts.append(artifact)

    display_order = list(artifacts)
    videos = [a for a in artifacts if a["type"] == "video"]
    audios = [a for a in artifacts if a["type"] == "audio"]
    execution_order = videos + audios
    return execution_order, display_order


def artifact_output_template(job_id, artifact_id, download_dir):
    """yt-dlp output template for a multi-output artifact."""
    return os.path.join(download_dir, f"{job_id}.{artifact_id}.%(ext)s")


def select_artifact_output_file(job_id, artifact_id, format_type, download_dir):
    """Find the final output file for a completed artifact."""
    import glob

    extension = ".mp3" if format_type == "audio" else ".mp4"
    exact = os.path.join(download_dir, f"{job_id}.{artifact_id}{extension}")
    if os.path.isfile(exact):
        return exact

    pattern = os.path.join(download_dir, f"{job_id}.{artifact_id}.*")
    candidates = [
        p for p in glob.glob(pattern)
        if os.path.isfile(p) and not is_intermediate_file(p) and p.lower().endswith(extension)
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def is_intermediate_file(path):
    """True for yt-dlp/ffmpeg working files, never for a final artifact.

    Recognized intermediates:
      * ``.part`` partial downloads (``<job>.<artifact>.mp4.part``, ``.part-Frag1``)
      * ``.ytdl`` resume metadata sidecars
      * ``.f<digits>`` per-format streams written before muxing (``.f137.mp4``)
      * ``.temp.<ext>`` LinkSift ffmpeg scratch output published via ``os.replace``

    Final artifacts are backend-named ``<job_id>[.<artifact_id>].<ext>`` and must
    never match, including names whose artifact id merely contains an ``f``.
    """
    name = os.path.basename(path)
    if ".part" in name:
        return True
    if name.endswith(".ytdl"):
        return True
    if ".temp." in name or name.endswith(".temp"):
        return True
    # ``.f`` only marks a fragment when digits follow it, so artifact ids and
    # extensions that merely contain ``f`` stay classified as final output.
    index = name.find(".f")
    while index != -1:
        suffix = name[index + 2:]
        if suffix and suffix[0].isdigit():
            return True
        index = name.find(".f", index + 1)
    return False


def build_ytdlp_command(url, job_id, artifact, download_dir, progress_prefix, postprocess_prefix,
                         concurrent_fragments, runtime_args, youtube_client=None,
                         output_template=None):
    """Build the yt-dlp argv list for downloading one artifact.

    ``youtube_client`` forces a specific YouTube player client via
    ``--extractor-args youtube:player_client=<client>``. It is namespaced under
    the ``youtube`` extractor, so it coexists with the ``youtubepot-bgutilhttp``
    extractor args already present in ``runtime_args`` -- yt-dlp merges repeated
    ``--extractor-args`` flags per extractor rather than overwriting them. Left
    at None the default client is used, which must stay the first-attempt
    behaviour because some videos disable embedding.

    ``output_template`` overrides the artifact-scoped ``-o`` template. The legacy
    single-output path owns a different filename contract
    (``<job_id>.%(ext)s`` instead of ``<job_id>.<artifact_id>.%(ext)s``) and
    passes its own template so both paths can share this one builder without
    either changing the files it produces.

    A fresh list is constructed on every call, so a caller retrying with a
    different client never mutates or aliases a previously spawned argv.
    """
    out_template = output_template or artifact_output_template(job_id, artifact["id"], download_dir)
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--newline",
        "--progress",
        "--progress-template", f"download:{progress_prefix}%(progress)j",
        "--progress-template", f"postprocess:{postprocess_prefix}%(progress)j",
        "--concurrent-fragments", str(concurrent_fragments),
        "--continue",
        "--part",
        "--retries", "10",
        "--fragment-retries", "10",
        "--extractor-retries", "3",
        "--retry-sleep", "http:exp=1:20",
        "--retry-sleep", "fragment:exp=1:20",
    ]
    cmd.extend(runtime_args)
    if youtube_client:
        cmd.extend(["--extractor-args", f"youtube:player_client={youtube_client}"])
    cmd.extend(["-o", out_template])

    if artifact["type"] == "audio":
        cmd.extend(["-x", "--audio-format", "mp3"])
    elif artifact.get("format_id"):
        cmd.extend(["-f", f"{artifact['format_id']}+bestaudio/best", "--merge-output-format", "mp4"])
    else:
        cmd.extend(["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"])

    cmd.extend(["--", url])
    return cmd


def build_ffmpeg_extract_command(source_mp4, output_mp3):
    """Build ffmpeg argv list to extract audio from a completed MP4.

    Uses libmp3lame at 320k CBR. No shell=True.

    ``-progress pipe:1`` makes ffmpeg emit machine-readable ``key=value`` blocks
    on stdout while it works, so packaging reports real progress instead of
    sitting at a fixed percentage until the process exits. ``-nostats``
    suppresses the human-readable carriage-return status line that would
    otherwise interleave with the log on stderr; the input metadata dump (which
    carries the ``Duration:`` line the reader needs) is unaffected, so the
    duration comes from ffmpeg itself with no extra ffprobe subprocess.

    Both are global options and are placed before ``-i`` so they apply to the
    run rather than to the output file.
    """
    return [
        "ffmpeg",
        "-y",
        "-nostats",
        "-progress", "pipe:1",
        "-i", source_mp4,
        "-vn",
        "-acodec", "libmp3lame",
        "-b:a", "320k",
        output_mp3,
    ]


# ``Duration: 00:01:23.11, start: 0.000000, bitrate: 129 kb/s``. Hours are not
# zero-padded to two digits for very long inputs, hence ``\d+``.
FFMPEG_DURATION_PATTERN = re.compile(r"\bDuration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")

# A ``-progress`` line is an unindented ``key=value``. Metadata lines from the
# input dump are indented and use ``key : value``, so they cannot match.
FFMPEG_PROGRESS_LINE_PATTERN = re.compile(r"^([a-z][a-z0-9_]*)=(.*)$")

FFMPEG_TIMESTAMP_PATTERN = re.compile(r"^(-?)(\d+):(\d{2}):(\d{2}(?:\.\d+)?)$")

FFMPEG_SPEED_PATTERN = re.compile(r"^(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)x$")

# Keys carrying the position reached in the input, best first. ``out_time`` is
# an unambiguous timestamp; ``out_time_us`` is microseconds by name and by
# behaviour. ``out_time_ms`` is deliberately last: ffmpeg has emitted
# MICROseconds under that name for years, so it is only trusted as a fallback
# and is read with the same microsecond scale ffmpeg actually uses.
FFMPEG_TIME_KEYS = ("out_time", "out_time_us", "out_time_ms")


def _finite(value):
    """float(value) when it is a real, finite number; None otherwise.

    Mirrors app.finite_number so the pure module stays importable on its own.
    NaN, infinities and booleans are rejected rather than propagated into a
    percentage.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def parse_ffmpeg_duration(line):
    """Total input seconds from an ffmpeg ``Duration:`` line, else None.

    ``Duration: N/A`` (streams with no known length) does not match, which is
    what keeps an unknown duration indeterminate instead of inventing one.
    """
    if not isinstance(line, str):
        return None
    match = FFMPEG_DURATION_PATTERN.search(line)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return total if total > 0 else None


def parse_ffmpeg_progress_line(line):
    """``(key, value)`` for one ffmpeg ``-progress`` line, else None."""
    if not isinstance(line, str):
        return None
    match = FFMPEG_PROGRESS_LINE_PATTERN.match(line)
    if not match:
        return None
    return match.group(1), match.group(2).strip()


def ffmpeg_progress_seconds(key, value):
    """Seconds processed, from one of the ``out_time*`` keys, else None.

    Negative positions (ffmpeg emits ``-577014:32:22.000000`` before the first
    frame is written) and ``N/A`` are rejected, not clamped to zero, so they
    never register as progress.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value == "N/A":
        return None

    if key == "out_time":
        match = FFMPEG_TIMESTAMP_PATTERN.match(value)
        if not match:
            return None
        sign, hours, minutes, seconds = match.groups()
        if sign == "-":
            return None
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    if key in ("out_time_us", "out_time_ms"):
        number = _finite(value)
        if number is None or number < 0:
            return None
        return number / 1_000_000.0

    return None


def parse_ffmpeg_speed(value):
    """Processing speed multiplier from ``speed=12.3x``, else None."""
    if not isinstance(value, str):
        return None
    match = FFMPEG_SPEED_PATTERN.match(value.strip())
    if not match:
        return None
    speed = _finite(match.group(1))
    if speed is None or speed <= 0:
        return None
    return speed


def compute_processing_percent(processed_seconds, duration_seconds, previous_percent=None):
    """Percent of the input processed, clamped to 0..100 and never decreasing.

    Returns None when the duration is unknown, so the caller keeps the artifact
    indeterminate rather than showing a fabricated number. A previously
    published percent is never lowered: ffmpeg can report a position that dips
    between blocks, and a progress bar that walks backwards reads as a bug.
    """
    processed = _finite(processed_seconds)
    duration = _finite(duration_seconds)
    previous = _finite(previous_percent)
    if previous is not None:
        previous = min(100.0, max(0.0, previous))

    if processed is None or duration is None or duration <= 0 or processed < 0:
        return previous

    percent = round(min(100.0, max(0.0, processed * 100.0 / duration)), 1)
    if previous is not None and percent < previous:
        return previous
    return percent


def compute_processing_eta(processed_seconds, duration_seconds, speed):
    """Whole seconds of wall-clock work left, else None.

    ``speed`` is ffmpeg's own multiplier (media seconds per wall-clock second),
    so remaining media time divided by it is a wall-clock estimate.
    """
    processed = _finite(processed_seconds)
    duration = _finite(duration_seconds)
    rate = _finite(speed)
    if processed is None or duration is None or rate is None:
        return None
    if duration <= 0 or rate <= 0 or processed < 0:
        return None
    remaining = duration - processed
    if remaining <= 0:
        return 0
    return int(round(remaining / rate))


def select_video_for_reuse(completed_video_artifacts):
    """Pick the best completed MP4 for MP3 derivation.

    Returns the artifact dict, or None if no usable video exists.
    Selection is stable: first completed video in execution order.
    """
    for art in completed_video_artifacts:
        if art.get("status") == "done" and art.get("file") and os.path.isfile(art["file"]):
            return art
    return None


def compute_aggregate_status(artifacts):
    """Compute parent job status from artifact statuses.

    Returns (parent_status, parent_phase).
    """
    if not artifacts:
        return "error", "error"

    statuses = [a["status"] for a in artifacts]
    non_terminal = [s for s in statuses if s not in ARTIFACT_TERMINAL]

    if non_terminal:
        if any(s in ("downloading", "retrying") for s in statuses):
            return "downloading", "downloading"
        if any(s == "processing" for s in statuses):
            return "downloading", "processing"
        return "downloading", "starting"

    done_count = sum(1 for s in statuses if s == "done")
    total = len(statuses)

    if done_count == total:
        return "done", "done"
    if done_count == 0:
        if all(s == "cancelled" for s in statuses):
            return "cancelled", "cancelled"
        if all(s == "timed_out" for s in statuses):
            return "timed_out", "timed_out"
        return "error", "error"
    return "partial", "partial"


def compute_aggregate_progress(artifacts, current_artifact_id):
    """Compute overall percent: (completed + current_pct/100) / total * 100.

    The current artifact contributes its own fractional progress, so it must be
    excluded from the completed count while it is being counted fractionally.
    Otherwise a finished current artifact is counted twice - once as completed
    and once at 100% - and a two-output job reports 150% the moment its last
    artifact publishes, because ``current_artifact_id`` keeps pointing at it.

    Any percent outside 0..100 is clamped before it enters the sum, so one bad
    artifact reading cannot push the aggregate out of range either.
    """
    total = len(artifacts)
    if total == 0:
        return None

    current_pct = 0.0
    current_counted = False
    if current_artifact_id is not None:
        for art in artifacts:
            if art["id"] == current_artifact_id:
                current_counted = True
                percent = _finite(art.get("percent"))
                if percent is not None:
                    current_pct = min(1.0, max(0.0, percent / 100.0))
                break

    completed = sum(
        1 for a in artifacts
        if a["status"] == "done" and not (current_counted and a["id"] == current_artifact_id)
    )

    return round(min(100.0, (completed + current_pct) / total * 100), 1)


def make_artifact_filename(title, artifact):
    """Generate a safe Content-Disposition filename for an artifact."""
    extension = ".mp3" if artifact["type"] == "audio" else ".mp4"
    if not isinstance(title, str):
        title = ""
    safe_title = "".join(
        char for char in title
        if char.isprintable() and char not in r'\\/:*?"<>|'
    ).strip(". ")[:100]
    suffix = artifact["label"]
    name = f"{safe_title} ({suffix}){extension}" if safe_title else f"{suffix}{extension}"
    return name
