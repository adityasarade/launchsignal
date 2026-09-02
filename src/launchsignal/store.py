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
import uuid
from datetime import timedelta
from pathlib import Path

from .models import Alert, Evidence, ReviewItem, SignalKind, utcnow

SCHEMA_VERSION = 3

#: Deliveries are retried this many times before the alert is reported as
#: undelivered rather than retried forever.
MAX_DELIVERY_ATTEMPTS = 5

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
  -- Tracked separately from existence. A record can be observed without having
  -- had its detail page fetched, and it must stay eligible for enrichment
  -- later: keying enrichment off "is new" means anything skipped by a
  -- per-cycle cap never gets a batch or a description at all.
  enriched      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (source, external_id)
);
CREATE INDEX IF NOT EXISTS observations_company ON observations(company_key);

-- Cross-process mutual exclusion. A `serve` loop and a Pond `run_scan` share
-- one database, and two simultaneous scans would each claim and deliver work.
CREATE TABLE IF NOT EXISTS locks (
  name       TEXT PRIMARY KEY,
  holder     TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

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


_MIGRATED_INDEXES = """
CREATE INDEX IF NOT EXISTS observations_enriched ON observations(source, enriched);
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
        # Order matters: tables, then additive column migrations, then any
        # index that depends on a migrated column. Creating that index first
        # fails outright on a database written by an earlier version.
        self.connection.executescript(_SCHEMA)
        self._migrate()
        self.connection.executescript(_MIGRATED_INDEXES)
        self.connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.connection.commit()

    def _migrate(self) -> None:
        """Additive migrations for databases created by an earlier version."""
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(observations)")
        }
        if "enriched" not in columns:
            self.connection.execute(
                "ALTER TABLE observations ADD COLUMN enriched INTEGER NOT NULL DEFAULT 0"
            )
        self.connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    # ------------------------------------------------------------------- locks

    def acquire_lock(self, name: str, *, ttl_seconds: int = 3600) -> str | None:
        """Take a named lock, or return None if someone else holds it.

        Expired locks are reclaimed so a killed process cannot wedge the
        monitor permanently.
        """
        holder = uuid.uuid4().hex
        now = utcnow()
        expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
        self.connection.execute(
            "DELETE FROM locks WHERE name = ? AND expires_at < ?", (name, now.isoformat())
        )
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO locks(name, holder, expires_at) VALUES(?, ?, ?)",
            (name, holder, expires),
        )
        self.connection.commit()
        return holder if cursor.rowcount == 1 else None

    def release_lock(self, name: str, holder: str) -> None:
        self.connection.execute(
            "DELETE FROM locks WHERE name = ? AND holder = ?", (name, holder)
        )
        self.connection.commit()

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

    def mark_enriched(self, source: str, external_id: str) -> None:
        self.connection.execute(
            "UPDATE observations SET enriched = 1 WHERE source = ? AND external_id = ?",
            (source, external_id),
        )
        self.connection.commit()

    def is_enriched(self, source: str, external_id: str) -> bool:
        row = self.connection.execute(
            "SELECT enriched FROM observations WHERE source = ? AND external_id = ?",
            (source, external_id),
        ).fetchone()
        return bool(row and row["enriched"])

    def unenriched_count(self, source: str) -> int:
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM observations WHERE source = ? AND enriched = 0",
                (source,),
            ).fetchone()[0]
        )

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

    def claim_alert(self, alert: Alert) -> bool:
        """Atomically claim the right to send this alert.

        The claim is the INSERT itself: exactly one caller can create the row,
        and that caller owns the send. Checking the state first and then sending
        is a race -- two sources in one scan, or two concurrent scans, both see
        a 'pending' or 'failed' row and both call Slack, so the same alert goes
        out twice.

        A row that already exists is left alone here. Retrying it is the
        dedicated `claim_pending` path, which leases it just as atomically.
        """
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO alerts
               (alert_key, company_key, kind, company_name, source, source_url,
                payload_json, state, attempts, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'sending', 1, ?)""",
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
        return cursor.rowcount == 1

    def claim_pending(self, alert_key: str) -> bool:
        """Lease a previously staged alert for one more delivery attempt.

        The conditional UPDATE is the lease: only the caller whose UPDATE
        actually changed a row may send, so concurrent retries cannot both fire.
        """
        cursor = self.connection.execute(
            """UPDATE alerts
               SET state = 'sending', attempts = attempts + 1
               WHERE alert_key = ? AND state IN ('pending', 'failed')""",
            (alert_key,),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def mark_alert_sent(self, key: str, slack_ts: str | None) -> None:
        self.connection.execute(
            """UPDATE alerts
               SET state = 'sent', sent_at = ?, slack_ts = ?, last_error = NULL
               WHERE alert_key = ?""",
            (utcnow().isoformat(), slack_ts, key),
        )
        self.connection.commit()

    def mark_alert_failed(self, key: str, error: str, *, terminal: bool = False) -> None:
        """Record a failed send.

        `terminal` marks it dead: a fatal Slack error such as channel_not_found
        or invalid_auth cannot be fixed by trying again, so retrying it on every
        scan just hides the real problem. Dead alerts stay visible in the health
        report instead of being silently abandoned once attempts run out.
        """
        self.connection.execute(
            """UPDATE alerts SET state = ?, last_error = ? WHERE alert_key = ?""",
            ("dead" if terminal else "failed", error[:500], key),
        )
        self.connection.commit()

    def pending_alerts(self, limit: int = 100) -> list[dict[str, object]]:
        """Alerts staged but never confirmed sent, oldest first.

        Retried at the start of the next scan so a Slack outage delays delivery
        instead of losing it. Anything left 'sending' by a crashed process is
        included, because nothing else will ever pick it up.
        """
        rows = self.connection.execute(
            """SELECT alert_key, payload_json, attempts FROM alerts
               WHERE state IN ('pending', 'failed', 'sending')
                 AND attempts < ?
               ORDER BY created_at LIMIT ?""",
            (MAX_DELIVERY_ATTEMPTS, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def undelivered_alerts(self, limit: int = 50) -> list[dict[str, object]]:
        """Alerts that will never be retried: dead, or out of attempts.

        Surfaced by `launchsignal health` so an operator sees a broken channel
        rather than a quietly emptying queue.
        """
        rows = self.connection.execute(
            """SELECT alert_key, company_name, kind, state, attempts, last_error
               FROM alerts
               WHERE state = 'dead' OR (state != 'sent' AND attempts >= ?)
               ORDER BY created_at DESC LIMIT ?""",
            (MAX_DELIVERY_ATTEMPTS, limit),
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
