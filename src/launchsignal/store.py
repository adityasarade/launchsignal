"""Durable state.

Three ideas carry the whole design:

1. `observations` is an append-only log keyed by (source, external_id). It
   answers "have I seen this exact item before?".
2. `company_key` clusters every source's view of one company, so an X post and
   a LinkedIn post about the same company are one thing, not two.
3. `alert_key` is `company_key|kind`. A directory confirmation and an early
   founder claim about the same company are *different kinds of news*, so each
   gets its own delivery slot. Keying alerts on the company alone means
   whichever source is scanned first silently consumes the other's alert.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .models import Alert, Evidence, ReviewItem, SignalKind, utcnow

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS observations (
  source        TEXT NOT NULL,
  external_id   TEXT NOT NULL,
  company_key   TEXT NOT NULL,
  url           TEXT NOT NULL,
  title         TEXT NOT NULL,
  excerpt       TEXT NOT NULL,
  observed_at   TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  PRIMARY KEY (source, external_id)
);
CREATE INDEX IF NOT EXISTS observations_company ON observations(company_key);

-- An alert row is written BEFORE the Slack call, in state 'pending'. If the
-- send fails or the process dies, the row survives and the next scan retries
-- it. Without this, the observation commits, the send fails, and the item can
-- never alert again because it is no longer "new".
CREATE TABLE IF NOT EXISTS alerts (
  alert_key    TEXT PRIMARY KEY,
  company_key  TEXT NOT NULL,
  kind         TEXT NOT NULL,
  company_name TEXT NOT NULL,
  source       TEXT NOT NULL,
  source_url   TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  state        TEXT NOT NULL DEFAULT 'pending',
  attempts     INTEGER NOT NULL DEFAULT 0,
  last_error   TEXT,
  created_at   TEXT NOT NULL,
  sent_at      TEXT,
  slack_ts     TEXT
);
CREATE INDEX IF NOT EXISTS alerts_state ON alerts(state);

-- Baseline is tracked per source, not globally. A source that fails on the
-- first run must not inherit another source's "baseline done" flag, and must
-- not force every other source through a second silent baseline.
CREATE TABLE IF NOT EXISTS source_state (
  source            TEXT PRIMARY KEY,
  baseline_complete INTEGER NOT NULL DEFAULT 0,
  etag              TEXT,
  last_success_at   TEXT,
  last_error        TEXT,
  consecutive_fails INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS review_queue (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source      TEXT NOT NULL,
  url         TEXT NOT NULL,
  excerpt     TEXT NOT NULL,
  reason      TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  resolved    INTEGER NOT NULL DEFAULT 0,
  UNIQUE(source, url, reason)
);

CREATE TABLE IF NOT EXISTS scan_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  counts_json TEXT NOT NULL DEFAULT '{}',
  outcome     TEXT
);
"""


def company_key(company_name: str, programme: str) -> str:
    """Stable cluster id for one company within one programme."""
    from .classifier import canonical_name

    raw = f"{programme.strip().lower()}|{canonical_name(company_name)}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def alert_key(company_key_value: str, kind: SignalKind) -> str:
    return f"{company_key_value}|{kind.value}"


