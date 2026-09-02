"""The .env loader and HTTP retry behaviour."""

import os
import tempfile
import unittest
from pathlib import Path

from launchsignal.config import load_env
from launchsignal.http import SourceError, request


class EnvLoaderTest(unittest.TestCase):
    """Without this loader the documented setup silently does nothing."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / ".env"
        self._added: list[str] = []

    def tearDown(self) -> None:
        for key in self._added:
            os.environ.pop(key, None)

    def write(self, body: str) -> str:
        self.path.write_text(body, encoding="utf-8")
        return str(self.path)

    def test_loads_simple_pairs(self) -> None:
        path = self.write("LS_TEST_A=hello\nLS_TEST_B=world\n")
        self._added += ["LS_TEST_A", "LS_TEST_B"]
        loaded = load_env(path)
        self.assertEqual(set(loaded), {"LS_TEST_A", "LS_TEST_B"})
        self.assertEqual(os.environ["LS_TEST_A"], "hello")

    def test_ignores_comments_and_blank_lines(self) -> None:
        path = self.write("# a comment\n\n  \nLS_TEST_C=value\n")
        self._added.append("LS_TEST_C")
        self.assertEqual(load_env(path), ["LS_TEST_C"])

    def test_strips_quotes_and_export_prefix(self) -> None:
        path = self.write("export LS_TEST_D=\"quoted value\"\nLS_TEST_E='single'\n")
        self._added += ["LS_TEST_D", "LS_TEST_E"]
        load_env(path)
        self.assertEqual(os.environ["LS_TEST_D"], "quoted value")
        self.assertEqual(os.environ["LS_TEST_E"], "single")

    def test_strips_trailing_inline_comment_on_bare_values(self) -> None:
        path = self.write("LS_TEST_F=true # keep this off\n")
        self._added.append("LS_TEST_F")
        load_env(path)
        self.assertEqual(os.environ["LS_TEST_F"], "true")

    def test_a_token_containing_hash_is_preserved_when_quoted(self) -> None:
        path = self.write("LS_TEST_G=\"abc#def\"\n")
        self._added.append("LS_TEST_G")
        load_env(path)
        self.assertEqual(os.environ["LS_TEST_G"], "abc#def")

    def test_real_environment_wins_by_default(self) -> None:
        """A deployment's injected secret must not be clobbered by a stale file."""
        os.environ["LS_TEST_H"] = "from-environment"
        self._added.append("LS_TEST_H")
        load_env(self.write("LS_TEST_H=from-file\n"))
        self.assertEqual(os.environ["LS_TEST_H"], "from-environment")

    def test_override_is_available_when_asked_for(self) -> None:
        os.environ["LS_TEST_I"] = "from-environment"
        self._added.append("LS_TEST_I")
        load_env(self.write("LS_TEST_I=from-file\n"), override=True)
        self.assertEqual(os.environ["LS_TEST_I"], "from-file")

    def test_missing_file_is_not_an_error(self) -> None:
        self.assertEqual(load_env(str(Path(self.dir.name) / "nope.env")), [])

    def test_returns_names_only_never_values(self) -> None:
        path = self.write("LS_TEST_J=super-secret-value\n")
        self._added.append("LS_TEST_J")
        self.assertEqual(load_env(path), ["LS_TEST_J"])


class HttpRetryTest(unittest.TestCase):
    def test_transient_status_is_retried_then_reported(self) -> None:
        import urllib.error
        import urllib.request

        attempts = {"n": 0}
        delays: list[float] = []

        def failing(request_obj, timeout=None):
            attempts["n"] += 1
            error = urllib.error.HTTPError(
                request_obj.full_url, 503, "unavailable", {}, None
            )
            error.close()
            raise error

        original = urllib.request.urlopen
        urllib.request.urlopen = failing
        try:
            with self.assertRaises(SourceError) as caught:
                request(
                    "https://example.com/x",
                    source="test",
                    attempts=3,
                    sleeper=delays.append,
                )
        finally:
            urllib.request.urlopen = original
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(len(delays), 2, "should back off between attempts")
        self.assertIn("test:", str(caught.exception))

    def test_permanent_status_is_not_retried(self) -> None:
        import urllib.error
        import urllib.request

        attempts = {"n": 0}

        def failing(request_obj, timeout=None):
            attempts["n"] += 1
            error = urllib.error.HTTPError(request_obj.full_url, 404, "gone", {}, None)
            error.close()
            raise error

        original = urllib.request.urlopen
        urllib.request.urlopen = failing
        try:
            with self.assertRaises(SourceError):
                request("https://example.com/x", source="test", attempts=4,
                        sleeper=lambda _s: None)
        finally:
            urllib.request.urlopen = original
        self.assertEqual(attempts["n"], 1, "a 404 must not be retried")

    def test_query_string_is_stripped_from_error_messages(self) -> None:
        """An API key passed as a query parameter must never reach a log."""
        import urllib.error
        import urllib.request

        def failing(request_obj, timeout=None):
            error = urllib.error.HTTPError(request_obj.full_url, 404, "gone", {}, None)
            error.close()
            raise error

        original = urllib.request.urlopen
        urllib.request.urlopen = failing
        try:
            with self.assertRaises(SourceError) as caught:
                request("https://api.example.com/search?api_key=SECRET123&q=x",
                        source="test", sleeper=lambda _s: None)
        finally:
            urllib.request.urlopen = original
        self.assertNotIn("SECRET123", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class EnvDuplicateKeyTest(unittest.TestCase):
    """A duplicate key inside .env must be last-wins, like every other loader."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / ".env"

    def tearDown(self) -> None:
        for key in ("LS_DUP_A", "LS_DUP_B"):
            os.environ.pop(key, None)

    def test_later_line_overrides_an_earlier_one(self) -> None:
        self.path.write_text("LS_DUP_A=first\nLS_DUP_A=second\n", encoding="utf-8")
        loaded = load_env(str(self.path))
        self.assertEqual(os.environ["LS_DUP_A"], "second")
        self.assertEqual(loaded.count("LS_DUP_A"), 1, "reported once, not twice")

    def test_real_environment_still_wins_over_the_whole_file(self) -> None:
        os.environ["LS_DUP_B"] = "from-environment"
        self.path.write_text("LS_DUP_B=first\nLS_DUP_B=second\n", encoding="utf-8")
        load_env(str(self.path))
        self.assertEqual(os.environ["LS_DUP_B"], "from-environment")


class UrlSchemeGuardTest(unittest.TestCase):
    """Endpoints are configurable, so a non-web scheme must be refused."""

    def test_non_web_schemes_are_rejected(self) -> None:
        from launchsignal.http import require_web_url

        for url in (
            "file:///etc/passwd",
            "ftp://example.com/x",
            "gopher://example.com",
            "data:text/plain,hi",
        ):
            with self.assertRaises(SourceError, msg=url):
                require_web_url(url, "test")

    def test_a_url_with_no_host_is_rejected(self) -> None:
        from launchsignal.http import require_web_url

        with self.assertRaises(SourceError):
            require_web_url("https:///nohost", "test")

    def test_ordinary_web_urls_pass(self) -> None:
        from launchsignal.http import require_web_url

        for url in ("http://example.com", "https://example.com/path?q=1"):
            self.assertEqual(require_web_url(url, "test"), url)
