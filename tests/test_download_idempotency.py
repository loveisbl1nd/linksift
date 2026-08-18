"""Contract tests for download-launch idempotency and the packaging status fields.

Two independent defects are locked down here.

Launch idempotency (``POST /api/download``):
  * A retried launch (double-click, flaky network, a browser replaying the POST)
    used to create a SECOND job for the same click: the endpoint minted a fresh
    ``uuid`` per request and submitted it, so one user action could occupy two
    queue slots and download the same file twice. ``client_request_id`` now makes
    the endpoint idempotent, and the check + insert + submit share ONE
    ``jobs_lock`` critical section so two concurrent retries cannot both win.
  * The same id reused for a DIFFERENT payload must be rejected (409) rather than
    answered with the other job's id, which would hand the client progress for a
    download it never asked for.
  * Requests without ``client_request_id`` keep their old behavior verbatim: a
    new job every time and no ``deduplicated`` key in the response.
  * The launch logs carry ids and states only. A log line containing the URL or
    the video title would leak what the user is downloading into whatever
    aggregates the server log, so the markers used below must never appear.
  * ``request_ids`` must not outlive the jobs it protects: ``run_cleanup``
    drops the entry when the job expires, but only when the entry still points
    at that job (an id already reassigned to a newer job must survive).

Packaging progress in ``/api/status`` (Part A API surface):
  * While ffmpeg packaged an artifact the response carried no time-based
    progress at all, so the parent card froze at the percent of the last
    completed artifact. ``processed_seconds`` / ``duration_seconds`` /
    ``processing_speed`` are now published per artifact AND mirrored onto the
    parent, letting the parent walk from 50% to 100% across the packaging step.
  * The three fields are ADDITIVE: every pre-existing key keeps its name and
    meaning, and the new keys are always present (null when the job never
    reached ffmpeg) so a client never has to tell "absent" from "unknown".
"""
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import app
import output_pipeline as pipeline


# Keys the status payload exposed before the packaging fields were added. Every
# one of them must survive unchanged.
LEGACY_PARENT_KEYS = frozenset({
    "status",
    "phase",
    "downloaded_bytes",
    "total_bytes",
    "speed",
    "eta",
    "percent",
    "error",
    "filename",
    "queue_position",
    "started_at",
    "attempt",
    "max_attempts",
    "artifacts",
    "current_artifact_id",
})

LEGACY_ARTIFACT_KEYS = frozenset({
    "id",
    "type",
    "format_id",
    "status",
    "phase",
    "filename",
    "downloaded_bytes",
    "total_bytes",
    "speed",
    "eta",
    "percent",
    "error",
    "attempt",
    "max_attempts",
})

PACKAGING_KEYS = frozenset({"processed_seconds", "duration_seconds", "processing_speed"})

# Deliberately distinctive so an accidental leak into a log line is unmistakable.
MARKER_URL = "https://secret.example/xyzzy-marker"
MARKER_TITLE = "MARKER-TITLE-9f2a"

BASE_OUTPUTS = [
    {"type": "video", "format_id": "137"},
    {"type": "audio"},
]


class RecordingScheduler:
    """Scheduler double that counts submits instead of running anything."""

    def __init__(self, accept=True):
        self.accept = accept
        self.submitted = []
        self._lock = threading.Lock()

    def submit(self, job_id, task):
        with self._lock:
            self.submitted.append(job_id)
        return self.accept

    def queue_position(self, job_id):
        return 1

    def cancel_queued(self, job_id):
        return False

    @property
    def submit_count(self):
        with self._lock:
            return len(self.submitted)


