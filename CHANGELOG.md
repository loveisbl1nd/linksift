# Changelog

All notable changes to LinkSift are documented here.

## [Unreleased]

## [0.3.0] - 2026-08-18

### Added

- Multi-output downloads: select multiple output formats for a single URL (e.g., MP4 + MP3). When you choose both video and audio, LinkSift automatically extracts audio from the downloaded video using ffmpeg, saving bandwidth and time. The UI displays per-artifact progress with individual save buttons for each completed output.
- New `partial` job status for multi-output jobs where some outputs succeed and others fail, allowing users to save the completed outputs while retrying the failed ones.
- Backend pipeline processes artifacts sequentially with a shared deadline and per-artifact retry logic; `/api/status/<job_id>` returns an `artifacts` array with individual progress for each output.

### Changed

- The parent `phase` now mirrors the current artifact's phase for every active phase (`starting`, `downloading`, `retrying`, `processing`), not just `retrying`/`processing`. Previously the parent sat on `starting` for the entire download of each artifact because a plain `downloading` artifact phase was not reflected. Terminal parent phases are never overwritten.
- `LINKSIFT_MAX_OUTPUTS_PER_JOB` (default 4, range 1–8, clamped at 8) caps how many outputs one request may declare; requests exceeding the limit are rejected with a 400 error.

### Fixed

- Cancellation and deadline expiry before publishing an ffmpeg-reuse output are now checked separately. Previously both raised `PipelineCancelled`, so a timed-out parent was reported as `cancelled` and followed the cancellation cleanup path instead of the timeout path. Now cancellation raises `PipelineCancelled` and deadline expiry raises `subprocess.TimeoutExpired`; `run_pipeline` converts the latter into a `timed_out` parent, and cancellation still wins the race when both are true.
- Unexpected exceptions out of `ffmpeg.communicate()` (e.g. `OSError`, `ValueError`) no longer leave a stale ffmpeg process behind. The `except` block now covers `BaseException`, reaps the process via the existing lifecycle helper, removes the temp output, and re-raises the original error without triggering the `return False` fallback path. The registry entry is evicted only if it still points at this process (identity check), so a slow unwind can never evict the next artifact's entry.

## [0.2.0] - 2026-08-14

### Added

- YouTube reliability hardening: the Docker image now bundles a pinned Deno runtime and yt-dlp-ejs so yt-dlp can solve YouTube's JavaScript challenges out of the box, and the startup updater refreshes yt-dlp together with yt-dlp-ejs (still skipped by `LINKSIFT_NO_UPDATE=1`).
- Fresh-extraction retries: transient download failures (HTTP 403/429/5xx, connection resets, network timeouts) re-run the full yt-dlp process for new signed media URLs, controlled by `LINKSIFT_JOB_RETRIES` (default 2 extra attempts) and `LINKSIFT_RETRY_BASE_DELAY` (default 2 s, exponential, capped) — all within the existing `LINKSIFT_DOWNLOAD_TIMEOUT` deadline. `.part` files are kept between attempts so downloads resume, `/api/status` gains additive `attempt`/`max_attempts` fields, and the UI shows "Retrying — attempt N of M".
- Optional "YouTube robust" mode: `docker-compose.youtube-robust.yml` adds a pinned bgutil PO token provider sidecar (internal-only, health-checked) and a `youtube-robust` image target with the matching pinned plugin, activated via `LINKSIFT_PO_TOKEN_PROVIDER_URL` (validated; applied to info, playlist, and download commands as `--extractor-args`).
- `/api/health` now reports additive `capabilities` (`youtube_js_runtime`, `youtube_ejs`, `po_token_provider_configured`, `po_token_provider`); missing capabilities are informational, never fatal. `po_token_provider_configured` means the environment URL is valid, while `po_token_provider` additionally requires the plugin to be installed — provider arguments are only passed to yt-dlp when both hold, and a single warning is logged when the URL is set on an image without the plugin.
- Download commands set explicit yt-dlp internal retry defaults (`--retries 10`, `--fragment-retries 10`, `--extractor-retries 3` with capped exponential retry sleeps).

