"""Claim detection and company resolution."""

import unittest

from launchsignal.classifier import (
    author_name,
    batch_label,
    canonical_name,
    claim_kind,
    founder_handle,
    official_check,
    official_mentions_company,
    resolve_company,
)
from launchsignal.models import (
    Evidence,
    OfficialState,
    SignalKind,
    Source,
    utcnow,
)


def post(text: str, title: str = "") -> Evidence:
    return Evidence(
        source=Source.X,
        external_id="1",
        url="https://x.com/f/status/1",
        title=title,
        excerpt=text,
    )


class ClaimDetectionTest(unittest.TestCase):
    def test_first_person_acceptance_is_an_early_claim(self) -> None:
        for text in (
            "We got into YC S26 and are moving to San Francisco.",
            "Thrilled to say we were accepted into Y Combinator!",
            "We're in YC — starting next month.",
            "Huge: we got accepted into YC W26.",
        ):
            self.assertEqual(claim_kind(post(text)), SignalKind.EARLY_FOUNDER_CLAIM, text)

    def test_speedrun_claim_is_detected(self) -> None:
        self.assertEqual(
            claim_kind(post("We joined a16z Speedrun SR004!")),
            SignalKind.EARLY_FOUNDER_CLAIM,
        )

    def test_backed_by_is_not_a_new_acceptance(self) -> None:
        """'Backed by YC' is true of every alumnus forever and is not news."""
        self.assertEqual(
            claim_kind(post("We are backed by Y Combinator and Sequoia.")),
            SignalKind.NONE,
        )

    def test_unrelated_post_is_not_a_claim(self) -> None:
        self.assertEqual(
            claim_kind(post("Congrats to all the YC founders out there!")),
            SignalKind.NONE,
        )

    def test_directory_evidence_is_confirmed_not_a_claim(self) -> None:
        evidence = Evidence(
            source=Source.YC_DIRECTORY,
            external_id="acme",
            url="u",
            title="Acme",
            excerpt="",
            company_name="Acme",
        )
        self.assertEqual(claim_kind(evidence), SignalKind.CONFIRMED)

    def test_company_page_is_corroboration_not_a_claim(self) -> None:
        evidence = Evidence(
            source=Source.LINKEDIN_COMPANY,
            external_id="c",
            url="https://www.linkedin.com/company/acme/",
            title="Acme",
            excerpt="",
            company_name="Acme",
        )
        self.assertEqual(claim_kind(evidence), SignalKind.COMPANY_PAGE_FIRST_SEEN)


class CompanyResolutionTest(unittest.TestCase):
    def test_resolves_company_from_founder_of_phrasing(self) -> None:
        name, reason = resolve_company(
            post("We got into YC S26. Founder of Hebbian Robotics.")
        )
        self.assertEqual(name, "Hebbian Robotics")
        self.assertEqual(reason, "founder-of")

    def test_resolves_company_stated_as_the_subject(self) -> None:
        name, _ = resolve_company(post("Acme AI got accepted into YC S26!"))
        self.assertEqual(name, "Acme AI")

    def test_does_not_swallow_the_leading_verb(self) -> None:
        """'Introducing X' names X, not 'Introducing X'."""
        name, _ = resolve_company(
            post("Introducing Orbital Freight. We got into YC S26.")
        )
        self.assertEqual(name, "Orbital Freight")

    def test_does_not_capture_a_whole_clause(self) -> None:
        """A greedy capture used to yield 'Freight Inc. Come join us in SF'."""
        name, _ = resolve_company(
            post(
                "Excited to share that we got into YC S26! Building the future of "
                "logistics at Freight Inc. Come join us in San Francisco."
            )
        )
        self.assertEqual(name, "Freight Inc")

    def test_refuses_to_invent_a_name(self) -> None:
        """No company named means no alert -- not a guessed one."""
        for text in (
            "i'm a founder and we got accepted into y combinator",
            "Thrilled - we got into YC W26 and are hiring!",
        ):
            name, reason = resolve_company(post(text))
            self.assertIsNone(name, text)
            self.assertEqual(reason, "unresolved")

    def test_linkedin_person_title_is_an_author_not_a_company(self) -> None:
        """'Jane Doe on LinkedIn:' must not become a company called Jane Doe."""
        evidence = post(
            "we just got into YC S26 and are hiring",
            title="Jane Doe on LinkedIn: big news",
        )
        name, _ = resolve_company(evidence)
        self.assertIsNone(name)
        self.assertEqual(author_name(evidence), "Jane Doe")

    def test_x_account_title_with_handle_resolves(self) -> None:
        evidence = post("We got into YC S26!", title="Acme AI (@acmeai) on X")
        name, reason = resolve_company(evidence)
        self.assertEqual(name, "Acme AI")
        self.assertEqual(reason, "x-account-title")
        self.assertEqual(founder_handle(evidence), "@acmeai")


