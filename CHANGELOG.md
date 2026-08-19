# Changelog

All notable changes to LinkSift are documented here.

## [Unreleased]

## [0.3.1] - 2026-08-19

### Added

- `POST /api/download` accepts two optional fields, `client_request_id` and `launch_source`. A request id is a 8–128 character `[A-Za-z0-9_-]` string that the UI generates once per intentional download action (`crypto.randomUUID()` where available, with a `getRandomValues` and then a `Math.random` fallback). Reusing an id with the same payload returns the original `job_id` together with `deduplicated: true` instead of creating a second job; reusing it with a different payload is rejected with 409. The duplicate check, the job insert, and the scheduler submit happen in one critical section under the jobs lock, so two concurrent retries of one click can never both reach the scheduler, and the id map is swept by the existing TTL cleanup so it can never outlive the job it points at. Requests that omit both fields behave exactly as before. Job creation and deduplication now emit a structured log line carrying only the job id, the launch source, and whether the job was new — no URL, title, cookie, token, or request body.
- `GET /api/status/<job_id>` reports three additive fields, `processed_seconds`, `duration_seconds`, and `processing_speed`, both per artifact and at the parent level (mirroring the current artifact). Every pre-existing field keeps its name and meaning, so a client that ignores the additions sees the response it saw before.

### Fixed