class RendezvousRequestIds(dict):
    """``request_ids`` double that makes the first two lookups overlap.

    The lookup IS the duplicate check, so installing the rendezvous here pins
    the synchronization to the exact instruction under test.

    Order matters: each call reads the map FIRST and only then waits. Waiting
    before the read is not enough - releasing both threads still lets one of
    them run the whole insert-and-register stretch before the other's read
    executes, so the loser observes the entry and deduplicates even when the
    check sits outside the lock. Reading first forces both threads to observe
    the map as it was before either of them wrote to it.

    * Real implementation (check inside ``jobs_lock``): the second thread is
      blocked on the lock and never reaches the read, so the first thread's
      wait times out, it proceeds and registers, and the second thread then
      reads the entry and deduplicates. One job.
    * Check moved outside the lock: both threads read ``None``, are released
      together, and both create a job - the double launch this test forbids.

    The wait is bounded by ``timeout``, so the passing path costs that once and
    can never hang.
    """

    def __init__(self, parties=2, timeout=0.5):
        super().__init__()
        self.parties = parties
        self.timeout = timeout
        self.barrier = threading.Barrier(parties)
        self.get_calls = 0
        self._counter_lock = threading.Lock()

    def get(self, key, default=None):
        with self._counter_lock:
            self.get_calls += 1
            rendezvous = self.get_calls <= self.parties
        # Read before synchronizing - see the class docstring.
        value = super().get(key, default)
        if rendezvous:
            try:
                self.barrier.wait(timeout=self.timeout)
            except threading.BrokenBarrierError:
                # The first thread timed out holding the lock: the correct path.
                pass
        return value


class DownloadIdempotencyTestCase(unittest.TestCase):
    """Shared fixture: clean registries, a runnable runtime, a fake scheduler."""

    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        app.request_ids.clear()
        self.addCleanup(app.jobs.clear)
        self.addCleanup(app.processes.clear)
        self.addCleanup(app.request_ids.clear)

        # The before_request cleanup hook must not fire mid-test and evict the
        # jobs these tests just created.
        previous_cleanup_mark = app.last_cleanup_monotonic
        app.last_cleanup_monotonic = time.monotonic()
        self.addCleanup(setattr, app, "last_cleanup_monotonic", previous_cleanup_mark)

        # /api/download refuses to run without yt-dlp AND ffmpeg on PATH; this
        # machine has no yt-dlp, so pretend both are installed.
        which_patch = patch.object(app.shutil, "which", return_value="/usr/bin/tool")
        which_patch.start()
        self.addCleanup(which_patch.stop)

        self.scheduler = RecordingScheduler()
        scheduler_patch = patch.object(app, "get_scheduler", return_value=self.scheduler)
        scheduler_patch.start()
        self.addCleanup(scheduler_patch.stop)

        self.client = app.app.test_client()

    def payload(self, **overrides):
        body = {
            "url": MARKER_URL,
            "title": MARKER_TITLE,
            "outputs": [dict(output) for output in BASE_OUTPUTS],
        }
        body.update(overrides)
        return body


