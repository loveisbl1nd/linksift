# LinkSift roadmap

LinkSift is a local-first, self-hosted utility. The roadmap favors reliability, understandable behavior, and a low-friction contributor experience over turning the project into a hosted platform.

## Project principles

- Keep the default deployment local and private: no accounts, telemetry, or cloud dependency.
- Make downloads bounded, cancellable, observable, and easy to clean up.
- Keep tests deterministic and offline; external-site checks belong in documented manual smoke tests.
- Preserve a small deployment surface: one container, one named volume, and one supported worker model.
- Accept focused changes that can be reviewed, tested, documented, and safely maintained.

## v0.1 — Reliable distribution

Available in the current source tree:

- [x] bounded download concurrency, cancellation, timeouts, and TTL cleanup;
- [x] playlist safety limits and malformed-entry handling;
- [x] offline unit and regression tests;
- [x] tag-driven GitHub Releases and multi-architecture GHCR images;
- [x] OCI metadata, SBOM generation, and verifiable build provenance;
- [x] explicit source provenance and third-party license notices.

Release milestone:

- [x] publish and verify the first `v0.1.0` release;
- [ ] validate the published image on Linux amd64, Linux arm64, Windows Docker Desktop, and macOS Docker Desktop;
- [ ] record a short, repeatable manual smoke-test checklist for common download flows.

## v0.2 — Queueing, performance, and compatibility

Included in `v0.2.0`:

- [x] bounded FIFO scheduler with configurable worker and queue limits;
- [x] cancellable queued jobs, queue positions, and explicit post-processing status;
- [x] configurable fragment concurrency and resumable partial downloads;
- [x] fresh-extraction retries within one total timeout budget;
- [x] bundled YouTube JavaScript challenge support and optional PO-token robust mode;
- [x] regression coverage for scheduler startup, claim, cancellation, retry, and status races.

## v0.3 — Multi-output download pipeline

Implemented in the current source tree; not yet released under a versioned tag:

- [x] one inspected URL can request multiple explicit output profiles, such as MP4 plus MP3 or multiple video qualities;
- [x] one request is represented as a parent job with independently visible output artifacts (`a000`, `a001`, …), each with its own status, phase, progress, and Save button;
- [x] source media is reused where practical — an audio artifact extracts MP3 from an already-downloaded MP4 via ffmpeg instead of re-downloading;
- [x] queue limits, cancellation, TTL cleanup, and total resource bounds remain correct for multi-output jobs: one parent holds one queue entry, one worker slot, and one subprocess registry slot, with artifacts running sequentially under one shared deadline;
- [x] command construction and lifecycle state are separated into a testable pure-logic module (`output_pipeline.py`) before the output graph expands;
- [x] the safe interface is preserved without exposing arbitrary yt-dlp or shell arguments (`format_id` is validated against a strict allowlist).

## Non-goals

The project is not planning to become:

- a public hosted downloader, multi-tenant service, or commercial SaaS backend;
- a DRM circumvention or access-control bypass tool;
- a media archive, library manager, or permanent job database;
- a telemetry or user-tracking system;
- a compatibility promise for every site listed by yt-dlp.

## How to contribute to the roadmap

Small documentation, accessibility, and regression-test improvements can go directly to a focused pull request. For changes to architecture, dependencies, network exposure, persisted state, or supported output behavior, open a feature request first and include the user problem, proposed scope, security implications, and an offline testing plan.

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and validation commands. Roadmap placement communicates direction, not a deadline or guarantee that a proposal will be accepted unchanged.
