"""Adversarial retention proofs using synthetic data and isolated SQLite files."""

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch

# Establish the existing synthetic-only configuration before importing core.
from tests import test_search_failure_reporting as existing_tests

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, update
from sqlmodel import Session, SQLModel, create_engine, select

from core.models import get_session
from core.models.flight import SearchFailureReportRequest
from core.models.search_failure import SearchFailureCleanupStatus, SearchFailureSample
from core.models.user import User
from core.routers import flights
from core.search_failure_retention_policy import (
    CLEANUP_HEADROOM_MS,
    CLEANUP_STATUS_STALE_MS,
    RETENTION_MS,
    SAMPLE_RETENTION_MS,
)
from core.services.search_failure import SearchFailureService
from scripts.cleanup_search_failures import run_cleanup


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
QUERY = "ZZ123 tomorrow"
CANARY = "SYNTHETIC_PRIVATE_SEARCH_CANARY"


@contextmanager
def sqlite_connection(database, **kwargs):
    connection = sqlite3.connect(database, **kwargs)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class SearchFailureFixture:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="sofly-search-retention-")
        self.database = Path(self.directory.name) / "samples.db"
        self.engine = create_engine(
            f"sqlite:///{self.database}",
            connect_args={"check_same_thread": False, "timeout": 3},
            hide_parameters=True,
        )
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.user = User(id="retention-test-owner")
        self.foreign_user = User(id="retention-other-owner")
        self.session.add_all([self.user, self.foreign_user])
        self.session.commit()
        self.base = int(time.time() * 1_000)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()
        self.directory.cleanup()

    def capture(self, *, at=None, **overrides):
        params = dict(
            session=self.session, user_id=self.user.id, query=QUERY,
            source="backend", query_type="flight_number", failure_reason="provider_no_match",
            provider_outcome="results", normalization_applied=False, provider_result_count=2,
            allow_new_capture=True,
        )
        params.update(overrides)
        with patch("core.services.search_failure.time", return_value=(at or self.base) / 1_000):
            sample = SearchFailureService.record(**params)
            self.session.commit()
            self.assertIsNotNone(sample)
            return sample.id

    def payload(self, sample_id, **overrides):
        values = dict(
            query=QUERY, failure_sample_id=sample_id, source="regular_search",
            query_type="flight_number", failure_reason="landed_only", provider_outcome="results",
            provider_result_count=2, filtered_result_count=2,
            search_journey_id="synthetic-journey", search_attempt_number=1,
            app_version="3.8", build_number="119", analytics_environment="testflight",
        )
        values.update(overrides)
        return SearchFailureReportRequest(**values)

    def report(self, sample_id, *, at=None, user=None, **overrides):
        with patch("core.services.search_failure.time", return_value=(at or self.base) / 1_000):
            return flights.report_app_search_failure(
                self.payload(sample_id, **overrides), self.session, user or self.user
            )

    def stored(self, sample_id):
        return self.session.get(SearchFailureSample, sample_id, populate_existing=True)

    def rows(self):
        return self.session.exec(select(SearchFailureSample)).all()


