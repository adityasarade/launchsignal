"""Slack delivery.

The card layout follows the reference alert in the task brief field for field:
Company, Founder, Batch, Source, Status, Original post, Post link, Detected.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

from .http import require_web_url
from .models import Alert, OfficialState, SignalKind

LOGGER = logging.getLogger("launchsignal.notify")

#: Constants, never user input. Both are https and validated before opening.
POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
AUTH_TEST_URL = "https://slack.com/api/auth.test"

#: Slack's chat.postMessage is roughly one message per second per channel.
#: A batch drop can add dozens of companies at once, so pace the sends rather
#: than discovering the limit as a mid-storm 429.
MIN_INTERVAL_SECONDS = 1.2

#: Errors where retrying cannot help. Anything else is treated as transient.
FATAL_SLACK_ERRORS = frozenset(
    {
        "invalid_auth",
        "account_inactive",
        "token_revoked",
        "token_expired",
        "not_authed",
        "channel_not_found",
        "not_in_channel",
        "is_archived",
        "invalid_blocks",
        "msg_too_long",
    }
)


class SlackConfigError(RuntimeError):
    """Slack is enabled but not usable. Message never contains the token."""


class SlackSendError(RuntimeError):
    def __init__(self, error_code: str, fatal: bool) -> None:
        super().__init__(f"Slack rejected the alert: {error_code}")
        self.error_code = error_code
        self.fatal = fatal


class SlackNotifier:
    def __init__(self, *, sleeper=time.sleep) -> None:
        self._last_send = 0.0
        self._sleeper = sleeper

    # ------------------------------------------------------------------ config

    @property
    def dry_run(self) -> bool:
        return os.environ.get("SLACK_DRY_RUN", "true").strip().lower() != "false"

    @staticmethod
    def _credentials() -> tuple[str, str]:
        token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
        channel = os.environ.get("SLACK_CHANNEL_ID", "").strip()
        if not token or not channel:
            raise SlackConfigError(
                "SLACK_BOT_TOKEN and SLACK_CHANNEL_ID must both be set when "
                "SLACK_DRY_RUN=false. Keep SLACK_DRY_RUN=true to run without Slack."
            )
        return token, channel

    def check(self) -> dict[str, str]:
        """Validate the credential without sending a message.

        Returns only non-secret identity fields, so the result is safe to print.
        """
        if self.dry_run:
            return {"mode": "dry-run"}
        token, channel = self._credentials()
        result = self._call(AUTH_TEST_URL, {}, token)
        return {
            "mode": "live",
            "team": str(result.get("team", "unknown")),
            "bot": str(result.get("user", "unknown")),
            "channel": f"...{channel[-4:]}" if len(channel) > 4 else "set",
        }

    # -------------------------------------------------------------------- send

    def send(self, alert: Alert) -> str | None:
        if self.dry_run:
            LOGGER.info(
                "[dry-run] %s | %s | %s", alert.kind.value, alert.company_name, alert.source_url
            )
            return "dry-run"
        token, channel = self._credentials()
        self._throttle()
        payload = {
            "channel": channel,
            "text": self._fallback_text(alert),
            "blocks": build_blocks(alert),
            "unfurl_links": False,
            "unfurl_media": False,
        }
        result = self._call(POST_MESSAGE_URL, payload, token)
        return str(result.get("ts")) if result.get("ts") else None

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_send
        if elapsed < MIN_INTERVAL_SECONDS:
            self._sleeper(MIN_INTERVAL_SECONDS - elapsed)
        self._last_send = time.monotonic()

    def _call(self, url: str, payload: dict, token: str, attempts: int = 3) -> dict:
        body = json.dumps(payload).encode()
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(  # noqa: S310 - url is a module constant
                url,
                data=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                method="POST",
            )
            try:
                require_web_url(url, "slack")
                with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                    result = json.loads(response.read().decode())
            except urllib.error.HTTPError as error:
                # 429 carries Retry-After. Never include the request headers in
                # a log line -- they contain the bearer token.
                if error.code == 429 and attempt < attempts:
                    delay = float(error.headers.get("Retry-After", "2") or 2)
                    LOGGER.warning("slack rate limited, waiting %.0fs", delay)
                    self._sleeper(min(delay, 60.0))
                    continue
                raise SlackSendError(f"http_{error.code}", fatal=error.code < 500) from None
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                if attempt < attempts:
                    self._sleeper(2.0 * attempt)
                    continue
                raise SlackSendError(f"network:{type(error).__name__}", fatal=False) from None

            if result.get("ok"):
                return result
            code = str(result.get("error", "unknown_error"))
            if code == "ratelimited" and attempt < attempts:
                self._sleeper(float(result.get("retry_after", 2) or 2))
                continue
            raise SlackSendError(code, fatal=code in FATAL_SLACK_ERRORS)
        raise SlackSendError("retries_exhausted", fatal=False)

    # ------------------------------------------------------------------ layout

    @staticmethod
    def _fallback_text(alert: Alert) -> str:
        return f"{headline(alert)} — {alert.company_name}"


def headline(alert: Alert) -> str:
    """The card's title.

    An early claim is only described as beating the official announcement when
    official accounts were actually checked. With no snapshot source configured
    the wording drops to an unverified founder claim, because "I checked
    nothing and found nothing" is not evidence that a programme has stayed
    quiet.
    """
    if alert.kind is SignalKind.EARLY_FOUNDER_CLAIM:
        if alert.official.state is OfficialState.NOT_SEEN:
            return "EARLY YC SIGNAL — Founder Announced Before YC"
        if alert.official.state is OfficialState.SEEN:
            return "FOUNDER POST — already announced officially"
        return "FOUNDER CLAIM — unverified (no official check configured)"
    if alert.kind is SignalKind.COMPANY_PAGE_FIRST_SEEN:
        return "LINKEDIN COMPANY PAGE — first observed"
    if alert.kind is SignalKind.AFFILIATION_MENTION:
        return f"{alert.programme.upper()} MENTION — affiliation only"
    return f"NEW {alert.programme.upper()} COMPANY"


def emoji(alert: Alert) -> str:
    if alert.kind is SignalKind.EARLY_FOUNDER_CLAIM:
        return "🚨" if alert.official.state is OfficialState.NOT_SEEN else "📣"
    if alert.kind is SignalKind.COMPANY_PAGE_FIRST_SEEN:
        return "🏢"
    if alert.kind is SignalKind.AFFILIATION_MENTION:
        return "🔎"
    return "✅"


def status_line(alert: Alert) -> str:
    if alert.kind is SignalKind.CONFIRMED:
        return f"Listed in the {alert.source_label} — membership is public record"
    if alert.kind is SignalKind.EARLY_FOUNDER_CLAIM:
        if alert.official.state is OfficialState.NOT_SEEN:
            return f"Founder-announced / not yet officially announced — {alert.official.describe()}"
        if alert.official.state is OfficialState.SEEN:
            return f"Founder-announced — {alert.official.describe()}"
        return f"Founder-announced — {alert.official.describe()}"
    if alert.kind is SignalKind.AFFILIATION_MENTION:
        return (
            "Post states a programme affiliation but not a new acceptance, so this "
            "is a weaker signal than an acceptance announcement"
        )
    return f"Company page first observed via public search — {alert.official.describe()}"


def build_blocks(alert: Alert) -> list[dict]:
    """Block Kit card mirroring the reference layout in the task brief."""
    fields = [("Company", alert.company_name)]
    founder = alert.founder
    if founder and alert.founder_handle:
        founder = f"{founder} ({alert.founder_handle})"
    elif not founder and alert.founder_handle:
        founder = alert.founder_handle
    if founder:
        fields.append(("Founder", founder))
    if alert.batch:
        fields.append(("Batch", alert.batch))
    fields.append(("Source", alert.source_label))

    detail = "\n".join(f"*{label}:*  {_escape(value)}" for label, value in fields)
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{emoji(alert)} {headline(alert)}", "emoji": True},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": detail}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Status:*  {_escape(status_line(alert))}"},
        },
    ]
    excerpt = (alert.excerpt or "").strip()
    if excerpt:
        clipped = excerpt if len(excerpt) <= 600 else excerpt[:597].rstrip() + "…"
        label = "Original post" if alert.kind is not SignalKind.CONFIRMED else "Description"
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{label}:*\n>{_escape(clipped)}"},
            }
        )
    links = [f"<{alert.source_url}|Open source>"] if alert.source_url else []
    if alert.profile_url and alert.profile_url != alert.source_url:
        links.append(f"<{alert.profile_url}|Programme profile>")
    if alert.official.matched_url:
        links.append(f"<{alert.official.matched_url}|Official post>")
    context = " · ".join(links) if links else "no link available"
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"{context}  |  Detected "
                        f"{alert.detected_at.strftime('%Y-%m-%d %H:%M UTC')}"
                    ),
                }
            ],
        }
    )
    return blocks


def _escape(value: str) -> str:
    """Slack mrkdwn escaping for the three characters that break a block."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
