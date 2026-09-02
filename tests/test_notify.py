"""The Slack card. Layout follows the reference alert in the task brief."""

import json
import unittest

from launchsignal.models import (
    Alert,
    OfficialCheck,
    OfficialState,
    SignalKind,
    Source,
    utcnow,
)
from launchsignal.notify import build_blocks, headline, status_line


def alert(**overrides) -> Alert:
    base = dict(
        alert_key="k",
        company_key="ck",
        kind=SignalKind.EARLY_FOUNDER_CLAIM,
        company_name="Acme AI",
        programme="YC",
        source=Source.X,
        source_url="https://x.com/example/status/1234567890",
        excerpt="We got into YC S26! After spending the last year building Acme AI, "
        "we're moving to SF and going all in.",
        official=OfficialCheck(
            state=OfficialState.NOT_SEEN,
            accounts_checked=("official_x",),
            snapshots_seen=3,
            checked_at=utcnow(),
        ),
        batch="YC S26",
        founder="Jane Doe",
        founder_handle="@janedoe",
    )
    base.update(overrides)
    return Alert(**base)


def rendered(blocks) -> str:
    return json.dumps(blocks)


class ReferenceCardTest(unittest.TestCase):
    def test_card_carries_every_field_from_the_brief(self) -> None:
        text = rendered(build_blocks(alert()))
        for expected in (
            "Company",
            "Acme AI",
            "*Founder:*",
            "Jane Doe (@janedoe)",
            "*Batch:*",
            "YC S26",
            "Source",
            "X (Twitter)",
            "Status",
            "Original post",
            "https://x.com/example/status/1234567890",
            "Detected",
        ):
            self.assertIn(expected, text, f"missing {expected!r} from the alert card")

    def test_source_is_labelled_for_humans(self) -> None:
        self.assertIn("X (Twitter)", rendered(build_blocks(alert())))
        self.assertNotIn('"x"', rendered(build_blocks(alert())))

    def test_confirmed_card_calls_the_excerpt_a_description(self) -> None:
        text = rendered(build_blocks(alert(
            kind=SignalKind.CONFIRMED, source=Source.YC_DIRECTORY,
            official=OfficialCheck(state=OfficialState.NOT_CHECKED),
        )))
        self.assertIn("Description", text)
        self.assertNotIn("Original post", text)

    def test_optional_fields_are_omitted_when_unknown(self) -> None:
        """Assert on the field markers: "Founder" also appears in the headline."""
        text = rendered(build_blocks(alert(batch=None, founder=None, founder_handle=None)))
        self.assertNotIn("*Batch:*", text)
        self.assertNotIn("*Founder:*", text)
        self.assertIn("*Company:*", text)

    def test_handle_alone_still_populates_founder(self) -> None:
        text = rendered(build_blocks(alert(founder=None, founder_handle="@acmeai")))
        self.assertIn("@acmeai", text)

    def test_programme_profile_link_is_included_when_distinct(self) -> None:
        text = rendered(build_blocks(alert(
            profile_url="https://www.ycombinator.com/companies/acme")))
        self.assertIn("Programme profile", text)

    def test_long_excerpt_is_clipped(self) -> None:
        text = rendered(build_blocks(alert(excerpt="x" * 2000)))
        self.assertLess(len(text), 1800)

    def test_markup_characters_are_escaped(self) -> None:
        text = rendered(build_blocks(alert(company_name="A<b>&C")))
        self.assertIn("&lt;b&gt;", text)
        self.assertNotIn("A<b>", text)

    def test_blocks_are_valid_shapes(self) -> None:
        for block in build_blocks(alert()):
            self.assertIn(block["type"], {"header", "section", "context"})
            self.assertLessEqual(len(json.dumps(block)), 3000)


class HeadlineHonestyTest(unittest.TestCase):
    def test_scoop_wording_only_after_a_real_check(self) -> None:
        card = alert(official=OfficialCheck(
            state=OfficialState.NOT_SEEN, accounts_checked=("official_x",),
            snapshots_seen=2, checked_at=utcnow()))
        self.assertEqual(headline(card), "EARLY YC SIGNAL — Founder Announced Before YC")

    def test_unchecked_claim_is_never_presented_as_a_scoop(self) -> None:
        """The integrity case.

        With no official snapshot source the card previously still said
        'Founder Announced Before YC' and stamped a check timestamp, asserting a
        verification that never happened.
        """
        card = alert(official=OfficialCheck(state=OfficialState.NOT_CHECKED))
        self.assertEqual(headline(card), "FOUNDER CLAIM — unverified (no official check configured)")
        self.assertIn("no official-snapshot source is configured", status_line(card))
        self.assertNotIn("not yet officially announced", status_line(card))

    def test_already_announced_is_labelled_as_such(self) -> None:
        card = alert(official=OfficialCheck(
            state=OfficialState.SEEN, accounts_checked=("official_x",),
            snapshots_seen=1, checked_at=utcnow(),
            matched_url="https://x.com/ycombinator/status/9"))
        self.assertIn("already announced", headline(card).lower() + status_line(card).lower())
        self.assertIn("Official post", rendered(build_blocks(card)))

    def test_status_names_the_accounts_that_were_checked(self) -> None:
        card = alert(official=OfficialCheck(
            state=OfficialState.NOT_SEEN, accounts_checked=("official_x", "official_linkedin"),
            snapshots_seen=7, checked_at=utcnow()))
        line = status_line(card)
        self.assertIn("official_x", line)
        self.assertIn("7 snapshot", line)

    def test_confirmed_directory_alert_states_public_record(self) -> None:
        card = alert(kind=SignalKind.CONFIRMED, source=Source.A16Z_SPEEDRUN,
                     programme="a16z Speedrun",
                     official=OfficialCheck(state=OfficialState.NOT_CHECKED))
        self.assertEqual(headline(card), "NEW A16Z SPEEDRUN COMPANY")
        self.assertIn("public record", status_line(card))

    def test_company_page_has_its_own_headline(self) -> None:
        card = alert(kind=SignalKind.COMPANY_PAGE_FIRST_SEEN,
                     source=Source.LINKEDIN_COMPANY,
                     official=OfficialCheck(state=OfficialState.NOT_CHECKED))
        self.assertIn("first observed", headline(card).lower())


if __name__ == "__main__":
    unittest.main()


class AffiliationAlertTest(unittest.TestCase):
    """"backed by Y Combinator" is reported without overstating it."""

    def card(self):
        return alert(
            kind=SignalKind.AFFILIATION_MENTION,
            excerpt="We are backed by Y Combinator.",
            official=OfficialCheck(state=OfficialState.NOT_CHECKED),
        )

    def test_headline_does_not_claim_a_new_acceptance(self) -> None:
        text = headline(self.card())
        self.assertIn("affiliation only", text)
        self.assertNotIn("Announced Before YC", text)

    def test_status_explains_why_it_is_weaker(self) -> None:
        self.assertIn("not a new acceptance", status_line(self.card()))

    def test_card_still_renders_every_required_field(self) -> None:
        text = rendered(build_blocks(self.card()))
        for expected in ("*Company:*", "*Source:*", "Original post", "Detected"):
            self.assertIn(expected, text)