class SearchFailureRetentionTests(SearchFailureFixture, unittest.TestCase):
    def test_capture_has_server_timestamp_and_early_expiry_within_seven_day_cap(self):
        sample = self.stored(self.capture())
        self.assertEqual(sample.created_at_ms, self.base)
        self.assertEqual(sample.last_reported_at_ms, self.base)
        self.assertEqual(sample.expires_at_ms, self.base + SAMPLE_RETENTION_MS)
        self.assertEqual(RETENTION_MS - SAMPLE_RETENTION_MS, CLEANUP_HEADROOM_MS)
        self.assertEqual(CLEANUP_HEADROOM_MS, 10 * 60 * 1_000)

    def test_repeated_near_expiry_retries_never_restart_capture_clock_or_ciphertext(self):
        sample_id = self.capture()
        original = self.stored(sample_id).model_dump()
        for offset in [1, SAMPLE_RETENTION_MS // 2, SAMPLE_RETENTION_MS - 1]:
            response = self.report(sample_id, at=self.base + offset)
            self.assertTrue(response["sample_recorded"])
            current = self.stored(sample_id)
            for field in ("id", "user_hash", "created_at_ms", "expires_at_ms", "query_ciphertext", "query_digest"):
                self.assertEqual(getattr(current, field), original[field])
        self.assertEqual(len(self.rows()), 1)

    def test_at_and_after_expiry_reports_never_encrypt_or_recreate_deleted_id(self):
        sample_id = self.capture()
        expiry = self.base + SAMPLE_RETENTION_MS
        with patch.object(SearchFailureService, "_encrypt_query", side_effect=AssertionError("must not encrypt")), \
                patch.object(SearchFailureService, "query_digest", side_effect=AssertionError("must not digest")):
            for at in [expiry, expiry + 1, expiry + RETENTION_MS]:
                self.assertFalse(self.report(sample_id, at=at)["sample_recorded"])
            SearchFailureService.purge_expired(self.session, now_ms=expiry)
            self.session.commit()
            for at in [expiry + 1, expiry + RETENTION_MS]:
                response = self.report(sample_id, at=at)
                self.assertEqual(response["detail"], "success")
                self.assertFalse(response["sample_recorded"])
        self.assertEqual(self.rows(), [])

    def test_missing_empty_and_unknown_ids_are_successful_noops_even_with_backend_source(self):
        with patch.object(SearchFailureService, "_encrypt_query", side_effect=AssertionError("must not encrypt")), \
                patch.object(SearchFailureService, "query_digest", side_effect=AssertionError("must not digest")):
            for sample_id in [None, "", "missing-synthetic-id"]:
                for at in [self.base, self.base + RETENTION_MS * 3]:
                    response = self.report(sample_id, at=at, source="backend")
                    self.assertEqual(response["detail"], "success")
                    self.assertFalse(response["sample_recorded"])
        self.assertEqual(self.rows(), [])

    def test_http_legacy_no_id_and_spoofed_internal_flag_remain_success_without_capture(self):
        app = FastAPI()
        app.include_router(flights.router)
        app.dependency_overrides[get_session] = lambda: self.session
        app.dependency_overrides[flights.get_current_user] = lambda: self.user
        body = self.payload(None, source="backend").model_dump()
        body["allow_new_capture"] = True
        with TestClient(app) as client:
            response = client.post("/search/failures", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["failure_sample_id"])
        self.assertFalse(response.json()["sample_recorded"])
        self.assertEqual(self.rows(), [])

    def test_internal_default_is_fail_closed_not_inferred_from_source_name(self):
        sample = SearchFailureService.record(
            session=self.session, user_id=self.user.id, query=QUERY, source="backend",
            query_type="flight_number", failure_reason="provider_no_match", provider_outcome="results",
            normalization_applied=False, provider_result_count=0,
        )
        self.assertIsNone(sample)
        self.session.commit()
        self.assertEqual(self.rows(), [])

    def test_foreign_id_never_creates_a_local_copy_or_changes_original_sample(self):
        sample_id = self.capture()
        original = self.stored(sample_id).model_dump()
        response = self.report(sample_id, user=self.foreign_user, filtered_result_count=999)
        self.assertEqual(response["detail"], "success")
        self.assertFalse(response["sample_recorded"])
        self.assertEqual(self.stored(sample_id).model_dump(), original)
        self.assertEqual(len(self.rows()), 1)

    def test_query_or_journey_attempt_conflicts_do_not_rebind_capture(self):
        sample_id = self.capture()
        self.report(sample_id)
        original = self.stored(sample_id).model_dump()
        for overrides in [dict(query="different synthetic query"), dict(search_journey_id="different-journey"),
                          dict(search_attempt_number=2)]:
            response = self.report(sample_id, at=self.base + 100, **overrides)
            self.assertFalse(response["sample_recorded"])
            self.assertEqual(self.stored(sample_id).model_dump(), original)

    def test_known_and_unknown_capture_context_are_frozen_with_one_time_correlation(self):
        known_id = self.capture(app_version="3.7", build_number="118", analytics_environment="production",
                                provider_latency_ms=42)
        self.report(known_id)
        known = self.stored(known_id)
        self.assertEqual((known.app_version, known.build_number, known.analytics_environment),
                         ("3.7", "118", "production"))
        self.assertEqual(known.provider_latency_ms, 42)

        missing_id = self.capture(query_type="unknown", provider_outcome="unknown")
        self.report(missing_id, provider_latency_ms=12)
        self.report(missing_id, at=self.base + 100, app_version="9.9", build_number="999",
                    analytics_environment="production", provider_latency_ms=999)
        first = self.stored(missing_id)
        self.assertEqual((first.app_version, first.build_number, first.analytics_environment),
                         (None, None, "unknown"))
        self.assertEqual((first.query_type, first.provider_outcome, first.provider_latency_ms),
                         ("unknown", "unknown", None))
        self.assertEqual((first.search_journey_id, first.search_attempt_number), ("synthetic-journey", 1))

    def test_old_retry_extended_expiry_is_clamped_without_changing_capture(self):
        sample_id = self.capture()
        sample = self.stored(sample_id)
        sample.expires_at_ms = self.base + RETENTION_MS * 2
        self.session.add(sample)
        self.session.commit()
        self.report(sample_id, at=self.base + 1)
        self.assertEqual(self.stored(sample_id).expires_at_ms, self.base + SAMPLE_RETENTION_MS)
        self.assertEqual(self.stored(sample_id).created_at_ms, self.base)

    def test_recent_and_decryption_exclude_old_extended_or_expired_rows_without_cleanup(self):
        sample_id = self.capture()
        sample = self.stored(sample_id)
        sample.expires_at_ms = self.base + RETENTION_MS * 2
        self.session.add(sample)
        self.session.commit()
        cutoff = self.base + SAMPLE_RETENTION_MS
        self.assertEqual(SearchFailureService.recent(self.session, since_ms=0, limit=100, now_ms=cutoff), [])
        with patch.object(SearchFailureService, "_fernet", side_effect=AssertionError("must not decrypt")):
            self.assertIsNone(SearchFailureService.decrypt_query(
                sample.query_ciphertext, created_at_ms=sample.created_at_ms,
                expires_at_ms=sample.expires_at_ms, now_ms=cutoff,
            ))
        self.assertEqual(len(self.rows()), 1, "Read-time expiry does not depend on deletion traffic")

    def test_decryption_requires_capture_context_and_rejects_exact_expiry(self):
        sample = self.stored(self.capture())
        with self.assertRaises(TypeError):
            SearchFailureService.decrypt_query(sample.query_ciphertext)
        self.assertEqual(SearchFailureService.decrypt_query(
            sample.query_ciphertext, created_at_ms=sample.created_at_ms,
            expires_at_ms=sample.expires_at_ms, now_ms=sample.expires_at_ms - 1,
        ), QUERY)
        self.assertIsNone(SearchFailureService.decrypt_query(
            sample.query_ciphertext, created_at_ms=sample.created_at_ms,
            expires_at_ms=sample.expires_at_ms, now_ms=sample.expires_at_ms,
        ))

    def test_expiry_crossed_during_decryption_discards_plaintext_before_return(self):
        sample = self.stored(self.capture())
        current = [sample.expires_at_ms - 1]
        expiry = sample.expires_at_ms

        class SyntheticFernet:
            def decrypt(self, ciphertext):
                current[0] = expiry
                return QUERY.encode()

        with patch("core.services.search_failure.time", side_effect=lambda: current[0] / 1_000), \
                patch.object(SearchFailureService, "_fernet", return_value=SyntheticFernet()):
            self.assertIsNone(SearchFailureService.decrypt_query(
                sample.query_ciphertext, created_at_ms=sample.created_at_ms,
                expires_at_ms=sample.expires_at_ms,
            ))

    def test_cleanup_after_commit_does_not_turn_successful_acknowledgement_into_500(self):
        sample_id = self.capture()
        expiry = self.stored(sample_id).expires_at_ms
        original_commit = self.session.commit

        def commit_then_purge():
            original_commit()
            with Session(self.engine) as independent:
                SearchFailureService.purge_expired(independent, now_ms=expiry)
                independent.commit()

        with patch.object(self.session, "commit", side_effect=commit_then_purge):
            response = self.report(sample_id, at=expiry - 1)
        self.assertEqual(response["detail"], "success")
        self.assertEqual(response["failure_sample_id"], sample_id)
        self.assertTrue(response["sample_recorded"])
        self.assertEqual(self.rows(), [])

    def test_exact_expiry_between_list_and_decrypt_returns_no_plaintext_metadata_or_count(self):
        sample_id = self.capture()
        expiry = self.stored(sample_id).expires_at_ms
        current = [expiry - 1]
        real_decrypt = SearchFailureService.decrypt_query

        def cross_deadline(*args, **kwargs):
            current[0] = expiry
            return real_decrypt(*args, **kwargs)

        with patch("core.routers.flights.time.time", side_effect=lambda: current[0] / 1_000), \
                patch("core.services.search_failure.time", side_effect=lambda: current[0] / 1_000), \
                patch.object(SearchFailureService, "decrypt_query", side_effect=cross_deadline) as decrypt, \
                patch.object(SearchFailureService, "_fernet", side_effect=AssertionError("expired decryption")):
            response = flights.get_search_failure_report(
                days=7, limit=100, include_samples=True, analytics_environment=None, session=self.session,
            )
        self.assertEqual(decrypt.call_count, 1)
        self.assertEqual(response["sample_count"], 0)
        self.assertEqual(response["recent_samples"], [])
        self.assertEqual(response["groups"], [])

    def test_future_capture_and_future_last_success_are_not_treated_as_live_or_healthy(self):
        self.capture(at=self.base + 1_000)
        self.assertEqual(SearchFailureService.recent(self.session, since_ms=0, limit=100, now_ms=self.base), [])
        self.session.add(SearchFailureCleanupStatus(id=1, outcome="success", last_success_at_ms=self.base + 1))
        self.session.commit()
        self.assertTrue(SearchFailureService.cleanup_status(self.session, now_ms=self.base)["stale"])

    def test_diagnostic_errors_and_rollback_errors_never_log_raw_query_or_traceback(self):
        with patch.object(SearchFailureService, "record", side_effect=RuntimeError(CANARY)), \
                patch.object(self.session, "rollback", side_effect=RuntimeError(CANARY)), \
                patch.object(self.session, "invalidate", side_effect=RuntimeError(CANARY)), \
                self.assertLogs("core.routers.flights", level="ERROR") as logged:
            with self.assertRaises(HTTPException) as caught:
                flights.report_app_search_failure(self.payload(None), self.session, self.user)
        self.assertEqual(caught.exception.status_code, 500)
        self.assertNotIn(CANARY, " ".join(logged.output))
        self.assertTrue(all(not record.exc_info and not record.stack_info for record in logged.records))

    def parallel_reports(self, payloads, *, at):
        self.session.rollback()
        barrier = Barrier(len(payloads))

        def send(pair):
            user_id, payload = pair
            with Session(self.engine) as independent:
                user = independent.get(User, user_id)
                barrier.wait(timeout=5)
                return flights.report_app_search_failure(payload, independent, user)

        with patch("core.services.search_failure.time", return_value=at / 1_000), \
                ThreadPoolExecutor(max_workers=len(payloads)) as pool:
            return list(pool.map(send, payloads))

    def test_concurrent_replays_keep_maximum_counts_original_expiry_and_single_row(self):
        sample_id = self.capture()
        payloads = [(self.user.id, self.payload(sample_id, filtered_result_count=index)) for index in range(10)]
        responses = self.parallel_reports(payloads, at=self.base + 100)
        self.assertTrue(all(response["sample_recorded"] for response in responses))
        sample = self.stored(sample_id)
        self.assertEqual(sample.filtered_result_count, 9)
        self.assertEqual(sample.created_at_ms, self.base)
        self.assertEqual(sample.expires_at_ms, self.base + SAMPLE_RETENTION_MS)
        self.assertEqual(len(self.rows()), 1)

    def test_concurrent_foreign_and_owned_replays_cannot_mix_accounts(self):
        sample_id = self.capture()
        responses = self.parallel_reports([
            (self.user.id, self.payload(sample_id, filtered_result_count=3)),
            (self.foreign_user.id, self.payload(sample_id, filtered_result_count=999)),
        ], at=self.base + 100)
        self.assertEqual([response["sample_recorded"] for response in responses], [True, False])
        self.assertEqual(self.stored(sample_id).filtered_result_count, 3)
        self.assertEqual(len(self.rows()), 1)

    def test_concurrent_no_id_replays_are_acknowledged_without_query_capture(self):
        responses = self.parallel_reports(
            [(self.user.id, self.payload(None)) for _ in range(6)], at=self.base,
        )
        self.assertTrue(all(response["detail"] == "success" and not response["sample_recorded"]
                            for response in responses))
        self.assertEqual(self.rows(), [])

    def test_expiry_clock_is_read_after_waiting_for_independent_writer(self):
        sample_id = self.capture()
        owner_id = self.user.id
        self.session.rollback()
        attempted_write = Event()

        def before_execute(connection, cursor, statement, parameters, context, executemany):
            if statement.lower().startswith("update searchfailuresample"):
                attempted_write.set()

        with Session(self.engine) as blocker:
            blocker.exec(update(SearchFailureSample).where(SearchFailureSample.id == sample_id)
                         .values(last_reported_at_ms=SearchFailureSample.last_reported_at_ms))
            event.listen(self.engine, "before_cursor_execute", before_execute)
            try:
                def send():
                    with Session(self.engine) as independent:
                        return flights.report_app_search_failure(
                            self.payload(sample_id), independent, independent.get(User, owner_id)
                        )

                with patch("core.services.search_failure.time", return_value=(self.base + SAMPLE_RETENTION_MS) / 1_000) as clock, \
                        ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(send)
                    self.assertTrue(attempted_write.wait(timeout=3))
                    self.assertEqual(clock.call_count, 0, "Expiry must not be evaluated before acquiring the lock")
                    blocker.commit()
                    response = pending.result(timeout=5)
                self.assertFalse(response["sample_recorded"])
            finally:
                event.remove(self.engine, "before_cursor_execute", before_execute)

    def test_concurrent_purge_and_expired_replay_never_resurrect_sample(self):
        sample_id = self.capture()
        owner_id = self.user.id
        expiry = self.base + SAMPLE_RETENTION_MS
        self.session.rollback()
        barrier = Barrier(2)

        def replay():
            with Session(self.engine) as independent:
                user = independent.get(User, owner_id)
                barrier.wait(timeout=5)
                return flights.report_app_search_failure(self.payload(sample_id), independent, user)

        def purge():
            with Session(self.engine) as independent:
                barrier.wait(timeout=5)
                count = SearchFailureService.purge_expired(independent, now_ms=expiry)
                independent.commit()
                return count

        with patch("core.services.search_failure.time", return_value=expiry / 1_000), \
                ThreadPoolExecutor(max_workers=2) as pool:
            replay_future, purge_future = pool.submit(replay), pool.submit(purge)
            self.assertFalse(replay_future.result(timeout=5)["sample_recorded"])
            self.assertEqual(purge_future.result(timeout=5), 1)
        self.assertEqual(self.rows(), [])


class SearchFailureCleanupTests(SearchFailureFixture, unittest.TestCase):
    def test_idle_cleanup_deletes_only_expired_samples_and_clamps_live_legacy_expiry(self):
        old_id = self.capture(at=self.base - RETENTION_MS)
        live_id = self.capture()
        live = self.stored(live_id)
        live.expires_at_ms = self.base + RETENTION_MS * 2
        self.session.add(live)
        self.session.commit()
        with sqlite_connection(self.database) as connection:
            users_before = connection.execute('SELECT * FROM "user" ORDER BY id').fetchall()
        result = run_cleanup(self.database, apply=True, now_ms=self.base)
        self.assertEqual(result["outcome"], "success")
        self.assertEqual((result["deleted_count"], result["clamped_count"]), (1, 1))
        self.assertIsNone(self.stored(old_id))
        self.assertEqual(self.stored(live_id).expires_at_ms, self.base + SAMPLE_RETENTION_MS)
        with sqlite_connection(self.database) as connection:
            self.assertEqual(connection.execute('SELECT * FROM "user" ORDER BY id').fetchall(), users_before)
        status = SearchFailureService.cleanup_status(self.session, now_ms=self.base)
        self.assertEqual(status["last_success_at_ms"], self.base)
        self.assertFalse(status["stale"])
        self.assertTrue(SearchFailureService.cleanup_status(
            self.session, now_ms=self.base + CLEANUP_STATUS_STALE_MS + 1
        )["stale"])

    def test_default_and_status_are_read_only_and_do_not_advance_success(self):
        self.capture(at=self.base - RETENTION_MS)
        before = self.database.read_bytes()
        for status_only in [False, True]:
            result = run_cleanup(self.database, status_only=status_only, now_ms=self.base)
            self.assertEqual(result["expired_remaining"], 1)
            self.assertEqual(result["deleted_count"], 0)
            self.assertIsNone(result["last_success_at_ms"])
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(len(self.rows()), 1)

    def test_bounded_backlog_requires_another_independent_run(self):
        for _ in range(5):
            self.capture(at=self.base - RETENTION_MS)
        first = run_cleanup(self.database, apply=True, batch_size=2, max_batches=1, now_ms=self.base)
        self.assertEqual(first["outcome"], "bounded_backlog")
        self.assertEqual(first["deleted_count"], 2)
        self.assertEqual(first["expired_remaining"], 3)
        self.assertIsNone(first["last_success_at_ms"])
        second = run_cleanup(self.database, apply=True, batch_size=2, max_batches=3, now_ms=self.base)
        self.assertEqual(second["outcome"], "success")
        self.assertEqual(second["deleted_count"], 3)
        self.assertEqual(self.rows(), [])

    def test_missing_or_wrong_database_does_not_create_or_change_files(self):
        missing = Path(self.directory.name) / "missing.db"
        self.assertEqual(run_cleanup(missing, apply=True)["outcome"], "database_unavailable")
        self.assertFalse(missing.exists())
        wrong = Path(self.directory.name) / "different.db"
        with sqlite_connection(wrong) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('synthetic preserved value')")
        before = wrong.read_bytes()
        self.assertEqual(run_cleanup(wrong, apply=True)["outcome"], "search_sample_schema_missing")
        self.assertEqual(wrong.read_bytes(), before)

    def test_bounded_lock_failure_does_not_claim_success_or_leak_error(self):
        self.capture(at=self.base - RETENTION_MS)
        self.session.rollback()
        with sqlite_connection(self.database, isolation_level=None) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            result = run_cleanup(self.database, apply=True, deadline_seconds=0.05, now_ms=self.base)
            self.assertLess(time.monotonic() - started, 1)
            self.assertNotEqual(result["outcome"], "success")
            self.assertIsNone(result["last_success_at_ms"])
            blocker.rollback()
        self.assertNotIn("Traceback", json.dumps(result))
        self.assertEqual(len(self.rows()), 1)

    def test_later_failure_preserves_last_success_fact_and_canary_never_leaves_result(self):
        good = run_cleanup(self.database, apply=True, now_ms=self.base)
        self.assertEqual(good["outcome"], "success")
        with patch("scripts.cleanup_search_failures._aggregate", side_effect=RuntimeError(CANARY)):
            failed = run_cleanup(self.database, apply=True, now_ms=self.base + 1)
        self.assertEqual(failed["outcome"], "cleanup_failed")
        self.assertEqual(failed["last_success_at_ms"], self.base)
        self.assertNotIn(CANARY, json.dumps(failed))
        self.assertEqual(SearchFailureService.cleanup_status(self.session, now_ms=self.base + 1)["last_success_at_ms"], self.base)

    def test_clean_cli_runs_without_app_environment_or_creating_cwd_database(self):
        cwd = Path(self.directory.name) / "unrelated-cwd"
        cwd.mkdir()
        command = [sys.executable, str(REPOSITORY_ROOT / "scripts/cleanup_search_failures.py"),
                   "--database", str(self.database), "--status"]
        completed = subprocess.run(command, cwd=cwd, env={"PYTHONDONTWRITEBYTECODE": "1"},
                                   capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(json.loads(completed.stdout)["outcome"], "status")
        self.assertFalse((cwd / "database.db").exists())
        self.assertEqual(list(cwd.iterdir()), [])

    def test_other_backup_files_are_not_selected_or_modified(self):
        self.capture(at=self.base - RETENTION_MS)
        backup = Path(self.directory.name) / "historical-backup.db"
        with sqlite_connection(self.database) as source, sqlite_connection(backup) as destination:
            source.backup(destination)
        before = hashlib.sha256(backup.read_bytes()).digest()
        run_cleanup(self.database, apply=True, now_ms=self.base)
        self.assertEqual(hashlib.sha256(backup.read_bytes()).digest(), before)
        with sqlite_connection(backup) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM searchfailuresample").fetchone()[0], 1)

    def test_secure_delete_and_wal_checkpoint_remove_expired_ciphertext_from_active_files(self):
        self.session.rollback()
        with sqlite_connection(self.database) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        sample_id = self.capture(at=self.base - RETENTION_MS)
        ciphertext = self.stored(sample_id).query_ciphertext.encode()
        self.session.rollback()
        result = run_cleanup(self.database, apply=True, now_ms=self.base)
        self.assertEqual(result["outcome"], "success")
        for path in [self.database, Path(str(self.database) + "-wal")]:
            if path.exists():
                self.assertNotIn(ciphertext, path.read_bytes())

    def test_wal_reader_blocked_checkpoint_is_not_reported_as_success(self):
        self.session.rollback()
        with sqlite_connection(self.database) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
        self.capture(at=self.base - RETENTION_MS)
        self.session.rollback()
        with sqlite_connection(self.database, isolation_level=None) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM searchfailuresample").fetchone()
            result = run_cleanup(self.database, apply=True, now_ms=self.base)
            self.assertEqual(result["outcome"], "checkpoint_busy")
            self.assertIsNone(result["last_success_at_ms"])
            reader.rollback()
        retried = run_cleanup(self.database, apply=True, now_ms=self.base + 1)
        self.assertEqual(retried["outcome"], "success")

    def test_unit_and_startup_gate_are_independent_bounded_and_clean_environment(self):
        units = REPOSITORY_ROOT / "ops/systemd"
        service = (units / "sofly-search-failure-cleanup.service").read_text()
        timer = (units / "sofly-search-failure-cleanup.timer").read_text()
        startup = (units / "flight-tracker.service.d/search-failure-retention.conf").read_text()
        for source in [service, startup]:
            self.assertIn("/usr/bin/env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1", source)
            self.assertIn("--database /home/ubuntu/backend-flight-tracker/database.db --apply", source)
            self.assertIn("--max-batches 40 --deadline-seconds 20", source)
        self.assertIn("TimeoutStartSec=30s", service)
        self.assertIn("OnCalendar=*-*-* *:*:00 UTC", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("ExecStartPre=", startup)


if __name__ == "__main__":
    unittest.main()
