"""Programme separation and the honesty of the official check."""

import unittest

from helpers import FakeSource, MonitorCase

from launchsignal.classifier import official_check, resolve_programme
from launchsignal.models import (
    PROGRAMME_SPEEDRUN,
    PROGRAMME_YC,
    Evidence,
    OfficialState,
    SignalKind,
    Source,
    utcnow,
)
from launchsignal.store import company_key


def social(text: str, source: Source = Source.X) -> Evidence:
    return Evidence(
        source=source, external_id="1", url="https://x.com/f/status/1",
        title="", excerpt=text,
    )


class ProgrammeResolutionTest(unittest.TestCase):
    def test_speedrun_claim_is_not_labelled_yc(self) -> None:
        """A search adapter cannot know the programme, and defaulting to YC
        turned every Speedrun post into an 'EARLY YC SIGNAL'."""
        evidence = social("Founder of Pixel — we joined a16z Speedrun SR004!")
        self.assertEqual(resolve_programme(evidence), PROGRAMME_SPEEDRUN)

    def test_yc_claim_resolves_to_yc(self) -> None:
        self.assertEqual(resolve_programme(social("we got into YC S26!")), PROGRAMME_YC)

    def test_directory_programme_is_trusted(self) -> None:
        evidence = Evidence(
            source=Source.A16Z_SPEEDRUN, external_id="1", url="u", title="Speedy",
            excerpt="", company_name="Speedy", programme=PROGRAMME_SPEEDRUN,
        )
        self.assertEqual(resolve_programme(evidence), PROGRAMME_SPEEDRUN)

    def test_the_two_programmes_get_different_company_keys(self) -> None:
        self.assertNotEqual(
            company_key("Acme", PROGRAMME_YC), company_key("Acme", PROGRAMME_SPEEDRUN)
        )


class ProgrammeRoutingTest(MonitorCase):
    def test_a_speedrun_post_alerts_as_speedrun(self) -> None:
        self.run_scan(FakeSource(Source.X, []))
        self.run_scan(
            FakeSource(Source.X, [social("Founder of Pixel — we joined a16z Speedrun SR004!")])
        )
        alert = self.notifier.sent[-1]
        self.assertEqual(alert.programme, PROGRAMME_SPEEDRUN)
        self.assertEqual(alert.batch, "SR004")

    def test_one_programmes_announcement_cannot_suppress_the_others_claim(self) -> None:
        """a16z posting about "Acme" must not answer a question about YC's Acme."""
        self.run_scan(FakeSource(Source.X, []))
        speedrun_official = FakeSource(
            Source.OFFICIAL_X,
            [Evidence(
                source=Source.OFFICIAL_X, external_id="o", url="https://x.com/a16z/status/1",
                title="", excerpt="Welcome Acme to Speedrun!",
            )],
        )
        speedrun_official.programme = PROGRAMME_SPEEDRUN
        speedrun_official.account_labels = ("@a16z",)
        self.run_scan(
            FakeSource(Source.X, [social("Founder of Acme — we got into YC S26!")]),
            official=[speedrun_official],
        )
        alert = self.notifier.sent[-1]
        self.assertEqual(alert.programme, PROGRAMME_YC)
        self.assertEqual(
            alert.official.state, OfficialState.NOT_CHECKED,
            "no YC official source was read, so YC's silence is unknown",
        )

    def test_the_matching_programmes_announcement_does_suppress(self) -> None:
        self.run_scan(FakeSource(Source.X, []))
        yc_official = FakeSource(
            Source.OFFICIAL_X,
            [Evidence(
                source=Source.OFFICIAL_X, external_id="o",
                url="https://x.com/ycombinator/status/1", title="",
                excerpt="Welcome Acme to YC S26!",
            )],
        )
        yc_official.programme = PROGRAMME_YC
        yc_official.account_labels = ("@ycombinator",)
        self.run_scan(
            FakeSource(Source.X, [social("Founder of Acme — we got into YC S26!")]),
            official=[yc_official],
        )
        self.assertEqual(self.notifier.sent[-1].official.state, OfficialState.SEEN)

    def test_audit_trail_names_real_accounts_not_source_labels(self) -> None:
        self.run_scan(FakeSource(Source.X, []))
        yc_official = FakeSource(
            Source.OFFICIAL_X,
            [Evidence(
                source=Source.OFFICIAL_X, external_id="o",
                url="https://x.com/ycombinator/status/1", title="",
                excerpt="Welcome Someone Else to YC S26!",
            )],
        )
        yc_official.programme = PROGRAMME_YC
        yc_official.account_labels = ("@ycombinator",)
        self.run_scan(
            FakeSource(Source.X, [social("Founder of Acme — we got into YC S26!")]),
            official=[yc_official],
        )
        check = self.notifier.sent[-1].official
        self.assertEqual(check.state, OfficialState.NOT_SEEN)
        self.assertIn("@ycombinator", check.accounts_checked)
        self.assertIn("@ycombinator", check.describe())


class ZeroSnapshotHonestyTest(unittest.TestCase):
    """A search that succeeds but finds nothing proves nothing."""

    def test_zero_snapshots_is_inconclusive_not_a_scoop(self) -> None:
        check = official_check("Acme", [], ("@ycombinator",), utcnow())
        self.assertEqual(check.state, OfficialState.NOT_CHECKED)
        self.assertIn("inconclusive", check.describe())
        self.assertIn("@ycombinator", check.describe())

    def test_unconfigured_and_inconclusive_read_differently(self) -> None:
        unconfigured = official_check("Acme", [], (), utcnow())
        inconclusive = official_check("Acme", [], ("@ycombinator",), utcnow())
        self.assertIn("no official-snapshot source is configured", unconfigured.describe())
        self.assertIn("returned no usable snapshots", inconclusive.describe())

    def test_one_snapshot_is_enough_to_conclude_not_seen(self) -> None:
        official = [Evidence(
            source=Source.OFFICIAL_X, external_id="o", url="u", title="",
            excerpt="Welcome Someone Else to YC S26!",
        )]
        check = official_check("Acme", official, ("@ycombinator",), utcnow())
        self.assertEqual(check.state, OfficialState.NOT_SEEN)

    def test_a_higher_threshold_can_be_required(self) -> None:
        official = [Evidence(
            source=Source.OFFICIAL_X, external_id="o", url="u", title="", excerpt="hi",
        )]
        check = official_check(
            "Acme", official, ("@ycombinator",), utcnow(), min_snapshots=3
        )
        self.assertEqual(check.state, OfficialState.NOT_CHECKED)

    def test_scoop_headline_is_withheld_on_zero_snapshots(self) -> None:
        from launchsignal.models import Alert
        from launchsignal.notify import headline

        alert = Alert(
            alert_key="k", company_key="c", kind=SignalKind.EARLY_FOUNDER_CLAIM,
            company_name="Acme", programme=PROGRAMME_YC, source=Source.X,
            source_url="https://x.com/f/status/1", excerpt="we got into YC S26",
            official=official_check("Acme", [], ("@ycombinator",), utcnow()),
        )
        self.assertNotIn("Announced Before YC", headline(alert))


if __name__ == "__main__":
    unittest.main()