- Bounded download scheduler: a fixed pool of `LINKSIFT_MAX_CONCURRENT_DOWNLOADS` workers drains a FIFO queue capped by `LINKSIFT_MAX_QUEUED_DOWNLOADS` (default 200); jobs beyond the worker limit now wait as `queued` instead of being rejected, and `POST /api/download` returns 429 only when the queue itself is full.
- Queued jobs can be cancelled before yt-dlp is ever spawned via the existing `DELETE /api/download/<job_id>` endpoint, immediately freeing queue capacity.
- `GET /api/status/<job_id>` now also reports `queue_position` (1-based while queued, otherwise null) and `started_at` (null until a worker picks the job up).
- Configurable fragment parallelism for DASH/HLS downloads via `LINKSIFT_CONCURRENT_FRAGMENTS` (default 4, clamped to 1–16), passed to yt-dlp as `--concurrent-fragments` together with explicit `--continue` and `--part`.
- Post-processing visibility: a dedicated yt-dlp postprocess progress template switches jobs to `phase: "processing"` while ffmpeg runs.

### Changed

- Download requests no longer spawn one thread per request; work is executed by the shared scheduler workers.
- Terminal job states (done, error, cancelled, timed_out) are sticky: late progress lines or a late-waking worker can no longer modify a finished job.
- `LINKSIFT_MAX_CONCURRENT_DOWNLOADS` is clamped to 1–16 so an environment typo cannot ask the scheduler for thousands of worker threads; `DownloadScheduler` applies the same bound when constructed directly.
- Scheduler worker startup is transactional: all workers start before the scheduler is published, and any thread-creation failure rolls back completely (started workers are shut down and joined, nothing stays queued). `POST /api/download` reports such failures as a stable `503 {"error": "Download scheduler is unavailable"}` and the next request recreates the scheduler.

### Fixed

- Removed the dequeue/status race: a worker now pops a job from the queue and marks it claimed in one atomic step under the jobs lock, so `GET /api/status/<job_id>` can no longer return `status: "queued"` with `queue_position: null` — a job is either queued with a 1-based position or already `downloading` with `started_at` set.
- Closed a cancellation race in the fresh-extraction retry loop: the cancellation check, `Popen` creation, and process-registry insertion now happen in one atomic critical section, so a cancellation recorded at any point before the spawn can never start another yt-dlp attempt, while a cancellation after the spawn is guaranteed to see and terminate the registered process.
- Queued downloads now render their real state in the UI — "Queued — #N in line" with a waiting hint — instead of appearing as active downloads with fake progress; queued cards stay cancellable, count as active, and keep polling until a worker claims them. A failed cancellation request restores the card's correct prior state, so a queued card keeps its queue position instead of briefly appearing as downloading.
- A failing claim hook no longer kills a scheduler worker or strands the job: the job is finalized atomically as `error` with the stable message "Download could not be started" (the internal exception goes only to the server log) and the same worker continues with the next queued job.

## [0.1.0] - 2026-08-14

### Added

- LinkSift public-project baseline: contribution guide, security policy, issue forms, pull-request template, and CI.
- Input validation, safe output selection, filename sanitization, and bounded concurrent downloads.
- Automatic lifecycle cleanup: finished jobs and their files expire after `LINKSIFT_JOB_TTL` seconds (default 24 hours), with orphan-file sweeps at startup and during normal request activity.
- Server-side download cancellation via `DELETE /api/download/<job_id>`, including process-tree termination, partial-file cleanup, and a Cancel button on active queue cards.
- Configurable playlist expansion limit via `LINKSIFT_MAX_PLAYLIST_ITEMS` (default 200); `/api/playlist` now reports `truncated` and `limit`, and the UI shows a gentle truncation notice.
- Tag-driven GitHub Releases with multi-architecture images published to GitHub Container Registry.
- OCI image metadata, SBOM generation, and GitHub build-provenance attestations for released images.
- Verified source provenance, preserved third-party notices, release instructions, and a contributor roadmap.

### Changed

- Rebranded from the inherited project identity to LinkSift.
- Hardened progress polling, browser folder saving, and frontend metadata rendering.
- Pinned CI and release actions to reviewed commit SHAs and documented the published-image deployment path.

### Fixed

- Playlist truncation is now detected from the raw yt-dlp entry count, so playlists containing unavailable entries still report `truncated` correctly; blank or malformed entries are skipped without failing the request.
- Container startup now normalizes the entrypoint to LF line endings, including builds made from a Windows working tree.

[Unreleased]: https://github.com/loveisbl1nd/linksift/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/loveisbl1nd/linksift/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/loveisbl1nd/linksift/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/loveisbl1nd/linksift/releases/tag/v0.1.0
