"""Baseline, deduplication, alert routing and failure recovery."""

import unittest

from helpers import FakeSource, MonitorCase, RecordingNotifier, directory_record, x_post

from launchsignal.models import Evidence, OfficialState, SignalKind, Source
from launchsignal.notify import SlackSendError
from launchsignal.service import Monitor


class BaselineTest(MonitorCase):
    def test_first_scan_is_silent(self) -> None:
        source = FakeSource(Source.YC_DIRECTORY, [directory_record("acme", "Acme")])
        result = self.run_scan(source)
        self.assertEqual(result["new"], 1)
        self.assertEqual(result["alerts"], 0)
        self.assertEqual(self.notifier.sent, [])
        self.assertIn("yc_directory", result["baselined_sources"])

    def test_second_scan_alerts_only_on_new_records(self) -> None:
        first = FakeSource(Source.YC_DIRECTORY, [directory_record("acme", "Acme")])
        self.run_scan(first)
        second = FakeSource(
            Source.YC_DIRECTORY,
            [directory_record("acme", "Acme"), directory_record("beta", "Beta")],
        )
        result = self.run_scan(second)
        self.assertEqual(result["new"], 1)
        self.assertEqual(result["alerts"], 1)
        self.assertEqual(self.notifier.sent[0].company_name, "Beta")

    def test_identical_rescan_sends_nothing(self) -> None:
        records = [directory_record("acme", "Acme")]
        self.run_scan(FakeSource(Source.YC_DIRECTORY, records))
        self.run_scan(FakeSource(Source.YC_DIRECTORY, records + [directory_record("b", "Beta")]))
        before = len(self.notifier.sent)
        result = self.run_scan(
            FakeSource(Source.YC_DIRECTORY, records + [directory_record("b", "Beta")])
        )
        self.assertEqual(result["new"], 0)
        self.assertEqual(result["alerts"], 0)
        self.assertEqual(len(self.notifier.sent), before)

    def test_baseline_is_per_source(self) -> None:
        """A source added later seeds silently without muting the others."""
        self.run_scan(FakeSource(Source.YC_DIRECTORY, [directory_record("acme", "Acme")]))
        result = self.run_scan(
            FakeSource(Source.YC_DIRECTORY, [directory_record("new", "Newco")]),
            FakeSource(Source.A16Z_SPEEDRUN, [
                Evidence(
                    source=Source.A16Z_SPEEDRUN, external_id="sr1", url="u",
                    title="Speedy", excerpt="", company_name="Speedy",
                    programme="a16z Speedrun",
                )
            ]),
        )
        # YC is past baseline so Newco alerts; Speedrun is seeding so it stays silent.
        self.assertEqual(result["alerts"], 1)
        self.assertEqual(self.notifier.sent[0].company_name, "Newco")

    def test_baseline_is_not_completed_after_a_failed_read(self) -> None:
        """A crash mid-baseline must not mark the source seeded.

        It also must not force a second silent baseline that suppresses alerts
        again -- the old global flag did exactly that.
        """
        boom = FakeSource(Source.YC_DIRECTORY, error=RuntimeError("transient 503"))
        result = self.run_scan(boom)
        self.assertFalse(self.store.baseline_complete("yc_directory"))
        self.assertEqual(result["outcome"], "partial (1 source(s) failed)")

        good = FakeSource(Source.YC_DIRECTORY, [directory_record("acme", "Acme")])
        second = self.run_scan(good)
        self.assertIn("yc_directory", second["baselined_sources"])
        self.assertEqual(second["alerts"], 0)

        third = self.run_scan(
            FakeSource(Source.YC_DIRECTORY, [directory_record("beta", "Beta")])
        )
        self.assertEqual(third["alerts"], 1)


class SourceIsolationTest(MonitorCase):
    def test_one_failing_source_does_not_stop_the_others(self) -> None:
        result = self.run_scan(
            FakeSource(Source.YC_DIRECTORY, error=RuntimeError("boom")),
            FakeSource(Source.A16Z_SPEEDRUN, [
                Evidence(
                    source=Source.A16Z_SPEEDRUN, external_id="sr1", url="u",
                    title="Speedy", excerpt="", company_name="Speedy",
                    programme="a16z Speedrun",
                )
            ]),
        )
        outcomes = {s["source"]: s for s in result["sources"]}
        self.assertFalse(outcomes["yc_directory"]["ok"])
        self.assertTrue(outcomes["a16z_speedrun"]["ok"])
        self.assertEqual(outcomes["a16z_speedrun"]["new"], 1)

    def test_source_health_records_the_failure(self) -> None:
        self.run_scan(FakeSource(Source.YC_DIRECTORY, error=RuntimeError("nope")))
        health = {row["source"]: row for row in self.store.source_health()}
        self.assertEqual(health["yc_directory"]["consecutive_fails"], 1)
        self.assertIn("nope", str(health["yc_directory"]["last_error"]))