class Store:
    def __init__(self, database_path: str) -> None:
        path = Path(database_path)
        if path.parent != Path(""):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(_SCHEMA)
        self.connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    # ---------------------------------------------------------------- sources

    def baseline_complete(self, source: str) -> bool:
        row = self.connection.execute(
            "SELECT baseline_complete FROM source_state WHERE source = ?", (source,)
        ).fetchone()
        return bool(row and row["baseline_complete"])

    def complete_baseline(self, source: str) -> None:
        self.connection.execute(
            """INSERT INTO source_state(source, baseline_complete)
               VALUES(?, 1)
               ON CONFLICT(source) DO UPDATE SET baseline_complete = 1""",
            (source,),
        )
        self.connection.commit()

    def get_etag(self, source: str) -> str | None:
        row = self.connection.execute(
            "SELECT etag FROM source_state WHERE source = ?", (source,)
        ).fetchone()
        return row["etag"] if row else None

    def set_etag(self, source: str, etag: str | None) -> None:
        if not etag:
            return
        self.connection.execute(
            """INSERT INTO source_state(source, etag) VALUES(?, ?)
               ON CONFLICT(source) DO UPDATE SET etag = excluded.etag""",
            (source, etag),
        )
        self.connection.commit()

    def record_source_success(self, source: str) -> None:
        self.connection.execute(
            """INSERT INTO source_state(source, last_success_at, last_error, consecutive_fails)
               VALUES(?, ?, NULL, 0)
               ON CONFLICT(source) DO UPDATE SET
                 last_success_at = excluded.last_success_at,
                 last_error = NULL,
                 consecutive_fails = 0""",
            (source, utcnow().isoformat()),
        )
        self.connection.commit()

    def record_source_failure(self, source: str, error: str) -> None:
        self.connection.execute(
            """INSERT INTO source_state(source, last_error, consecutive_fails)
               VALUES(?, ?, 1)
               ON CONFLICT(source) DO UPDATE SET
                 last_error = excluded.last_error,
                 consecutive_fails = source_state.consecutive_fails + 1""",
            (source, error[:500]),
        )
        self.connection.commit()

    def source_health(self) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """SELECT source, baseline_complete, last_success_at, last_error,
                      consecutive_fails
               FROM source_state ORDER BY source"""
        ).fetchall()
        return [dict(row) for row in rows]

    # ----------------------------------------------------------- observations

    def record_observation(self, evidence: Evidence, company_key_value: str) -> bool:
        """Insert an observation. Returns True only if it had not been seen."""
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO observations
               (source, external_id, company_key, url, title, excerpt,
                observed_at, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                evidence.source.value,
                evidence.external_id,
                company_key_value,
                evidence.url,
                evidence.title,
                evidence.excerpt,
                evidence.observed_at.isoformat(),
                json.dumps(evidence.metadata, sort_keys=True, default=str),
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def observation_count(self) -> int:
        return int(
            self.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        )

    # ------------------------------------------------------------- alert flow

    def alert_state(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT state FROM alerts WHERE alert_key = ?", (key,)
        ).fetchone()
        return row["state"] if row else None

    def stage_alert(self, alert: Alert) -> bool:
        """Reserve a delivery slot before calling Slack.

        Returns True if this caller now owns the send. False means the alert was
        already delivered (or is already staged by this run).
        """
        existing = self.alert_state(alert.alert_key)
        if existing == "sent":
            return False
        if existing is None:
            self.connection.execute(
                """INSERT INTO alerts
                   (alert_key, company_key, kind, company_name, source, source_url,
                    payload_json, state, attempts, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)""",
                (
                    alert.alert_key,
                    alert.company_key,
                    alert.kind.value,
                    alert.company_name,
                    alert.source.value,
                    alert.source_url,
                    json.dumps(_alert_payload(alert), default=str),
                    utcnow().isoformat(),
                ),
            )
        self.connection.commit()
        return True

    def mark_alert_sent(self, key: str, slack_ts: str | None) -> None:
        self.connection.execute(
            """UPDATE alerts
               SET state = 'sent', sent_at = ?, slack_ts = ?, last_error = NULL,
                   attempts = attempts + 1
               WHERE alert_key = ?""",
            (utcnow().isoformat(), slack_ts, key),
        )
        self.connection.commit()

    def mark_alert_failed(self, key: str, error: str) -> None:
        self.connection.execute(
            """UPDATE alerts
               SET state = 'failed', last_error = ?, attempts = attempts + 1
               WHERE alert_key = ?""",
            (error[:500], key),
        )
        self.connection.commit()

    def pending_alerts(self, limit: int = 100) -> list[dict[str, object]]:
        """Alerts staged but never confirmed sent, oldest first.

        Retried at the start of the next scan so a Slack outage delays delivery
        instead of losing it.
        """
        rows = self.connection.execute(
            """SELECT alert_key, payload_json, attempts FROM alerts
               WHERE state IN ('pending', 'failed') AND attempts < 5
               ORDER BY created_at LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def alert_counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT state, COUNT(*) AS n FROM alerts GROUP BY state"
        ).fetchall()
        return {row["state"]: int(row["n"]) for row in rows}

    # ------------------------------------------------------------- review etc

    def add_review(self, item: ReviewItem) -> None:
        self.connection.execute(
            """INSERT OR IGNORE INTO review_queue
               (source, url, excerpt, reason, observed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                item.source.value,
                item.url,
                item.excerpt[:1000],
                item.reason,
                item.observed_at.isoformat(),
            ),
        )
        self.connection.commit()

    def review_items(self, limit: int = 50) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """SELECT id, source, url, excerpt, reason, observed_at
               FROM review_queue WHERE resolved = 0
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def start_run(self) -> int:
        cursor = self.connection.execute(
            "INSERT INTO scan_runs(started_at) VALUES(?)", (utcnow().isoformat(),)
        )
        self.connection.commit()
        return int(cursor.lastrowid or 0)

    def finish_run(self, run_id: int, counts: dict[str, object], outcome: str) -> None:
        self.connection.execute(
            "UPDATE scan_runs SET finished_at = ?, counts_json = ?, outcome = ? WHERE id = ?",
            (utcnow().isoformat(), json.dumps(counts, default=str), outcome, run_id),
        )
        self.connection.commit()

    def last_run(self) -> dict[str, object] | None:
        row = self.connection.execute(
            "SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def _alert_payload(alert: Alert) -> dict[str, object]:
    return {
        "alert_key": alert.alert_key,
        "company_key": alert.company_key,
        "kind": alert.kind.value,
        "company_name": alert.company_name,
        "programme": alert.programme,
        "source": alert.source.value,
        "source_url": alert.source_url,
        "excerpt": alert.excerpt,
        "batch": alert.batch,
        "founder": alert.founder,
        "founder_handle": alert.founder_handle,
        "profile_url": alert.profile_url,
        "detected_at": alert.detected_at.isoformat(),
        "official": {
            "state": alert.official.state.value,
            "accounts_checked": list(alert.official.accounts_checked),
            "snapshots_seen": alert.official.snapshots_seen,
            "checked_at": alert.official.checked_at.isoformat()
            if alert.official.checked_at
            else None,
            "matched_url": alert.official.matched_url,
        },
    }


def alert_from_payload(payload: dict[str, object]) -> Alert:
    """Rebuild an Alert for retrying a staged delivery."""
    from datetime import datetime

    from .models import OfficialCheck, OfficialState, Source

    official = payload.get("official") or {}
    checked_at = official.get("checked_at")
    return Alert(
        alert_key=str(payload["alert_key"]),
        company_key=str(payload["company_key"]),
        kind=SignalKind(str(payload["kind"])),
        company_name=str(payload["company_name"]),
        programme=str(payload["programme"]),
        source=Source(str(payload["source"])),
        source_url=str(payload["source_url"]),
        excerpt=str(payload.get("excerpt") or ""),
        official=OfficialCheck(
            state=OfficialState(str(official.get("state", "not_checked"))),
            accounts_checked=tuple(official.get("accounts_checked") or ()),
            snapshots_seen=int(official.get("snapshots_seen") or 0),
            checked_at=datetime.fromisoformat(str(checked_at)) if checked_at else None,
            matched_url=official.get("matched_url"),
        ),
        batch=payload.get("batch"),
        founder=payload.get("founder"),
        founder_handle=payload.get("founder_handle"),
        profile_url=payload.get("profile_url"),
        detected_at=datetime.fromisoformat(str(payload["detected_at"])),
    )
