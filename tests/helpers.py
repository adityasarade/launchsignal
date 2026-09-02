"""Shared fixtures. No test touches the network."""

from __future__ import annotations

import logging
import os
import tempfile
import unittest

# Several tests deliberately fail a source or a send. Their stack traces are the
# expected behaviour, not a problem, so keep them out of the test output.
logging.disable(logging.CRITICAL)

from launchsignal.models import Evidence, Source
from launchsignal.notify import SlackNotifier
from launchsignal.service import Monitor
from launchsignal.store import Store


class FakeSource:
    """An adapter that yields a fixed list, or raises, on demand."""

    def __init__(self, name: Source, items=(), error: Exception | None = None):
        self.name = name
        self.items = list(items)
        self.error = error
        self.scans = 0

    def scan(self, store=None):
        self.scans += 1
        if self.error is not None:
            raise self.error
        yield from self.items


class RecordingNotifier(SlackNotifier):
    """Captures alerts instead of calling Slack; can be told to fail."""

    def __init__(self, fail_on: set[str] | None = None, error=None):
        super().__init__(sleeper=lambda _s: None)
        self.sent: list = []
        self.fail_on = fail_on or set()
        self.error = error

    @property
    def dry_run(self) -> bool:
        return False

    def check(self):
        return {"mode": "test", "team": "t", "bot": "b", "channel": "c"}

    def send(self, alert):
        if alert.company_name in self.fail_on:
            raise self.error
        self.sent.append(alert)
        return f"ts.{len(self.sent)}"


def x_post(identifier: str, text: str, *, title: str = "") -> Evidence:
    return Evidence(
        source=Source.X,
        external_id=identifier,
        url=f"https://x.com/founder/status/{identifier}",
        title=title,
        excerpt=text,
    )


def directory_record(slug: str, name: str, *, batch: str | None = None) -> Evidence:
    return Evidence(
        source=Source.YC_DIRECTORY,
        external_id=slug,
        url=f"https://www.ycombinator.com/companies/{slug}",
        title=name,
        excerpt="A public directory listing.",
        company_name=name,
        programme="YC",
        batch=batch,
        profile_url=f"https://www.ycombinator.com/companies/{slug}",
    )


class MonitorCase(unittest.TestCase):
    """Base class giving each test a private database and notifier."""

    def setUp(self) -> None:
        self._previous = os.environ.get("SLACK_DRY_RUN")
        os.environ["SLACK_DRY_RUN"] = "false"
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = Store(f"{self._dir.name}/test.sqlite3")
        self.addCleanup(self.store.close)
        self.notifier = RecordingNotifier()
        self.monitor = Monitor(self.store, self.notifier)

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop("SLACK_DRY_RUN", None)
        else:
            os.environ["SLACK_DRY_RUN"] = self._previous

    def run_scan(self, *sources, official=()):
        return self.monitor.run(list(sources), list(official))