class EarlySignalRoutingTest(MonitorCase):
    """The task's headline requirement: a founder post before the official post."""

    def _seed(self) -> None:
        self.run_scan(FakeSource(Source.YC_DIRECTORY, []), FakeSource(Source.X, []))

    def test_directory_listing_does_not_suppress_a_later_early_claim(self) -> None:
        """The central regression.

        Keying alerts on the company alone meant a directory confirmation
        consumed the only slot, so the founder claim -- the whole point of the
        product -- silently never fired.
        """
        self._seed()
        confirmed = self.run_scan(
            FakeSource(Source.YC_DIRECTORY, [directory_record("hebbian", "Hebbian Robotics")])
        )
        self.assertEqual(confirmed["alerts"], 1)
        self.assertEqual(self.notifier.sent[-1].kind, SignalKind.CONFIRMED)

        early = self.run_scan(
            FakeSource(Source.X, [
                x_post("9", "We got into YC S26. Founder of Hebbian Robotics.")
            ])
        )
        self.assertEqual(early["alerts"], 1, "the early founder claim must still alert")
        self.assertEqual(self.notifier.sent[-1].kind, SignalKind.EARLY_FOUNDER_CLAIM)
        self.assertEqual(self.notifier.sent[-1].company_name, "Hebbian Robotics")

    def test_early_claim_then_directory_confirmation_both_alert(self) -> None:
        self._seed()
        self.run_scan(
            FakeSource(Source.X, [x_post("1", "Founder of Zephyr — we got into YC S26!")])
        )
        self.assertEqual(self.notifier.sent[-1].kind, SignalKind.EARLY_FOUNDER_CLAIM)
        result = self.run_scan(FakeSource(Source.YC_DIRECTORY, [directory_record("zephyr", "Zephyr")]))
        self.assertEqual(result["alerts"], 1)
        self.assertEqual(self.notifier.sent[-1].kind, SignalKind.CONFIRMED)

    def test_same_kind_never_alerts_twice_for_one_company(self) -> None:
        self._seed()
        self.run_scan(
            FakeSource(Source.X, [x_post("1", "Founder of Zephyr — we got into YC S26!")])
        )
        count = len(self.notifier.sent)
        # A different post about the same company, same kind of news.
        self.run_scan(
            FakeSource(Source.X, [x_post("2", "Founder of Zephyr — we got into YC S26!")])
        )
        self.assertEqual(len(self.notifier.sent), count, "duplicate news must not re-alert")

    def test_one_company_across_two_networks_is_one_alert(self) -> None:
        self._seed()
        self.run_scan(FakeSource(Source.LINKEDIN, []))
        result = self.run_scan(
            FakeSource(Source.X, [x_post("1", "Founder of Orbital — we got into YC S26!")]),
            FakeSource(Source.LINKEDIN, [
                Evidence(
                    source=Source.LINKEDIN, external_id="li1",
                    url="https://www.linkedin.com/posts/jane_activity-1",
                    title="", excerpt="Founder of Orbital — we got into YC S26!",
                )
            ]),
        )
        self.assertEqual(result["alerts"], 1)

    def test_official_announcement_downgrades_the_claim(self) -> None:
        self._seed()
        official = FakeSource(Source.OFFICIAL_X, [
            Evidence(
                source=Source.OFFICIAL_X, external_id="o1",
                url="https://x.com/ycombinator/status/5", title="",
                excerpt="Welcome Zephyr to YC S26!",
            )
        ])
        self.run_scan(
            FakeSource(Source.X, [x_post("1", "Founder of Zephyr — we got into YC S26!")]),
            official=[official],
        )
        alert = self.notifier.sent[-1]
        self.assertEqual(alert.official.state, OfficialState.SEEN)
        self.assertEqual(alert.official.matched_url, "https://x.com/ycombinator/status/5")

    def test_no_official_source_yields_not_checked_not_a_false_scoop(self) -> None:
        """With nothing to compare against, the alert must not claim a scoop."""
        self._seed()
        self.run_scan(
            FakeSource(Source.X, [x_post("1", "Founder of Zephyr — we got into YC S26!")])
        )
        alert = self.notifier.sent[-1]
        self.assertEqual(alert.official.state, OfficialState.NOT_CHECKED)
        self.assertFalse(alert.official.performed)

    def test_unresolvable_claim_goes_to_review_not_to_slack(self) -> None:
        self._seed()
        result = self.run_scan(
            FakeSource(Source.X, [x_post("1", "we got into YC S26 and are hiring!")])
        )
        self.assertEqual(result["alerts"], 0)
        self.assertEqual(result["review"], 1)
        queue = self.store.review_items()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["reason"], "company-unresolved")