class BatchLabelTest(unittest.TestCase):
    def test_extracts_batch_from_a_post(self) -> None:
        self.assertEqual(batch_label(post("we got into YC S26!")), "YC S26")
        self.assertEqual(batch_label(post("accepted into YC W26")), "YC W26")

    def test_extracts_speedrun_cohort(self) -> None:
        self.assertEqual(batch_label(post("joining a16z Speedrun SR003")), "SR003")

    def test_prefers_the_value_the_source_supplied(self) -> None:
        evidence = Evidence(
            source=Source.YC_DIRECTORY,
            external_id="a",
            url="u",
            title="Acme",
            excerpt="",
            batch="YC F24",
        )
        self.assertEqual(batch_label(evidence), "YC F24")

    def test_no_batch_when_none_is_stated(self) -> None:
        self.assertIsNone(batch_label(post("we got into Y Combinator")))


class OfficialComparisonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.official = [
            Evidence(
                source=Source.OFFICIAL_X,
                external_id="o1",
                url="https://x.com/ycombinator/status/2",
                title="YC S26",
                excerpt="Congratulations to Multiply, Searchlight and Arcade on joining YC S26.",
            )
        ]

    def test_whole_token_match_only(self) -> None:
        """Substring matching reported Arc inside Arcade, silently killing alerts."""
        for name in ("Arc", "Ply", "Light", "Cade"):
            self.assertFalse(
                official_mentions_company(name, self.official),
                f"{name} must not match a longer unrelated token",
            )

    def test_genuine_mentions_still_match(self) -> None:
        for name in ("Multiply", "Searchlight", "Arcade"):
            self.assertTrue(official_mentions_company(name, self.official), name)

    def test_multiword_name_matches_in_sequence(self) -> None:
        official = [
            Evidence(
                source=Source.OFFICIAL_X,
                external_id="o",
                url="u",
                title="",
                excerpt="Welcome Hebbian Robotics to the batch.",
            )
        ]
        self.assertTrue(official_mentions_company("Hebbian Robotics", official))
        self.assertFalse(official_mentions_company("Robotics Hebbian", official))

    def test_legal_suffixes_are_ignored_when_comparing(self) -> None:
        self.assertEqual(canonical_name("Freight Inc."), canonical_name("Freight"))

    def test_no_accounts_checked_reports_not_checked(self) -> None:
        """The critical honesty case: nothing checked is not 'not announced'."""
        check = official_check("Acme", [], (), utcnow())
        self.assertEqual(check.state, OfficialState.NOT_CHECKED)
        self.assertFalse(check.performed)
        self.assertIn("not checked", check.describe())

    def test_checked_and_absent_reports_not_seen_with_audit_trail(self) -> None:
        check = official_check("Acme", self.official, ("official_x",), utcnow())
        self.assertEqual(check.state, OfficialState.NOT_SEEN)
        self.assertTrue(check.performed)
        self.assertEqual(check.snapshots_seen, 1)
        self.assertIn("official_x", check.describe())

    def test_checked_and_present_reports_seen_with_the_url(self) -> None:
        check = official_check("Multiply", self.official, ("official_x",), utcnow())
        self.assertEqual(check.state, OfficialState.SEEN)
        self.assertEqual(check.matched_url, "https://x.com/ycombinator/status/2")


if __name__ == "__main__":
    unittest.main()


class RealWorldDirectoryNameTest(unittest.TestCase):
    """Names taken verbatim from the live YC directory.

    These were all silently dropped when the tweet-sanitising rules were also
    applied to authoritative source-provided names.
    """

    def resolve(self, name: str):
        return resolve_company(
            Evidence(
                source=Source.YC_DIRECTORY,
                external_id="slug",
                url="https://www.ycombinator.com/companies/slug",
                title=name,
                excerpt="",
                company_name=name,
            )
        )

    def test_long_multiword_names_are_kept(self) -> None:
        for name in (
            "Mark Cuban Cost Plus Drug Company PBC",
            "Sanitation and Health Rights in India (SHRI)",
            "Evolve, Makers of Podcast App and Rest",
            "Touch and Pay Technologies Limited",
            "DeepAware AI Robotics Center of Silicon Valley",
        ):
            resolved, reason = self.resolve(name)
            self.assertEqual(resolved, name, f"{name!r} must not be trimmed")
            self.assertEqual(reason, "source-provided")

    def test_single_common_word_names_are_kept(self) -> None:
        """Real YC companies are called Super, Here, Welcome, Her and Seed."""
        for name in ("Super", "Here", "Welcome", "Her", "Seed", "Pre", "L"):
            resolved, _ = self.resolve(name)
            self.assertEqual(resolved, name, f"{name!r} is a real company name")

    def test_names_with_no_alphanumeric_content_are_rejected(self) -> None:
        for name in ("", "   ", "---", "!!!"):
            resolved, reason = self.resolve(name)
            self.assertIsNone(resolved)
            self.assertEqual(reason, "unresolved")

    def test_absurdly_long_names_are_rejected(self) -> None:
        resolved, _ = self.resolve("A" * 200)
        self.assertIsNone(resolved)

    def test_social_extraction_still_sanitises_aggressively(self) -> None:
        """Trusting a source name must not loosen extraction from prose."""
        resolved, _ = resolve_company(
            post(
                "Excited to share that we got into YC S26! Building the future of "
                "logistics at Freight Inc. Come join us in San Francisco."
            )
        )
        self.assertEqual(resolved, "Freight Inc")
