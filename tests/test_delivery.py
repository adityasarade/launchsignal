"""Delivery safety: exactly-once claiming, dead-lettering, locking."""

import json
import threading
import unittest

from helpers import FakeSource, MonitorCase, RecordingNotifier, directory_record

from launchsignal.models import (
    Alert,
    OfficialCheck,
    OfficialState,
    SignalKind,
    Source,
)
from launchsignal.notify import SlackSendError
from launchsignal.service import Monitor


def make_alert(name: str = "Acme", kind: SignalKind = SignalKind.CONFIRMED) -> Alert:
    return Alert(
        alert_key=f"key-{name}-{kind.value}",
        company_key=f"ck-{name}",
        kind=kind,
        company_name=name,
        programme="YC",
        source=Source.YC_DIRECTORY,
        source_url="https://www.ycombinator.com/companies/acme",
        excerpt="",
        official=OfficialCheck(state=OfficialState.NOT_CHECKED),
    )


class AtomicClaimTest(MonitorCase):
    def test_only_one_caller_can_claim_an_alert(self) -> None:
        alert = make_alert()
        self.assertTrue(self.store.claim_alert(alert))
        self.assertFalse(self.store.claim_alert(alert), "a second claim must lose")

    def test_a_failed_alert_cannot_be_reclaimed_by_ingestion(self) -> None:
        """The race that let two sources double-send.

        Checking the state and then sending is not atomic: both callers see a
        'failed' row and both call Slack. Only the dedicated lease may retry.
        """
        alert = make_alert()
        self.store.claim_alert(alert)
        self.store.mark_alert_failed(alert.alert_key, "ratelimited")
        self.assertFalse(self.store.claim_alert(alert))

    def test_pending_lease_is_exclusive(self) -> None:
        alert = make_alert()
        self.store.claim_alert(alert)
        self.store.mark_alert_failed(alert.alert_key, "http_500")
        self.assertTrue(self.store.claim_pending(alert.alert_key))
        self.assertFalse(
            self.store.claim_pending(alert.alert_key), "a second lease must lose"
        )

    def test_two_sources_one_company_send_exactly_once(self) -> None:
        self.run_scan(FakeSource(Source.YC_DIRECTORY, []), FakeSource(Source.A16Z_SPEEDRUN, []))
        record = directory_record("acme", "Acme")
        result = self.run_scan(
            FakeSource(Source.YC_DIRECTORY, [record]),
            FakeSource(Source.A16Z_SPEEDRUN, [record]),
        )
        self.assertEqual(result["alerts"], 1)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_concurrent_claims_yield_exactly_one_winner(self) -> None:
        """Hammer the claim from many threads; exactly one must win."""
        from launchsignal.store import Store

        alert = make_alert("Concurrent")
        wins: list[bool] = []
        lock = threading.Lock()
        path = f"{self._dir.name}/test.sqlite3"

        def attempt() -> None:
            store = Store(path)
            try:
                won = store.claim_alert(alert)
            finally:
                store.close()
            with lock:
                wins.append(won)

        threads = [threading.Thread(target=attempt) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(wins), 1, f"exactly one claim must win, got {sum(wins)}")