class ClientRequestIdDeduplicationTests(DownloadIdempotencyTestCase):
    def test_repeated_launch_returns_same_job_and_submits_once(self):
        """A retried POST must reuse the first job instead of queueing a second.

        Before idempotency the endpoint minted a fresh job id per request, so
        this scenario produced two job ids and two scheduler submits for one
        user action.
        """
        body = self.payload(client_request_id="retry-id-0001", launch_source="single-card")

        first = self.client.post("/api/download", json=body)
        second = self.client.post("/api/download", json=body)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_payload = first.get_json()
        second_payload = second.get_json()
        self.assertEqual(second_payload["job_id"], first_payload["job_id"])
        self.assertTrue(second_payload["deduplicated"])
        self.assertNotIn("deduplicated", first_payload)
        self.assertEqual(self.scheduler.submitted, [first_payload["job_id"]])
        self.assertEqual(list(app.jobs), [first_payload["job_id"]])
        self.assertEqual(
            app.request_ids["retry-id-0001"]["job_id"], first_payload["job_id"]
        )

    def test_job_records_the_client_request_id_that_created_it(self):
        """The job keeps its id so cleanup can find the map entry to drop."""
        body = self.payload(client_request_id="stored-id-0001")
        job_id = self.client.post("/api/download", json=body).get_json()["job_id"]
        self.assertEqual(app.jobs[job_id]["client_request_id"], "stored-id-0001")

    def test_client_request_id_is_whitespace_normalized_before_matching(self):
        """A padded id is the SAME id, so it must dedupe rather than fork a job."""
        first = self.client.post(
            "/api/download", json=self.payload(client_request_id="padded-id-0001")
        ).get_json()
        second = self.client.post(
            "/api/download", json=self.payload(client_request_id="  padded-id-0001  ")
        ).get_json()

        self.assertEqual(second["job_id"], first["job_id"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(self.scheduler.submit_count, 1)
        self.assertEqual(list(app.request_ids), ["padded-id-0001"])

    def test_concurrent_identical_launches_submit_exactly_one_job(self):
        """Two overlapping retries must not both create a job.

        The duplicate check, the job insert and the scheduler submit share one
        critical section. If the check ever moved out of the lock, both threads
        would miss each other's entry and queue the same download twice.

        The rendezvous is installed on ``request_ids.get`` - the duplicate check
        itself - so it is the LOCK that decides the outcome. Synchronizing any
        earlier (on the fingerprint call, say) does not test the lock at all:
        the winning thread runs the whole stretch from there to the check
        without yielding, so the loser arrives late and deduplicates even when
        the check has been moved outside the lock.
        """
        racing_ids = RendezvousRequestIds(parties=2, timeout=0.5)
        results = []
        results_lock = threading.Lock()

        def launch():
            client = app.app.test_client()
            response = client.post(
                "/api/download",
                json=self.payload(
                    client_request_id="racing-id-0001", launch_source="download-all"
                ),
            )
            with results_lock:
                results.append((response.status_code, response.get_json()))

        with patch.object(app, "request_ids", racing_ids):
            threads = [threading.Thread(target=launch) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

            for thread in threads:
                self.assertFalse(thread.is_alive(), "launch thread did not finish")
            # Both threads really did run the duplicate check; without this the
            # assertions below could pass on a handler that never checked.
            self.assertEqual(racing_ids.get_calls, 2)
            request_id_keys = list(racing_ids)

        self.assertEqual(len(results), 2)
        self.assertEqual({status for status, _ in results}, {200})

        job_ids = {body["job_id"] for _, body in results}
        self.assertEqual(len(job_ids), 1, f"concurrent launches forked jobs: {job_ids}")
        deduplicated = [body.get("deduplicated") for _, body in results]
        self.assertEqual(sorted(str(flag) for flag in deduplicated), ["None", "True"])

        self.assertEqual(self.scheduler.submit_count, 1)
        self.assertEqual(list(app.jobs), list(job_ids))
        self.assertEqual(request_id_keys, ["racing-id-0001"])

    def test_reused_id_with_different_payload_is_rejected(self):
        """Answering a changed payload with the old job id would be a wrong answer."""
        original = self.payload(client_request_id="conflict-id-01")
        first = self.client.post("/api/download", json=original)
        self.assertEqual(first.status_code, 200)
        original_job_id = first.get_json()["job_id"]

        variants = {
            "format_id": self.payload(
                client_request_id="conflict-id-01",
                outputs=[{"type": "video", "format_id": "136"}, {"type": "audio"}],
            ),
            "url": self.payload(
                client_request_id="conflict-id-01", url="https://other.example/video"
            ),
            "title": self.payload(
                client_request_id="conflict-id-01", title="A Different Title"
            ),
            "outputs_subset": self.payload(
                client_request_id="conflict-id-01",
                outputs=[{"type": "video", "format_id": "137"}],
            ),
            "outputs_order": self.payload(
                client_request_id="conflict-id-01",
                outputs=[{"type": "audio"}, {"type": "video", "format_id": "137"}],
            ),
        }

        for label, body in variants.items():
            with self.subTest(difference=label):
                response = self.client.post("/api/download", json=body)
                self.assertEqual(response.status_code, 409)
                self.assertIn("error", response.get_json())
                self.assertNotIn("job_id", response.get_json())
                # No extra job, no extra submit, and the map still points at the
                # original launch.
                self.assertEqual(self.scheduler.submitted, [original_job_id])
                self.assertEqual(list(app.jobs), [original_job_id])
                self.assertEqual(
                    app.request_ids["conflict-id-01"]["job_id"], original_job_id
                )


class LegacyClientCompatibilityTests(DownloadIdempotencyTestCase):
    def test_requests_without_client_request_id_keep_creating_new_jobs(self):
        """Clients predating the field must be untouched by the dedupe path."""
        first = self.client.post("/api/download", json=self.payload())
        second = self.client.post("/api/download", json=self.payload())

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_payload = first.get_json()
        second_payload = second.get_json()
        self.assertIn("job_id", first_payload)
        self.assertIn("job_id", second_payload)
        self.assertNotEqual(first_payload["job_id"], second_payload["job_id"])
        self.assertNotIn("deduplicated", first_payload)
        self.assertNotIn("deduplicated", second_payload)
        self.assertEqual(self.scheduler.submit_count, 2)
        self.assertEqual(app.request_ids, {})

    def test_explicit_null_client_request_id_behaves_like_omitted(self):
        response = self.client.post(
            "/api/download", json=self.payload(client_request_id=None)
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("deduplicated", response.get_json())
        self.assertEqual(app.request_ids, {})


class LaunchFieldValidationTests(DownloadIdempotencyTestCase):
    def test_malformed_client_request_id_is_rejected_without_creating_a_job(self):
        """A malformed id must fail closed: no job, no queue slot, no map entry.

        The charset is restricted so the value is safe to drop into a log line
        unescaped; a value carrying a newline could forge a second log record.
        """
        cases = {
            "number": 12345,
            "float": 1.5,
            "bool": True,
            "list": ["abcdefgh"],
            "dict": {"id": "abcdefgh"},
            "too_short": "abc1234",
            "too_long": "a" * 129,
            "space": "abc def123",
            "semicolon": "aaaaaaaa;drop",
            "embedded_newline": "aaaa\naaaa",
            "trailing_newline_with_text": "aaaaaaaa\nx",
            "empty": "",
        }
        for label, value in cases.items():
            with self.subTest(client_request_id=label):
                response = self.client.post(
                    "/api/download", json=self.payload(client_request_id=value)
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.get_json())
                self.assertEqual(app.jobs, {})
                self.assertEqual(app.request_ids, {})
                self.assertEqual(self.scheduler.submit_count, 0)

    def test_client_request_id_boundary_lengths_are_accepted(self):
        for label, value in (("min_8", "a" * 8), ("max_128", "b" * 128)):
            with self.subTest(client_request_id=label):
                app.jobs.clear()
                app.request_ids.clear()
                response = self.client.post(
                    "/api/download", json=self.payload(client_request_id=value)
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn(value, app.request_ids)

    def test_unknown_launch_source_is_rejected(self):
        """launch_source is a closed set the frontend owns; anything else is a bug."""
        for label, value in (
            ("unknown_word", "reload"),
            ("number", 7),
            ("list", ["single-card"]),
            ("empty", ""),
        ):
            with self.subTest(launch_source=label):
                response = self.client.post(
                    "/api/download", json=self.payload(launch_source=value)
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.get_json())
                self.assertEqual(app.jobs, {})
                self.assertEqual(self.scheduler.submit_count, 0)

    def test_known_launch_sources_are_accepted(self):
        for source in ("single-card", "download-all"):
            with self.subTest(launch_source=source):
                app.jobs.clear()
                app.request_ids.clear()
                response = self.client.post(
                    "/api/download", json=self.payload(launch_source=source)
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn("job_id", response.get_json())

    def test_missing_launch_source_is_accepted(self):
        response = self.client.post("/api/download", json=self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertIn("job_id", response.get_json())


class LaunchLoggingTests(DownloadIdempotencyTestCase):
    def assert_no_payload_leak(self, records):
        for record in records:
            message = record.getMessage()
            self.assertNotIn(MARKER_URL, message)
            self.assertNotIn(MARKER_TITLE, message)
            self.assertNotIn("xyzzy-marker", message)
            self.assertNotIn("MARKER-TITLE", message)
            self.assertNotIn("secret.example", message)

    def test_accepted_launch_logs_ids_and_state_only(self):
        """The accept log must identify the launch without describing it.

        Before this log existed there was no way to tell a deduplicated launch
        from a fresh one in production; the fix must not pay for that visibility
        with the user's URL or video title.
        """
        body = self.payload(client_request_id="logging-id-001", launch_source="download-all")
        with self.assertLogs(app.app.logger, level="INFO") as captured:
            response = self.client.post("/api/download", json=body)

        job_id = response.get_json()["job_id"]
        messages = [record.getMessage() for record in captured.records]
        accepted = [line for line in messages if "accepted" in line]
        self.assertEqual(len(accepted), 1, messages)
        self.assertIn(job_id, accepted[0])
        self.assertIn("download-all", accepted[0])
        self.assertIn("deduplicated false", accepted[0])
        self.assert_no_payload_leak(captured.records)

    def test_deduplicated_launch_logs_the_reused_job_and_state(self):
        body = self.payload(client_request_id="logging-id-002", launch_source="single-card")
        first_job_id = self.client.post("/api/download", json=body).get_json()["job_id"]

        with self.assertLogs(app.app.logger, level="INFO") as captured:
            second = self.client.post("/api/download", json=body)

        self.assertTrue(second.get_json()["deduplicated"])
        messages = [record.getMessage() for record in captured.records]
        dedup = [line for line in messages if "deduplicated true" in line]
        self.assertEqual(len(dedup), 1, messages)
        self.assertIn(first_job_id, dedup[0])
        self.assertIn("single-card", dedup[0])
        self.assert_no_payload_leak(captured.records)

    def test_conflict_warning_names_the_existing_job_but_not_the_payload(self):
        body = self.payload(client_request_id="logging-id-003", launch_source="single-card")
        first_job_id = self.client.post("/api/download", json=body).get_json()["job_id"]

        conflicting = self.payload(
            client_request_id="logging-id-003",
            launch_source="download-all",
            url="https://secret.example/xyzzy-marker-two",
            title="MARKER-TITLE-9f2a-two",
        )
        with self.assertLogs(app.app.logger, level="INFO") as captured:
            response = self.client.post("/api/download", json=conflicting)

        self.assertEqual(response.status_code, 409)
        warnings = [
            record for record in captured.records if record.levelname == "WARNING"
        ]
        self.assertEqual(len(warnings), 1, [r.getMessage() for r in captured.records])
        message = warnings[0].getMessage()
        self.assertIn(first_job_id, message)
        self.assertIn("download-all", message)
        self.assert_no_payload_leak(captured.records)
        self.assertNotIn("xyzzy-marker-two", message)
        self.assertNotIn("MARKER-TITLE-9f2a-two", message)

    def test_launch_without_launch_source_logs_a_placeholder(self):
        with self.assertLogs(app.app.logger, level="INFO") as captured:
            response = self.client.post(
                "/api/download", json=self.payload(client_request_id="logging-id-004")
            )
        job_id = response.get_json()["job_id"]
        accepted = [
            line for line in (r.getMessage() for r in captured.records) if "accepted" in line
        ]
        self.assertEqual(len(accepted), 1)
        self.assertIn(job_id, accepted[0])
        self.assertIn("unspecified", accepted[0])


class QueueFullLaunchTests(DownloadIdempotencyTestCase):
    def test_rejected_submit_leaves_no_job_and_no_request_id_entry(self):
        """A full queue must roll back the map entry too.

        A leftover request_ids entry would make every retry of that launch
        answer 200 with a job_id that no longer exists, so the card would poll a
        404 forever instead of re-queueing.
        """
        self.scheduler.accept = False
        response = self.client.post(
            "/api/download",
            json=self.payload(client_request_id="queuefull-id-1", launch_source="single-card"),
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(app.jobs, {})
        self.assertEqual(app.request_ids, {})

    def test_retry_after_queue_full_can_create_a_job(self):
        self.scheduler.accept = False
        first = self.client.post(
            "/api/download", json=self.payload(client_request_id="queuefull-id-2")
        )
        self.assertEqual(first.status_code, 429)

        self.scheduler.accept = True
        second = self.client.post(
            "/api/download", json=self.payload(client_request_id="queuefull-id-2")
        )
        self.assertEqual(second.status_code, 200)
        self.assertNotIn("deduplicated", second.get_json())
        self.assertIn("queuefull-id-2", app.request_ids)


class RequestIdCleanupTests(unittest.TestCase):
    """run_cleanup must retire idempotency entries with the jobs they protect."""

    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        app.request_ids.clear()
        self.addCleanup(app.jobs.clear)
        self.addCleanup(app.processes.clear)
        self.addCleanup(app.request_ids.clear)

        scheduler_patch = patch.object(app, "scheduler", Mock())
        scheduler_patch.start()
        self.addCleanup(scheduler_patch.stop)

    def run_cleanup_in_temp_dir(self, now):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            app, "DOWNLOAD_DIR", temp_dir
        ), patch.object(app, "get_job_ttl", return_value=100):
            app.run_cleanup(now=now)

    def test_expired_job_releases_its_client_request_id(self):
        """Without this, request_ids grows for the lifetime of the process."""
        now = time.time()
        job_id = "expiredjob"
        app.jobs[job_id] = {
            "id": job_id,
            "status": "done",
            "created_at": now - 900,
            "finished_at": now - 500,
            "client_request_id": "expired-id-001",
        }
        app.request_ids["expired-id-001"] = {"job_id": job_id, "fingerprint": "abc"}

        self.run_cleanup_in_temp_dir(now)

        self.assertNotIn(job_id, app.jobs)
        self.assertNotIn("expired-id-001", app.request_ids)

    def test_cleanup_keeps_entries_reassigned_to_a_newer_job(self):
        """An id already pointing at a newer job must survive the old job's TTL.

        Popping unconditionally would delete the live launch's protection, so
        the next retry of the NEW launch would queue a duplicate download.
        """
        now = time.time()
        old_job_id = "oldjob0001"
        new_job_id = "newjob0002"
        app.jobs[old_job_id] = {
            "id": old_job_id,
            "status": "done",
            "created_at": now - 900,
            "finished_at": now - 500,
            "client_request_id": "reused-id-0001",
        }
        app.jobs[new_job_id] = {
            "id": new_job_id,
            "status": "queued",
            "created_at": now,
            "finished_at": None,
            "client_request_id": "reused-id-0001",
        }
        app.request_ids["reused-id-0001"] = {"job_id": new_job_id, "fingerprint": "abc"}

        self.run_cleanup_in_temp_dir(now)

        self.assertNotIn(old_job_id, app.jobs)
        self.assertIn(new_job_id, app.jobs)
        self.assertEqual(app.request_ids["reused-id-0001"]["job_id"], new_job_id)

    def test_unexpired_job_keeps_its_client_request_id(self):
        now = time.time()
        job_id = "freshjob01"
        app.jobs[job_id] = {
            "id": job_id,
            "status": "done",
            "created_at": now - 50,
            "finished_at": now - 10,
            "client_request_id": "fresh-id-00001",
        }
        app.request_ids["fresh-id-00001"] = {"job_id": job_id, "fingerprint": "abc"}

        self.run_cleanup_in_temp_dir(now)

        self.assertIn(job_id, app.jobs)
        self.assertIn("fresh-id-00001", app.request_ids)

    def test_expired_job_without_a_client_request_id_is_handled(self):
        now = time.time()
        job_id = "legacyjob1"
        app.jobs[job_id] = {
            "id": job_id,
            "status": "error",
            "created_at": now - 900,
            "finished_at": now - 500,
            "client_request_id": None,
        }
        app.request_ids["unrelated-id-1"] = {"job_id": "otherjob99", "fingerprint": "abc"}

        self.run_cleanup_in_temp_dir(now)

        self.assertNotIn(job_id, app.jobs)
        self.assertIn("unrelated-id-1", app.request_ids)


class DownloadRequestFingerprintTests(unittest.TestCase):
    """The fingerprint decides dedupe-vs-conflict; it must be pure and total."""

    def outputs(self):
        return [
            {"type": "video", "format_id": "137"},
            {"type": "audio", "format_id": None},
        ]

    def test_identical_requests_hash_identically(self):
        first = app.download_request_fingerprint("u", "t", self.outputs())
        second = app.download_request_fingerprint("u", "t", self.outputs())
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_every_component_changes_the_digest(self):
        baseline = app.download_request_fingerprint("u", "t", self.outputs())
        variants = {
            "url": ("u2", "t", self.outputs()),
            "title": ("u", "t2", self.outputs()),
            "format_id": ("u", "t", [
                {"type": "video", "format_id": "136"},
                {"type": "audio", "format_id": None},
            ]),
            "type": ("u", "t", [
                {"type": "audio", "format_id": "137"},
                {"type": "audio", "format_id": None},
            ]),
            "fewer_outputs": ("u", "t", [{"type": "video", "format_id": "137"}]),
        }
        for label, args in variants.items():
            with self.subTest(difference=label):
                self.assertNotEqual(baseline, app.download_request_fingerprint(*args))

    def test_output_order_is_significant(self):
        """outputs is a list, so reordering is a different ask, not the same one."""
        forward = app.download_request_fingerprint("u", "t", self.outputs())
        reversed_outputs = list(reversed(self.outputs()))
        self.assertNotEqual(forward, app.download_request_fingerprint("u", "t", reversed_outputs))

    def test_extra_output_keys_are_ignored(self):
        """Only type and format_id define the ask; incidental keys must not matter."""
        plain = app.download_request_fingerprint("u", "t", self.outputs())
        decorated = app.download_request_fingerprint("u", "t", [
            {"type": "video", "format_id": "137", "label": "MP4 137"},
            {"type": "audio", "format_id": None, "label": "MP3 audio"},
        ])
        self.assertEqual(plain, decorated)

    def test_empty_outputs_still_hashes(self):
        self.assertEqual(len(app.download_request_fingerprint("u", "t", [])), 64)


class PackagingStatusFieldTests(unittest.TestCase):
    """/api/status must expose ffmpeg progress per artifact and on the parent."""

    def setUp(self):
        app.jobs.clear()
        app.processes.clear()
        app.request_ids.clear()
        self.addCleanup(app.jobs.clear)
        self.addCleanup(app.processes.clear)
        self.addCleanup(app.request_ids.clear)

        previous_cleanup_mark = app.last_cleanup_monotonic
        app.last_cleanup_monotonic = time.monotonic()
        self.addCleanup(setattr, app, "last_cleanup_monotonic", previous_cleanup_mark)

        self.client = app.app.test_client()

    def make_packaging_job(self, job_id="packagingj"):
        execution_order, display_order = pipeline.plan_artifacts([
            {"type": "video", "format_id": "137"},
            {"type": "audio", "format_id": None},
        ])
        video, audio = display_order
        video.update({
            "status": "done",
            "phase": "done",
            "percent": 100.0,
            "filename": "clip (MP4 137).mp4",
            "attempt": 1,
            "max_attempts": 3,
        })
        audio.update({
            "status": "processing",
            "phase": "processing",
            "percent": 40.0,
            "processed_seconds": 33.2,
            "duration_seconds": 83.0,
            "processing_speed": 12.3,
            "eta": 4,
            "attempt": 1,
            "max_attempts": 3,
        })
        app.jobs[job_id] = {
            "id": job_id,
            "status": "downloading",
            "phase": "downloading",
            "url": "https://example.test/video",
            "title": "clip",
            "artifacts": display_order,
            "execution_order": execution_order,
            "current_artifact_id": audio["id"],
            "downloaded_bytes": 0,
            "total_bytes": None,
            "speed": None,
            "eta": None,
            "percent": None,
            "created_at": time.time(),
            "started_at": 10.0,
            "finished_at": None,
            "client_request_id": None,
        }
        return job_id, video, audio

    def test_packaging_artifact_reports_time_progress(self):
        """The packaging artifact must carry its own ffmpeg time figures.

        Before this, an artifact in `processing` reported only a stale percent,
        so the UI had nothing to render but "Waiting for data".
        """
        job_id, _, audio = self.make_packaging_job()
        payload = self.client.get(f"/api/status/{job_id}").get_json()

        packaging = payload["artifacts"][1]
        self.assertEqual(packaging["id"], audio["id"])
        self.assertEqual(packaging["type"], "audio")
        self.assertEqual(packaging["status"], "processing")
        self.assertEqual(packaging["processed_seconds"], 33.2)
        self.assertEqual(packaging["duration_seconds"], 83.0)
        self.assertEqual(packaging["processing_speed"], 12.3)
        self.assertEqual(packaging["eta"], 4)

    def test_parent_mirrors_the_packaging_artifact_and_passes_fifty_percent(self):
        """The parent must advance from 50% to 100% while ffmpeg packages.

        With two artifacts and the first done, the parent used to sit at exactly
        50.0 for the whole packaging step because the audio artifact contributed
        no progress. It now contributes its ffmpeg percent, so a 40%-packaged
        second artifact reads 70.0 on the parent.
        """
        job_id, _, _ = self.make_packaging_job()
        payload = self.client.get(f"/api/status/{job_id}").get_json()

        self.assertEqual(payload["percent"], 70.0)
        self.assertGreater(payload["percent"], 50.0)
        self.assertEqual(payload["processed_seconds"], 33.2)
        self.assertEqual(payload["duration_seconds"], 83.0)
        self.assertEqual(payload["processing_speed"], 12.3)
        self.assertEqual(payload["eta"], 4)
        self.assertEqual(payload["phase"], "processing")

    def test_parent_percent_tracks_the_packaging_artifact_to_completion(self):
        """Positive oracle for the 50% -> 100% walk across the packaging step."""
        job_id, _, audio = self.make_packaging_job()
        for artifact_percent, expected_parent in ((0.0, 50.0), (40.0, 70.0), (100.0, 100.0)):
            with self.subTest(artifact_percent=artifact_percent):
                audio["percent"] = artifact_percent
                payload = self.client.get(f"/api/status/{job_id}").get_json()
                self.assertEqual(payload["percent"], expected_parent)

    def test_status_payload_keeps_every_legacy_key_and_adds_only_packaging_fields(self):
        """Additive change: nothing renamed, nothing dropped, nothing else added."""
        job_id, _, _ = self.make_packaging_job()
        payload = self.client.get(f"/api/status/{job_id}").get_json()

        self.assertTrue(LEGACY_PARENT_KEYS.issubset(payload))
        self.assertTrue(PACKAGING_KEYS.issubset(payload))
        self.assertEqual(set(payload), LEGACY_PARENT_KEYS | PACKAGING_KEYS)

        for index, artifact in enumerate(payload["artifacts"]):
            with self.subTest(artifact=index):
                self.assertTrue(LEGACY_ARTIFACT_KEYS.issubset(artifact))
                self.assertTrue(PACKAGING_KEYS.issubset(artifact))
                self.assertEqual(set(artifact), LEGACY_ARTIFACT_KEYS | PACKAGING_KEYS)

    def test_completed_artifact_reports_null_packaging_fields(self):
        job_id, video, _ = self.make_packaging_job()
        payload = self.client.get(f"/api/status/{job_id}").get_json()
        done_artifact = payload["artifacts"][0]
        self.assertEqual(done_artifact["id"], video["id"])
        for key in sorted(PACKAGING_KEYS):
            with self.subTest(field=key):
                self.assertIn(key, done_artifact)
                self.assertIsNone(done_artifact[key])

    def test_non_packaging_job_reports_present_but_null_packaging_fields(self):
        """Null, never absent: a client must not have to tell the two apart."""
        execution_order, display_order = pipeline.plan_artifacts([
            {"type": "video", "format_id": "137"},
        ])
        artifact = display_order[0]
        artifact.update({
            "status": "downloading",
            "phase": "downloading",
            "percent": 12.5,
            "downloaded_bytes": 1000,
            "total_bytes": 8000,
            "attempt": 1,
            "max_attempts": 3,
        })
        job_id = "plaindload"
        app.jobs[job_id] = {
            "id": job_id,
            "status": "downloading",
            "phase": "downloading",
            "title": "clip",
            "artifacts": display_order,
            "execution_order": execution_order,
            "current_artifact_id": artifact["id"],
            "started_at": 1.0,
            "created_at": time.time(),
            "finished_at": None,
            "percent": None,
            "client_request_id": None,
        }

        payload = self.client.get(f"/api/status/{job_id}").get_json()
        for key in sorted(PACKAGING_KEYS):
            with self.subTest(scope="parent", field=key):
                self.assertIn(key, payload)
                self.assertIsNone(payload[key])
            with self.subTest(scope="artifact", field=key):
                self.assertIn(key, payload["artifacts"][0])
                self.assertIsNone(payload["artifacts"][0][key])
        self.assertEqual(payload["percent"], 12.5)

    def test_plan_artifacts_seeds_the_packaging_fields(self):
        """A freshly planned artifact already owns the three keys, set to None."""
        _, display_order = pipeline.plan_artifacts([{"type": "audio", "format_id": None}])
        artifact = display_order[0]
        for key in sorted(PACKAGING_KEYS):
            with self.subTest(field=key):
                self.assertIn(key, artifact)
                self.assertIsNone(artifact[key])


if __name__ == "__main__":
    unittest.main()
