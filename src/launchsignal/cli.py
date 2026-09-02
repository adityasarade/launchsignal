"""Command line entry point.

    launchsignal doctor    validate setup without sending anything
    launchsignal scan      one full cycle over every source
    launchsignal fast      founder-claim lane only (recent social posts)
    launchsignal serve     run scan on a schedule, plus the fast lane
    launchsignal health    operational report
    launchsignal review    show items that could not be resolved
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import sys
import time

from .config import database_path, fast_lane_minutes, interval_minutes, load_env
from .notify import SlackConfigError, SlackNotifier
from .service import Monitor, health_report
from .sources import (
    directory_sources,
    official_sources,
    social_sources,
)
from .store import Store

LOGGER = logging.getLogger("launchsignal")

_STOP = False


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _handle_stop(signum, _frame) -> None:
    global _STOP
    _STOP = True
    LOGGER.info("received signal %s; finishing the current cycle then exiting", signum)


def scan_once(store: Store, notifier: SlackNotifier, *, fast: bool = False) -> dict:
    """One cycle.

    `fast` runs only the social lane with a freshness window, which is the
    cheap path that makes early detection credible between full scans.
    """
    if not fast:
        sources = [*directory_sources(), *social_sources()]
        return Monitor(store, notifier).run(sources, official_sources())

    window = max(fast_lane_minutes() * 3, 90)
    candidates = social_sources(recency_minutes=window)
    # A source's first scan is a silent baseline. If the fast lane were allowed
    # to perform it, a fresh install would baseline the social sources against a
    # recency-filtered query -- silently swallowing exactly the fresh founder
    # posts this lane exists to catch. Baselining is the full scan's job.
    ready = [s for s in candidates if store.baseline_complete(s.name.value)]
    skipped = [s.name.value for s in candidates if s not in ready]
    if skipped:
        LOGGER.info(
            "fast lane skipping %s until the full scan has established a baseline",
            ", ".join(skipped),
        )
    if not ready:
        return {
            "observations": 0, "new": 0, "alerts": 0, "review": 0, "retried": 0,
            "baselined_sources": [], "sources": [], "outcome": "ok",
            "skipped_pending_baseline": skipped,
        }
    result = Monitor(store, notifier).run(ready, official_sources())
    result["skipped_pending_baseline"] = skipped
    return result


def _locked_scan(store: Store, notifier: SlackNotifier, *, fast: bool) -> dict:
    """Run a scan under the shared lock.

    The scheduler and the Pond control plane can both be pointed at one
    database. Without a shared lock two scans overlap, each claiming and
    delivering alerts with its own Slack throttle.
    """
    from .pond import SCAN_LOCK

    holder = store.acquire_lock(SCAN_LOCK, ttl_seconds=3600)
    if holder is None:
        LOGGER.info("skipping: another scan is already running")
        return {"outcome": "skipped", "reason": "another scan is already running"}
    try:
        return scan_once(store, notifier, fast=fast)
    finally:
        store.release_lock(SCAN_LOCK, holder)


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, default=str))


def cmd_doctor(args) -> int:
    """Check everything a first-time user can get wrong, without side effects."""
    problems: list[str] = []
    notes: list[str] = []

    if sys.version_info < (3, 12):  # noqa: UP036 - a guard for source checkouts
        problems.append(f"Python 3.12+ required, found {sys.version.split()[0]}")
    else:
        notes.append(f"Python {sys.version.split()[0]}")

    env_loaded = load_env(args.env_file)
    notes.append(
        f".env: loaded {len(env_loaded)} setting(s) from {args.env_file}"
        if env_loaded
        else f".env: no file at {args.env_file} (using process environment)"
    )

    store = Store(database_path())
    notes.append(f"database: {database_path()} ({store.observation_count()} observations)")

    notifier = SlackNotifier()
    if notifier.dry_run:
        notes.append("Slack: DRY RUN — no message will be sent (set SLACK_DRY_RUN=false to enable)")
    else:
        try:
            identity = notifier.check()
            notes.append(
                f"Slack: live as bot '{identity['bot']}' in team '{identity['team']}', "
                f"channel {identity['channel']}"
            )
        except SlackConfigError as error:
            problems.append(f"Slack: {error}")
        except Exception as error:  # noqa: BLE001
            problems.append(f"Slack: credential check failed ({type(error).__name__})")

    if os.environ.get("TINYFISH_API_KEY"):
        notes.append("Tinyfish: key present — X and LinkedIn discovery enabled")
    else:
        problems.append(
            "Tinyfish: TINYFISH_API_KEY is not set. X, LinkedIn and official-account "
            "snapshots CANNOT run, and early claims will be reported as unverified. "
            "Get a free key at https://tinyfish.ai and add it to .env"
        )

    for line in notes:
        print(f"  ok   {line}")
    for line in problems:
        print(f"  !!   {line}")
    store.close()
    print()
    if problems:
        print(f"{len(problems)} problem(s) found. The monitor will still run, but "
              "sources listed above will be skipped.")
        return 1
    print("All checks passed.")
    return 0


def cmd_scan(args) -> int:
    load_env(args.env_file)
    store = Store(database_path())
    try:
        result = scan_once(store, SlackNotifier(), fast=args.fast)
        _print(result)
        return 0 if result.get("outcome") == "ok" else 2
    finally:
        store.close()


def cmd_serve(args) -> int:
    load_env(args.env_file)
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)
    full_interval = args.interval_minutes or interval_minutes()
    fast_interval = fast_lane_minutes()
    store = Store(database_path())
    notifier = SlackNotifier()
    LOGGER.info(
        "serving: full scan every %d min, founder fast lane every %s",
        full_interval,
        f"{fast_interval} min" if fast_interval else "never (disabled)",
    )
    next_full = 0.0
    next_fast = 0.0
    try:
        while not _STOP:
            now = time.monotonic()
            try:
                if now >= next_full:
                    LOGGER.info("full scan starting")
                    LOGGER.info("full scan: %s", json.dumps(
                        _locked_scan(store, notifier, fast=False), default=str))
                    next_full = time.monotonic() + full_interval * 60
                elif fast_interval and now >= next_fast:
                    LOGGER.info("fast lane: %s", json.dumps(
                        _locked_scan(store, notifier, fast=True), default=str))
                    next_fast = time.monotonic() + fast_interval * 60
            except SlackConfigError as error:
                # Misconfiguration is not transient; stop rather than spin.
                LOGGER.error("stopping: %s", error)
                return 1
            except Exception:
                # A crash inside one cycle must never end the daemon. The old
                # loop had no guard, so a single 503 killed the monitor.
                LOGGER.exception("cycle failed; continuing to the next one")
                next_full = max(next_full, time.monotonic() + 60)
            # Short sleeps so a signal is noticed promptly, with jitter so many
            # deployments do not hit the same source in lockstep.
            time.sleep(min(30.0, 5.0 + random.uniform(0, 5)))  # noqa: S311 - jitter
    finally:
        store.close()
    LOGGER.info("stopped cleanly")
    return 0


def cmd_health(args) -> int:
    load_env(args.env_file)
    store = Store(database_path())
    try:
        _print(health_report(store, SlackNotifier()))
        return 0
    finally:
        store.close()


def cmd_pond(args) -> int:
    load_env(args.env_file)
    from .pond import serve as pond_serve

    pond_serve(args.host, args.port)
    return 0


def cmd_review(args) -> int:
    load_env(args.env_file)
    store = Store(database_path())
    try:
        items = store.review_items(limit=args.limit)
        if not items:
            print("Review queue is empty.")
            return 0
        for item in items:
            print(f"[{item['source']}] {item['reason']}  {item['url']}")
            print(f"    {str(item['excerpt'])[:200]}")
        return 0
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="launchsignal",
        description="Stateful public-source YC / a16z Speedrun launch monitor for Slack.",
    )
    parser.add_argument("--env-file", default=".env", help="path to the .env file")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="validate setup; sends nothing")
    doctor.set_defaults(func=cmd_doctor)

    scan = sub.add_parser("scan", help="run one scan cycle")
    scan.add_argument("--fast", action="store_true", help="founder social lane only")
    scan.set_defaults(func=cmd_scan)

    fast = sub.add_parser("fast", help="run the founder fast lane once")
    fast.set_defaults(func=cmd_scan, fast=True)

    serve = sub.add_parser("serve", help="run on a schedule until stopped")
    serve.add_argument("--interval-minutes", type=int, default=None)
    serve.set_defaults(func=cmd_serve)

    health = sub.add_parser("health", help="print an operational report")
    health.set_defaults(func=cmd_health)

    pond = sub.add_parser("pond", help="serve the Pond Protocol V1 control plane")
    # Containers need every interface; restrict it at the network layer.
    pond.add_argument("--host", default="0.0.0.0")  # noqa: S104
    pond.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    pond.set_defaults(func=cmd_pond)

    review = sub.add_parser("review", help="show unresolved candidates")
    review.add_argument("--limit", type=int, default=50)
    review.set_defaults(func=cmd_review)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "fast"):
        args.fast = False
    _configure_logging(args.verbose)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
