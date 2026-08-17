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
                         concurrent_fragments, runtime_args):
    """Build the yt-dlp argv list for downloading one artifact."""
    out_template = artifact_output_template(job_id, artifact["id"], download_dir)
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
    """
    return [
        "ffmpeg",
        "-y",
        "-i", source_mp4,
        "-vn",
        "-acodec", "libmp3lame",
        "-b:a", "320k",
        output_mp3,
    ]


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
    """Compute overall percent: (completed + current_pct/100) / total * 100."""
    total = len(artifacts)
    if total == 0:
        return None

    completed = sum(1 for a in artifacts if a["status"] == "done")
    current_pct = 0.0
    if current_artifact_id is not None:
        for art in artifacts:
            if art["id"] == current_artifact_id and art.get("percent") is not None:
                current_pct = art["percent"] / 100.0
                break

    return round((completed + current_pct) / total * 100, 1)


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