class DeliveryDurabilityTest(MonitorCase):
    def test_a_failed_send_is_retried_on_the_next_scan(self) -> None:
        """A Slack outage must delay an alert, never lose it.

        Sending before recording meant the observation was already committed, so
        the item was no longer 'new' and could never alert again.
        """
        self.run_scan(FakeSource(Source.YC_DIRECTORY, []))
        flaky = RecordingNotifier(
            fail_on={"Beta"}, error=SlackSendError("ratelimited", fatal=False)
        )
        monitor = Monitor(self.store, flaky)
        result = monitor.run([FakeSource(Source.YC_DIRECTORY, [
            directory_record("a", "Alpha"), directory_record("b", "Beta"),
            directory_record("c", "Gamma"),
        ])], [])
        self.assertEqual(result["alerts"], 2)
        self.assertEqual([a.company_name for a in flaky.sent], ["Alpha", "Gamma"])
        self.assertEqual(self.store.alert_counts().get("failed"), 1)

        healthy = RecordingNotifier()
        recovered = Monitor(self.store, healthy).run([FakeSource(Source.YC_DIRECTORY, [])], [])
        self.assertEqual(recovered["retried"], 1)
        self.assertEqual([a.company_name for a in healthy.sent], ["Beta"])
        self.assertEqual(self.store.alert_counts().get("sent"), 3)

    def test_a_retried_alert_is_not_sent_twice(self) -> None:
        self.run_scan(FakeSource(Source.YC_DIRECTORY, []))
        self.run_scan(FakeSource(Source.YC_DIRECTORY, [directory_record("a", "Alpha")]))
        healthy = RecordingNotifier()
        result = Monitor(self.store, healthy).run([FakeSource(Source.YC_DIRECTORY, [])], [])
        self.assertEqual(result["retried"], 0)
        self.assertEqual(healthy.sent, [])

    def test_alert_payload_survives_a_restart(self) -> None:
        """The staged row must rebuild into an identical alert."""
        self.run_scan(FakeSource(Source.YC_DIRECTORY, []))
        flaky = RecordingNotifier(fail_on={"Alpha"}, error=SlackSendError("http_500", fatal=False))
        Monitor(self.store, flaky).run(
            [FakeSource(Source.YC_DIRECTORY, [directory_record("a", "Alpha", batch="YC S26")])], []
        )
        healthy = RecordingNotifier()
        Monitor(self.store, healthy).run([FakeSource(Source.YC_DIRECTORY, [])], [])
        rebuilt = healthy.sent[0]
        self.assertEqual(rebuilt.company_name, "Alpha")
        self.assertEqual(rebuilt.batch, "YC S26")
        self.assertEqual(rebuilt.kind, SignalKind.CONFIRMED)
        self.assertEqual(rebuilt.programme, "YC")


class ExtensibilityTest(MonitorCase):
    def test_a_new_network_needs_no_classifier_change(self) -> None:
        """Registering a category is the only step to add a fifth network."""
        from launchsignal.models import SOURCE_CATEGORIES, SourceCategory

        # Reuse an enum member to stand in for a newly added network.
        original = SOURCE_CATEGORIES[Source.LINKEDIN]
        SOURCE_CATEGORIES[Source.LINKEDIN] = SourceCategory.SOCIAL
        try:
            self.run_scan(FakeSource(Source.LINKEDIN, []))
            result = self.run_scan(FakeSource(Source.LINKEDIN, [
                Evidence(
                    source=Source.LINKEDIN, external_id="n1",
                    url="https://www.linkedin.com/posts/x_activity-1",
                    title="", excerpt="Founder of Newnet — we got into YC S26!",
                )
            ]))
            self.assertEqual(result["alerts"], 1)
        finally:
            SOURCE_CATEGORIES[Source.LINKEDIN] = original


if __name__ == "__main__":
    unittest.main()


class FastLaneTest(MonitorCase):
    """The fast lane must not perform a source's silent baseline."""

    def test_fast_lane_waits_for_the_full_scan_to_baseline(self) -> None:
        import os

        from launchsignal.cli import scan_once

        os.environ["TINYFISH_API_KEY"] = "test-key-not-real"
        try:
            result = scan_once(self.store, self.notifier, fast=True)
        finally:
            os.environ.pop("TINYFISH_API_KEY", None)
        self.assertEqual(result["alerts"], 0)
        self.assertEqual(result["observations"], 0)
        self.assertTrue(result["skipped_pending_baseline"])
        self.assertFalse(self.store.baseline_complete("x"))
