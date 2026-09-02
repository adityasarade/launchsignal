"""The monitor.

One scan does four things, in this order:

1. Retry any alert that was staged but never confirmed delivered.
2. Collect official-account snapshots, so claims can be compared against them.
3. Read each source in isolation, so one failure cannot hide the others.
4. Turn genuinely new evidence into at-most-one alert per company per kind.

The first scan for a source is a silent baseline: it records what already
exists without alerting, so installing the monitor does not announce six
thousand companies.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable

from .classifier import (
    author_name,
    batch_label,
    claim_kind,
    founder_handle,
    official_check,
    resolve_company,
    resolve_programme,
)
from .http import SourceError
from .models import (
    Alert,
    Evidence,
    OfficialCheck,
    OfficialState,
    ReviewItem,
    SignalKind,
    SourceOutcome,
    utcnow,
)
from .notify import SlackConfigError, SlackNotifier, SlackSendError
from .store import Store, alert_from_payload, alert_key, company_key

LOGGER = logging.getLogger("launchsignal.service")


class Monitor:
    def __init__(self, store: Store, notifier: SlackNotifier) -> None:
        self.store = store
        self.notifier = notifier

    # --------------------------------------------------------------- scanning

    def run(
        self,
        sources: Iterable[object],
        official_sources: Iterable[object] = (),
    ) -> dict[str, object]:
        run_id = self.store.start_run()
        counts: dict[str, object] = {
            "observations": 0,
            "new": 0,
            "alerts": 0,
            "review": 0,
            "retried": 0,
            "baselined_sources": [],
            "sources": [],
        }

        counts["retried"] = self._flush_pending()
        official = self._collect_official(official_sources, counts)

        for source in sources:
            outcome = self._run_source(source, official, counts)
            counts["sources"].append(vars(outcome))

        failures = [s for s in counts["sources"] if not s["ok"]]
        outcome_label = "ok" if not failures else f"partial ({len(failures)} source(s) failed)"
        self.store.finish_run(run_id, counts, outcome_label)
        counts["outcome"] = outcome_label
        return counts

    def _run_source(
        self,
        source: object,
        official: dict[str, tuple[list[Evidence], tuple[str, ...]]],
        counts: dict[str, object],
    ) -> SourceOutcome:
        name = getattr(source, "name", None)
        label = name.value if name is not None else str(source)
        started = time.monotonic()
        # Baseline is decided per source. A source that has never succeeded must
        # seed silently even if its siblings are long past baseline, and a source
        # that fails mid-baseline must not drag the others back through one.
        baseline = not self.store.baseline_complete(label)
        outcome = SourceOutcome(source=label, ok=True)
        try:
            for evidence in source.scan(self.store):
                outcome.observations += 1
                counts["observations"] = int(counts["observations"]) + 1
                if self._ingest(evidence, baseline, official, counts, outcome):
                    outcome.new += 1
        except SourceError as error:
            # One bad source is reported and skipped. It does not abort the scan,
            # and it does not mark its own baseline complete.
            outcome.ok = False
            outcome.error = error.message
            self.store.record_source_failure(label, error.message)
            LOGGER.error("source %s failed: %s", label, error.message)
        except Exception as error:  # noqa: BLE001 - a source must never kill the run
            outcome.ok = False
            outcome.error = f"{type(error).__name__}: {error}"
            self.store.record_source_failure(label, outcome.error)
            LOGGER.exception("source %s raised unexpectedly", label)
        else:
            self.store.record_source_success(label)
            if baseline:
                # Only on a clean pass. Completing the baseline after a partial
                # read would permanently hide everything the failed half missed.
                self.store.complete_baseline(label)
                counts["baselined_sources"].append(label)
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        return outcome

    def _ingest(
        self,
        evidence: Evidence,
        baseline: bool,
        official: dict[str, tuple[list[Evidence], tuple[str, ...]]],
        counts: dict[str, object],
        outcome: SourceOutcome,
    ) -> bool:
        company, reason = resolve_company(evidence)
        if not company:
            # Never invent a name. An unresolved claim is a review item, so a
            # human can look at it instead of a garbage alert going out.
            if claim_kind(evidence) is not SignalKind.NONE:
                counts["review"] = int(counts["review"]) + 1
                self.store.add_review(
                    ReviewItem(
                        source=evidence.source,
                        url=evidence.url,
                        excerpt=evidence.text[:1000],
                        reason="company-unresolved",
                    )
                )
            return False

        # The programme is resolved from the claim, not assumed. A social
        # adapter cannot know it in advance, and defaulting to YC made every
        # Speedrun post an "EARLY YC SIGNAL" with a YC company key.
        programme = resolve_programme(evidence)
        key = company_key(company, programme)
        if not self.store.record_observation(evidence, key):
            return False
        counts["new"] = int(counts["new"]) + 1
        if baseline:
            return True

        alert = self._build_alert(evidence, company, programme, key, official)
        if alert is None:
            return True
        if self._deliver(alert):
            counts["alerts"] = int(counts["alerts"]) + 1
        return True

    def _build_alert(
        self,
        evidence: Evidence,
        company: str,
        programme: str,
        key: str,
        official: dict[str, tuple[list[Evidence], tuple[str, ...]]],
    ) -> Alert | None:
        kind = claim_kind(evidence)
        if kind in {SignalKind.NONE, SignalKind.NEEDS_REVIEW}:
            return None
        if kind is SignalKind.CONFIRMED:
            check = OfficialCheck(state=OfficialState.NOT_CHECKED)
        else:
            # Only this programme's own accounts count. Comparing a YC claim
            # against a16z's feed lets one programme's post suppress the
            # other's early signal.
            snapshots, accounts = official.get(programme, ([], ()))
            check = official_check(company, snapshots, accounts, utcnow())
        return Alert(
            # Keyed on company AND kind. A directory confirmation and an early
            # founder claim about the same company are different news, so they
            # get separate delivery slots. Keying on the company alone lets
            # whichever source is scanned first swallow the other's alert.
            alert_key=alert_key(key, kind),
            company_key=key,
            kind=kind,
            company_name=company,
            programme=programme,
            source=evidence.source,
            source_url=evidence.url,
            excerpt=evidence.excerpt,
            official=check,
            batch=batch_label(evidence),
            founder=author_name(evidence),
            founder_handle=founder_handle(evidence),
            profile_url=evidence.profile_url,
        )

    # --------------------------------------------------------------- delivery

    def _deliver(self, alert: Alert) -> bool:
        """Claim, send, then confirm.

        The claim is an atomic INSERT, so exactly one caller can own a given
        alert_key. The row is written before the Slack call: if the send fails
        or the process dies, the row survives and the next scan retries it.
        Sending first and recording after loses the alert permanently, because
        by then the observation is committed and no longer new.
        """
        if not self.store.claim_alert(alert):
            return False
        return self._send_claimed(alert)

    def _send_claimed(self, alert: Alert) -> bool:
        try:
            slack_ts = self.notifier.send(alert)
        except SlackConfigError:
            raise
        except SlackSendError as error:
            # A fatal error cannot be fixed by trying again, so it is recorded
            # as dead and surfaced in the health report rather than retried on
            # every scan until the attempt budget quietly runs out.
            self.store.mark_alert_failed(
                alert.alert_key, error.error_code, terminal=error.fatal
            )
            LOGGER.error(
                "alert %s not delivered (%s%s)",
                alert.company_name,
                error.error_code,
                "; fatal, will not retry" if error.fatal else "",
            )
            return False
        self.store.mark_alert_sent(alert.alert_key, slack_ts)
        return True

    def _flush_pending(self) -> int:
        """Deliver alerts staged by an earlier run that never confirmed."""
        import json

        sent = 0
        for row in self.store.pending_alerts():
            key = str(row["alert_key"])
            try:
                alert = alert_from_payload(json.loads(str(row["payload_json"])))
            except Exception:  # noqa: BLE001 - a bad row must not block the run
                LOGGER.warning("skipping unreadable pending alert %s", key)
                continue
            # Lease it before sending. Without the conditional update two
            # concurrent retries would both list the row and both send it.
            if not self.store.claim_pending(key):
                continue
            if self._send_claimed(alert):
                sent += 1
        if sent:
            LOGGER.info("re-delivered %d previously staged alert(s)", sent)
        return sent

    # --------------------------------------------------------------- official

    def _collect_official(
        self, official_sources: Iterable[object], counts: dict[str, object]
    ) -> dict[str, tuple[list[Evidence], tuple[str, ...]]]:
        """Snapshot each programme's official accounts, keyed by programme.

        The returned accounts tuple is the audit trail printed on the alert. It
        holds the real handles (@ycombinator), not a source label, and it is
        scoped per programme so one programme's announcements cannot be used to
        answer a question about the other.
        """
        collected: dict[str, tuple[list[Evidence], tuple[str, ...]]] = {}
        for source in official_sources:
            label = getattr(getattr(source, "name", None), "value", str(source))
            programme = getattr(source, "programme", None) or "YC"
            handles = tuple(getattr(source, "account_labels", (label,)))
            try:
                items = list(source.scan(self.store))
            except SourceError as error:
                LOGGER.warning("official source %s unavailable: %s", label, error.message)
                counts.setdefault("official_errors", []).append(
                    {"source": label, "programme": programme, "error": error.message}
                )
                continue
            except Exception as error:  # noqa: BLE001
                LOGGER.warning("official source %s raised: %s", label, error)
                counts.setdefault("official_errors", []).append(
                    {"source": label, "programme": programme, "error": str(error)}
                )
                continue
            snapshots, accounts = collected.get(programme, ([], ()))
            collected[programme] = (
                [*snapshots, *items],
                tuple(dict.fromkeys([*accounts, *handles])),
            )
        counts["official"] = {
            programme: {"snapshots": len(items), "accounts": list(accounts)}
            for programme, (items, accounts) in collected.items()
        }
        return collected


def health_report(store: Store, notifier: SlackNotifier) -> dict[str, object]:
    """Operational summary with no secrets in it."""
    try:
        slack = notifier.check()
    except Exception as error:  # noqa: BLE001
        slack = {"mode": "error", "detail": type(error).__name__}
    last = store.last_run() or {}
    return {
        "observations": store.observation_count(),
        "alerts": store.alert_counts(),
        "pending_alerts": len(store.pending_alerts()),
        "undelivered_alerts": store.undelivered_alerts(),
        "review_queue": len(store.review_items(limit=1000)),
        "sources": store.source_health(),
        "last_run": {
            "started_at": last.get("started_at"),
            "finished_at": last.get("finished_at"),
            "outcome": last.get("outcome"),
        },
        "slack": slack,
        "checked_at": utcnow().isoformat(),
    }
