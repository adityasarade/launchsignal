"""Source parsing. Every fixture is inline; no test touches the network."""

import unittest

from launchsignal import sources
from launchsignal.http import SourceError
from launchsignal.models import Source

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.ycombinator.com/companies/industry/aiops</loc></url>
  <url><loc>https://www.ycombinator.com/companies/fresco</loc><lastmod>2026-08-30</lastmod></url>
  <url><loc>https://www.ycombinator.com/companies/ticket-wallet</loc><lastmod>2026-08-31</lastmod></url>
</urlset>"""

PROFILE = (
    '<html><body><script>{&quot;company&quot;:{&quot;id&quot;:30089,'
    '&quot;slug&quot;:&quot;fresco&quot;,&quot;name&quot;:&quot;Fresco&quot;,'
    '&quot;batch&quot;:&quot;F24&quot;,&quot;batch_name&quot;:&quot;Fall 2024&quot;,'
    '&quot;description&quot;:&quot;AI copilot for construction estimators&quot;,'
    '&quot;url&quot;:&quot;https://fresco-ai.com/&quot;}}</script></body></html>'
)


class SitemapParsingTest(unittest.TestCase):
    def test_only_company_urls_are_kept(self) -> None:
        entries = list(sources._parse_sitemap(SITEMAP, sources.YcSitemapSource.slug_pattern, "yc"))
        self.assertEqual([slug for slug, _, _ in entries], ["fresco", "ticket-wallet"])

    def test_lastmod_is_captured(self) -> None:
        entries = list(sources._parse_sitemap(SITEMAP, sources.YcSitemapSource.slug_pattern, "yc"))
        self.assertEqual(entries[0][2], "2026-08-30")

    def test_invalid_xml_is_a_named_source_error(self) -> None:
        with self.assertRaises(SourceError) as caught:
            list(sources._parse_sitemap("<html><meta></html>", sources.YcSitemapSource.slug_pattern, "yc"))
        self.assertIn("not valid XML", str(caught.exception))

    def test_html_error_page_is_rejected_not_silently_parsed(self) -> None:
        """A well-formed HTML error page parses as valid XML and yields nothing.

        Treating that as a clean scan makes the monitor silently blind, so it is
        refused. The DOCTYPE guard catches the common case first.
        """
        page = "<!DOCTYPE html><html><body>Access denied</body></html>"
        with self.assertRaises(SourceError) as caught:
            list(sources._parse_sitemap(page, sources.YcSitemapSource.slug_pattern, "yc"))
        self.assertIn("DOCTYPE", str(caught.exception))

    def test_an_xml_page_with_no_company_urls_is_rejected(self) -> None:
        page = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>'
        adapter = sources.YcSitemapSource(enrich=False)
        original = sources.get_text
        sources.get_text = lambda *a, **k: (page, type("R", (), {"etag": None})())
        try:
            with self.assertRaises(SourceError) as caught:
                list(adapter.scan(None))
            self.assertIn("no company URLs", str(caught.exception))
        finally:
            sources.get_text = original

    def test_an_entity_declaration_is_refused(self) -> None:
        """Entity expansion is the attack vector against the stdlib parser."""
        bomb = (
            '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
            "<urlset><url><loc>x</loc></url></urlset>"
        )
        with self.assertRaises(SourceError) as caught:
            list(sources._parse_sitemap(bomb, sources.YcSitemapSource.slug_pattern, "yc"))
        self.assertIn("refusing to parse", str(caught.exception))

    def test_an_oversized_document_is_refused(self) -> None:
        huge = "<urlset>" + ("x" * (sources.MAX_SITEMAP_BYTES + 1))
        with self.assertRaises(SourceError) as caught:
            list(sources._parse_sitemap(huge, sources.YcSitemapSource.slug_pattern, "yc"))
        self.assertIn("exceeds", str(caught.exception))


class ProfileEnrichmentTest(unittest.TestCase):
    def test_batch_description_and_name_are_extracted(self) -> None:
        detail = sources._extract_yc_profile(PROFILE)
        self.assertEqual(detail["name"], "Fresco")
        self.assertEqual(detail["batch"], "F24")
        self.assertEqual(detail["description"], "AI copilot for construction estimators")
        self.assertEqual(detail["website"], "https://fresco-ai.com/")

    def test_batch_label_prefers_the_short_code(self) -> None:
        self.assertEqual(sources._yc_batch_label({"batch": "F24", "batch_name": "Fall 2024"}), "YC F24")

    def test_batch_label_falls_back_to_the_long_name(self) -> None:
        self.assertEqual(sources._yc_batch_label({"batch_name": "Fall 2024"}), "YC Fall 2024")

    def test_no_batch_data_yields_none(self) -> None:
        self.assertIsNone(sources._yc_batch_label({}))

    def test_scan_yields_batch_and_description_for_new_slugs(self) -> None:
        adapter = sources.YcSitemapSource()
        original_text = sources.get_text

        def fake_get_text(url, **kwargs):
            body = PROFILE if "/fresco" in url and "sitemap" not in url else SITEMAP
            return body, type("R", (), {"etag": '"abc"'})()

        sources.get_text = fake_get_text
        try:
            items = list(adapter.scan(None))
        finally:
            sources.get_text = original_text
        fresco = next(i for i in items if i.external_id == "fresco")
        self.assertEqual(fresco.company_name, "Fresco")
        self.assertEqual(fresco.batch, "YC F24")
        self.assertEqual(fresco.excerpt, "AI copilot for construction estimators")
        self.assertEqual(fresco.metadata["lastmod"], "2026-08-30")


class SpeedrunPaginationTest(unittest.TestCase):
    def _fake_api(self, pages):
        calls = []

        def fake(url, **kwargs):
            calls.append(url)
            index = len(calls) - 1
            return (pages[index] if index < len(pages) else pages[-1]), None

        return fake, calls

    def test_offset_advances_by_page_size(self) -> None:
        pages = [
            {"results": [{"id": f"i{n}", "name": f"C{n}", "slug": f"c{n}"} for n in range(100)],
             "next": "more"},
            {"results": [{"id": "z", "name": "Zed", "slug": "zed"}], "next": None},
        ]
        fake, calls = self._fake_api(pages)
        original = sources.get_json
        sources.get_json = fake
        try:
            items = list(sources.A16zSpeedrunSource().scan(None))
        finally:
            sources.get_json = original
        self.assertEqual(len(items), 101)
        self.assertIn("offset=0", calls[0])
        self.assertIn("offset=100", calls[1])

    def test_empty_page_with_next_set_raises_instead_of_looping(self) -> None:
        """offset += len(records) never advanced here, so the loop ran forever."""
        fake, calls = self._fake_api([{"results": [], "next": "more"}])
        original = sources.get_json
        sources.get_json = fake
        try:
            with self.assertRaises(SourceError) as caught:
                list(sources.A16zSpeedrunSource().scan(None))
        finally:
            sources.get_json = original
        self.assertIn("empty page", str(caught.exception))
        self.assertEqual(len(calls), 1, "must not retry the same offset")

    def test_record_maps_cohort_founder_and_profile_url(self) -> None:
        record = {
            "id": "uuid-1", "name": "2weeks", "slug": "2weeks", "cohort": "SR003",
            "description": "Breakout hits on the open web.",
            "website_url": "https://2weeks.games",
            "x_url": "https://x.com/2weeksgames",
            "linkedin_url": "https://www.linkedin.com/company/2weeks-corp/",
            "founder_set": [{"first_name": "Brandon", "last_name": "Dillon", "title": "CEO"}],
        }
        evidence = sources.A16zSpeedrunSource()._to_evidence(record)
        self.assertEqual(evidence.programme, "a16z Speedrun")
        self.assertEqual(evidence.batch, "SR003")
        self.assertEqual(evidence.founder, "Brandon Dillon")
        self.assertEqual(evidence.founder_handle, "@2weeksgames")
        # The alert links to the programme record, not the company's own site.
        self.assertEqual(evidence.url, "https://speedrun.a16z.com/companies/2weeks")

    def test_records_missing_an_id_or_name_are_skipped(self) -> None:
        adapter = sources.A16zSpeedrunSource()
        self.assertIsNone(adapter._to_evidence({"name": "No id"}))
        self.assertIsNone(adapter._to_evidence({"id": "x"}))


class UrlFilterTest(unittest.TestCase):
    def test_official_source_requires_the_named_account(self) -> None:
        """Any x.com status used to count as an official YC announcement."""
        allowed = ("ycombinator",)
        self.assertTrue(sources.is_allowed_public_url(
            Source.OFFICIAL_X, "https://x.com/ycombinator/status/1", allowed))
        self.assertFalse(sources.is_allowed_public_url(
            Source.OFFICIAL_X, "https://x.com/randomguy/status/1", allowed))
        self.assertFalse(sources.is_allowed_public_url(
            Source.OFFICIAL_X, "https://x.com/competitor_vc/status/9", allowed))

    def test_candidate_posts_accept_twitter_and_mobile_hosts(self) -> None:
        for url in (
            "https://x.com/f/status/1",
            "https://twitter.com/f/status/1",
            "https://mobile.x.com/f/status/1",
            "https://x.com/f/status/1?s=20",
        ):
            self.assertTrue(sources.is_allowed_public_url(Source.X, url), url)

    def test_non_post_urls_are_rejected(self) -> None:
        for url in ("https://x.com/somebody", "https://example.com/status/1", ""):
            self.assertFalse(sources.is_allowed_public_url(Source.X, url), url)

    def test_company_pages_are_a_separate_source(self) -> None:
        self.assertTrue(sources.is_allowed_public_url(
            Source.LINKEDIN_COMPANY, "https://www.linkedin.com/company/acme/"))
        self.assertFalse(sources.is_allowed_public_url(
            Source.LINKEDIN, "https://www.linkedin.com/company/acme/"))

    def test_tracking_parameters_do_not_create_a_second_identity(self) -> None:
        """Hashing the raw URL made ?s=20 a different post, alerting twice."""
        self.assertEqual(
            sources._canonical_id("https://x.com/a/status/123?s=20&t=xyz"),
            sources._canonical_id("https://twitter.com/a/status/123"),
        )

    def test_linkedin_activity_id_is_the_identity(self) -> None:
        self.assertEqual(
            sources._canonical_id("https://www.linkedin.com/posts/jane-doe_activity-7890"),
            sources._canonical_id("https://linkedin.com/posts/other_activity-7890"),
        )


class QueryRegistryTest(unittest.TestCase):
    def test_linkedin_company_pages_have_their_own_query_family(self) -> None:
        """Requirement 4 asks for new company pages, not only posts."""
        names = {s.name for s in sources.social_sources()}
        self.assertIn(Source.LINKEDIN_COMPANY, names)
        company = next(s for s in sources.social_sources() if s.name is Source.LINKEDIN_COMPANY)
        self.assertTrue(all("linkedin.com/company" in q for q in company.queries))

    def test_fast_lane_passes_a_freshness_window(self) -> None:
        for source in sources.social_sources(recency_minutes=120):
            self.assertEqual(source.recency_minutes, 120)

    def test_official_sources_pin_their_accounts(self) -> None:
        for source in sources.official_sources():
            self.assertTrue(source.allowed_authors, "an official source must pin an account")

    def test_batches_are_configurable(self) -> None:
        import os
        os.environ["LAUNCHSIGNAL_BATCHES"] = "YC S27,YC W27"
        try:
            queries = " ".join(sources.social_sources()[0].queries)
            self.assertIn("YC S27", queries)
            self.assertNotIn("YC S26", queries)
        finally:
            del os.environ["LAUNCHSIGNAL_BATCHES"]

    def test_unconfigured_tinyfish_is_an_error_not_a_silent_pass(self) -> None:
        """Returning quietly made X and LinkedIn look scanned when they were not."""
        import os
        previous = os.environ.pop("TINYFISH_API_KEY", None)
        try:
            source = sources.TinyfishSearchSource(Source.X, ["site:x.com test"])
            self.assertFalse(source.configured)
            with self.assertRaises(SourceError):
                list(source.scan(None))
        finally:
            if previous:
                os.environ["TINYFISH_API_KEY"] = previous


if __name__ == "__main__":
    unittest.main()


class BaselineEnrichmentTest(unittest.TestCase):
    """Profile enrichment must not be spent on the silent baseline."""

    def _adapter_with_fakes(self):
        calls = []

        def fake_get_text(url, **kwargs):
            calls.append(url)
            body = PROFILE if "sitemap" not in url else SITEMAP
            return body, type("R", (), {"etag": '"e"'})()

        return fake_get_text, calls

    def test_baseline_scan_does_not_fetch_profiles(self) -> None:
        import tempfile

        from launchsignal.store import Store

        fake, calls = self._adapter_with_fakes()
        original = sources.get_text
        sources.get_text = fake
        with tempfile.TemporaryDirectory() as directory:
            store = Store(f"{directory}/s.sqlite3")
            try:
                items = list(sources.YcSitemapSource().scan(store))
            finally:
                sources.get_text = original
                store.close()
        self.assertEqual(len(items), 2)
        self.assertEqual([c for c in calls if "sitemap" not in c], [],
                         "baseline must not spend ~6000 profile fetches")
        self.assertTrue(all(i.batch is None for i in items))

    def test_already_observed_records_are_not_refetched(self) -> None:
        """Enrichment is for listings that will alert.

        Backfilling detail for thousands of already-observed companies costs
        one request each and changes nothing: they can never alert again.
        """
        import tempfile

        from launchsignal.store import Store

        fake, calls = self._adapter_with_fakes()
        original = sources.get_text
        sources.get_text = fake
        with tempfile.TemporaryDirectory() as directory:
            store = Store(f"{directory}/s.sqlite3")
            try:
                store.complete_baseline("yc_directory")
                for slug in ("fresco", "ticket-wallet"):
                    store.connection.execute(
                        """INSERT INTO observations (source, external_id, company_key,
                           url, title, excerpt, observed_at, metadata_json)
                           VALUES ('yc_directory', ?, 'k', 'u', 't', 'e', '2026-01-01', '{}')""",
                        (slug,),
                    )
                store.connection.commit()
                calls.clear()
                list(sources.YcSitemapSource().scan(store))
            finally:
                sources.get_text = original
                store.close()
        self.assertEqual([c for c in calls if "sitemap" not in c], [])

    def test_a_deferred_listing_stays_new_for_the_next_cycle(self) -> None:
        """Over the cap, a listing is skipped rather than stored without detail.

        Storing it unenriched strands it: it can never be enriched again and it
        alerts with no batch and no description.
        """
        import tempfile

        from launchsignal.store import Store

        fake, calls = self._adapter_with_fakes()
        original = sources.get_text
        sources.get_text = fake
        with tempfile.TemporaryDirectory() as directory:
            store = Store(f"{directory}/s.sqlite3")
            try:
                store.complete_baseline("yc_directory")
                first = list(sources.YcSitemapSource(max_enrich=1).scan(store))
                self.assertEqual(len(first), 1, "only the capped number is yielded")
                self.assertEqual(first[0].batch, "YC F24")
                # Nothing was recorded for the deferred slug, so a later cycle
                # still sees it as new.
                second = list(sources.YcSitemapSource(max_enrich=5).scan(store))
                self.assertEqual(len(second), 2)
            finally:
                sources.get_text = original
                store.close()

    def test_post_baseline_scan_enriches_new_companies(self) -> None:
        import tempfile

        from launchsignal.store import Store

        fake, calls = self._adapter_with_fakes()
        original = sources.get_text
        sources.get_text = fake
        with tempfile.TemporaryDirectory() as directory:
            store = Store(f"{directory}/s.sqlite3")
            try:
                store.complete_baseline("yc_directory")
                items = list(sources.YcSitemapSource().scan(store))
            finally:
                sources.get_text = original
                store.close()
        self.assertTrue([c for c in calls if "sitemap" not in c], "should fetch profiles")
        self.assertTrue(any(i.batch == "YC F24" for i in items))


class ExcludedSlugTest(unittest.TestCase):
    def test_yc_own_entry_is_not_a_portfolio_company(self) -> None:
        sitemap = SITEMAP.replace(
            "<url><loc>https://www.ycombinator.com/companies/fresco</loc><lastmod>2026-08-30</lastmod></url>",
            "<url><loc>https://www.ycombinator.com/companies/y-combinator</loc><lastmod>2026-08-30</lastmod></url>",
        )
        original = sources.get_text
        sources.get_text = lambda *a, **k: (sitemap, type("R", (), {"etag": None})())
        try:
            items = list(sources.YcSitemapSource(enrich=False).scan(None))
        finally:
            sources.get_text = original
        self.assertEqual([i.external_id for i in items], ["ticket-wallet"])