class DeadLetterTest(MonitorCase):
    def test_a_fatal_slack_error_is_not_retried(self) -> None:
        """channel_not_found cannot be fixed by trying again."""
        self.run_scan(FakeSource(Source.YC_DIRECTORY, []))
        fatal = RecordingNotifier(
            fail_on={"Acme"}, error=SlackSendError("channel_not_found", fatal=True)
        )
        Monitor(self.store, fatal).run(
            [FakeSource(Source.YC_DIRECTORY, [directory_record("acme", "Acme")])], []
        )
        self.assertEqual(self.store.alert_counts().get("dead"), 1)
        self.assertEqual(self.store.pending_alerts(), [])

        healthy = RecordingNotifier()
        result = Monitor(self.store, healthy).run([FakeSource(Source.YC_DIRECTORY, [])], [])
        self.assertEqual(result["retried"], 0)
        self.assertEqual(healthy.sent, [])

    def test_a_transient_error_is_retried(self) -> None:
        self.run_scan(FakeSource(Source.YC_DIRECTORY, []))
        flaky = RecordingNotifier(
            fail_on={"Acme"}, error=SlackSendError("ratelimited", fatal=False)
        )
        Monitor(self.store, flaky).run(
            [FakeSource(Source.YC_DIRECTORY, [directory_record("acme", "Acme")])], []
        )
        self.assertEqual(self.store.alert_counts().get("failed"), 1)
        healthy = RecordingNotifier()
        result = Monitor(self.store, healthy).run([FakeSource(Source.YC_DIRECTORY, [])], [])
        self.assertEqual(result["retried"], 1)

    def test_undelivered_alerts_are_visible_not_silently_dropped(self) -> None:
        alert = make_alert("Doomed")
        self.store.claim_alert(alert)
        self.store.mark_alert_failed(alert.alert_key, "channel_not_found", terminal=True)
        undelivered = self.store.undelivered_alerts()
        self.assertEqual(len(undelivered), 1)
        self.assertEqual(undelivered[0]["company_name"], "Doomed")
        self.assertEqual(undelivered[0]["last_error"], "channel_not_found")

    def test_health_report_surfaces_undelivered_alerts(self) -> None:
        from launchsignal.service import health_report

        alert = make_alert("Doomed")
        self.store.claim_alert(alert)
        self.store.mark_alert_failed(alert.alert_key, "invalid_auth", terminal=True)
        report = health_report(self.store, self.notifier)
        self.assertEqual(len(report["undelivered_alerts"]), 1)

    def test_an_alert_stranded_in_sending_is_recovered(self) -> None:
        """A process killed mid-send leaves 'sending'; nothing else would retry it."""
        alert = make_alert("Stranded")
        self.store.claim_alert(alert)
        self.assertEqual(self.store.alert_state(alert.alert_key), "sending")
        self.assertEqual(
            [row["alert_key"] for row in self.store.pending_alerts()], [alert.alert_key]
        )


class ScanLockTest(MonitorCase):
    def test_a_lock_is_exclusive_then_released(self) -> None:
        holder = self.store.acquire_lock("scan")
        self.assertIsNotNone(holder)
        self.assertIsNone(self.store.acquire_lock("scan"), "second holder must fail")
        self.store.release_lock("scan", holder)
        self.assertIsNotNone(self.store.acquire_lock("scan"))

    def test_an_expired_lock_is_reclaimed(self) -> None:
        """A killed process must not wedge the monitor forever."""
        self.store.acquire_lock("scan", ttl_seconds=-1)
        self.assertIsNotNone(self.store.acquire_lock("scan"))

    def test_releasing_with_the_wrong_holder_is_a_no_op(self) -> None:
        self.store.acquire_lock("scan")
        self.store.release_lock("scan", "not-the-holder")
        self.assertIsNone(self.store.acquire_lock("scan"))

    def test_pond_run_scan_skips_when_a_scan_holds_the_lock(self) -> None:
        import os

        from launchsignal import pond

        previous = os.environ.get("LAUNCHSIGNAL_DB_PATH")
        os.environ["LAUNCHSIGNAL_DB_PATH"] = f"{self._dir.name}/test.sqlite3"
        holder = self.store.acquire_lock(pond.SCAN_LOCK)
        try:
            result = pond.run_action("run_scan", {})
            self.assertEqual(result["outcome"], "skipped")
        finally:
            self.store.release_lock(pond.SCAN_LOCK, holder)
            if previous is None:
                os.environ.pop("LAUNCHSIGNAL_DB_PATH", None)
            else:
                os.environ["LAUNCHSIGNAL_DB_PATH"] = previous


class SchemaMigrationTest(MonitorCase):
    def test_a_v2_database_gains_the_enriched_column(self) -> None:
        """An existing install must upgrade in place, not crash."""
        from launchsignal.store import Store

        path = f"{self._dir.name}/old.sqlite3"
        import sqlite3

        legacy = sqlite3.connect(path)
        legacy.executescript(
            """CREATE TABLE observations (
                 source TEXT NOT NULL, external_id TEXT NOT NULL,
                 company_key TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
                 excerpt TEXT NOT NULL, observed_at TEXT NOT NULL,
                 metadata_json TEXT NOT NULL, PRIMARY KEY(source, external_id));
               INSERT INTO observations VALUES
                 ('yc_directory','acme','ck','u','t','e','2026-01-01','{}');"""
        )
        legacy.commit()
        legacy.close()

        store = Store(path)
        try:
            self.assertFalse(store.is_enriched("yc_directory", "acme"))
            store.mark_enriched("yc_directory", "acme")
            self.assertTrue(store.is_enriched("yc_directory", "acme"))
            self.assertEqual(store.observation_count(), 1)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