- A multi-output job's per-output rows no longer freeze while the parent bar keeps moving. `renderCard`'s fast path redrew only the parent progress bar and returned whenever the artifact signature was unchanged, but that signature truncated `percent` and `processed_seconds` to whole numbers and omitted `duration_seconds` and `processing_speed` entirely — all four of which the packaging row renders. So the Audio row held a stale position, a stale `12.3×` speed and a stale bar width while the parent advanced, and a bar that was indeterminate for want of a duration stayed indeterminate after one arrived. Artifact rows now carry a stable `data-artifact-id` and are patched in place from the live payload (status text, bar width, `aria-valuenow`), so a sub-integer percent move and a speed-only change both reach the row on the next poll. The signature now gates structure only — status, phase, save state, and whether a duration is known — which is what keeps the patch from rebuilding the card: Cancel keeps its focus and its hover/focus-visible treatment, listeners are not rebound, save state is not reset, and the artifact objects `saveArtifact` holds across an `await` are never replaced. Structural transitions (packaging → done, a Save button appearing, a bar being created or removed) still trigger a full render, and the artifact bar now exposes `role="progressbar"` with min/max/now like the parent bar already did.
- Audio packaging now reports real progress instead of appearing frozen. `ffmpeg` was spawned without machine-readable progress and awaited with a single blocking `communicate()` call, so an artifact sat at `processing` with `percent: None` from the first frame to the last, and a two-output job's parent bar stopped dead at 50% for the entire extraction. The extract command now passes `-progress pipe:1` and `-nostats` (both before `-i`, as global options), stderr is merged into stdout so one reader thread drains both without deadlocking, and a daemon reader parses each `key=value` block as ffmpeg emits it, publishing `processed_seconds`, `duration_seconds`, `percent`, `eta`, and `processing_speed` under the jobs lock. Duration comes from ffmpeg's own input dump rather than a second `ffprobe` process or a client-supplied value. Percent is clamped to 0–100 and never moves backwards; `N/A`, negative, NaN, and infinite readings are discarded rather than rendered; and when the duration is genuinely unknown the UI stays indeterminate instead of inventing a number. The shared deadline, cancellation, process-tree termination, the process registry with its identity-checked eviction, the pre-spawn gate, atomic temp-to-final publication, and the three-outcome contract (published / ordinary failure may fall back to yt-dlp / cancellation and timeout must not) are all unchanged, and a cancel or timeout during packaging still reaps the whole process tree and never falls back.
- A finished multi-output job reported more than 100% progress. `compute_aggregate_progress` counted the current artifact once in its completed tally and again through its own percentage, so the moment the last artifact published, a two-output job reported 150%, a three-output job 133.3%, and a four-output job 125% — the frontend only hid this because it clamps the bar width. The current artifact is now excluded from the completed count while it is being counted fractionally, the result is capped at 100, and a non-finite artifact percentage is ignored rather than propagated.
- The queue card no longer shows "Waiting for data" while ffmpeg packages an output. That branch keyed off downloaded bytes, which are absent during packaging, so the most informative moment of a multi-output job read as though nothing were happening. Packaging now shows "Processing audio" or "Packaging file", the media time processed as `42:10 / 1:23:11 processed`, the encoding speed as `12.3×` when ffmpeg reports it, and the remaining time when it can be computed; layout, responsive behavior, icons, fonts, and the existing hover and focus treatments are untouched.
- YouTube downloads that fail with HTTP 403 now retry with a different player client instead of repeating an identical, equally doomed attempt. HTTP 403 was already classified as transient, so the fresh-extraction loop re-ran the same command with the same default client and hit the same 403 every time. The next retry now rebuilds the command from scratch with `--extractor-args youtube:player_client=web_embedded`, but only when the hostname really resolves to YouTube (`youtube.com`, its subdomains, or `youtu.be` — a lookalike such as `youtube.com.evil.example` does not qualify) and yt-dlp's stderr genuinely reports `HTTP Error 403: Forbidden`. The first attempt deliberately keeps the default client, since some videos disable embedded playback. The fallback keeps the PO token provider arguments, selected format, output path, and progress contract; it is applied at most once per download and consumes one of the existing `LINKSIFT_JOB_RETRIES` attempts rather than adding new spawns; and cancellation, the shared deadline, and terminal job status are re-checked before the retry is spawned. Non-YouTube URLs and non-403 failures never change the client, and no cookies or account authentication were added.
- The queue card's output summary now carries type icons in every state, not just when the card is ready. While a job was downloading, errored, queued, or cancelled, the summary fell back to a text-only chip, so lines like `MP4 video 137` or `1 video + audio` lost the video/audio indicator the ready chips show. Each summary branch now emits the matching inline SVG — both icons for a mixed multi-output summary — while remaining a plain status label with no selection state.
- The hovered or keyboard-focused queue card no longer paints a highlight that is cut off flush at its left edge. `.card` had no border radius and no horizontal padding, so the hover gradient started on a square vertical edge against the thumbnail, and the clipping `.manifest` parent ruled out fixing it with a negative margin. The card now owns part of the row's horizontal inset and a border radius, so the highlight rounds off inside the row while staying aligned with the manifest header on both desktop and mobile. The separator moved from `border-bottom` (which curves at the corners of a rounded box) to an inset pseudo-element, suppressed on the last card, and `:focus-within` now mirrors the hover background from outside the pointer-only media query so keyboard and touch users get the same shape. Hover changes only the background, so nothing shifts, and a hovered error card still shows its danger styling.
- Vietnamese titles and uploaders now render with intact diacritics. `.card-title` no longer uses the decorative Instrument Serif face (which lacks Vietnamese combining-mark coverage) for provider-supplied metadata; it now uses a Vietnamese-capable sans stack (`--font-sans-vi`, falling back through Inter / "Segoe UI" / system-ui / sans-serif) with adjusted weight, line-height, and letter-spacing. Title and uploader strings are normalized to Unicode NFC before render so decomposed forms collapse to the precomposed spelling.
- Quality and "Audio only" output chips now show a selected state. `renderCard` previously toggled a `selected` CSS class that no rule matched, while the CSS styled `[aria-pressed="true"]` — an attribute the buttons never emitted. The chips now emit `aria-pressed` as the single source of truth for selection; the selected border, background, text color, and selection check all key off it, and a re-render preserves the clicked chip's focus.
- The primary controls now carry inline SVG icons (video/audio type indicators on quality chips and on the non-interactive output summary, download/save/retry/cancel on action buttons, and a selection check on format-switch, queue filter, and output chips). All icons use `currentColor`, are `aria-hidden`, and sit beside a readable text label; no icon font, sprite, package, or CDN was added, and no Unicode glyph is used as a control icon. The selection check replaces a previous `content: "✓"` CSS pseudo-element that depended on the page font shipping a glyph for U+2713; the check is now an inline SVG shown via `aria-pressed`, with a fixed footprint so button width stays stable on toggle.

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

[Unreleased]: https://github.com/loveisbl1nd/linksift/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/loveisbl1nd/linksift/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/loveisbl1nd/linksift/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/loveisbl1nd/linksift/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/loveisbl1nd/linksift/releases/tag/v0.1.0
