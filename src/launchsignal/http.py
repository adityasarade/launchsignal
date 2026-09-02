"""A small, deliberate HTTP client.

Every network call in a monitor that runs unattended for weeks needs four
things the standard library does not give you for free: a timeout, bounded
retries with backoff, a way to tell "not modified" from "empty", and errors
that name the source that failed. That is all this module is.
"""

from __future__ import annotations

import gzip
import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

LOGGER = logging.getLogger("launchsignal.http")

USER_AGENT = (
    "LaunchSignal/1.0 (+https://github.com/adityasarade/launchsignal; "
    "public-source launch monitor)"
)

#: Retried: transient upstream problems and rate limits.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
#: Not retried, and not an error either -- the caller asked with an ETag.
NOT_MODIFIED = 304


class SourceError(RuntimeError):
    """A source could not be read. Carries the source name for reporting."""

    def __init__(self, source: str, message: str) -> None:
        super().__init__(f"{source}: {message}")
        self.source = source
        self.message = message


#: Only these schemes may be opened. Endpoints are configurable, so a stray
#: file:// or custom scheme must be refused rather than fetched.
ALLOWED_SCHEMES = frozenset({"http", "https"})


def require_web_url(url: str, source: str = "http") -> str:
    """Reject anything that is not an ordinary web URL."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise SourceError(source, f"refusing to open a non-web URL scheme: {parsed.scheme!r}")
    if not parsed.netloc:
        raise SourceError(source, "refusing to open a URL with no host")
    return url


class NotModified(Exception):  # noqa: N818 - a control-flow signal, not an error
    """The upstream resource is unchanged since the stored validator."""


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: dict[str, str]

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> object:
        return json.loads(self.text)

    @property
    def etag(self) -> str | None:
        return self.headers.get("ETag") or self.headers.get("Etag")


def request(
    url: str,
    *,
    source: str = "http",
    headers: dict[str, str] | None = None,
    etag: str | None = None,
    timeout: float = 30.0,
    attempts: int = 4,
    backoff: float = 1.5,
    sleeper=time.sleep,
) -> Response:
    """GET `url`, retrying transient failures with jittered backoff.

    Raises NotModified when an ETag was supplied and the resource is unchanged,
    and SourceError when every attempt failed.
    """
    merged = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
        **(headers or {}),
    }
    if etag:
        merged["If-None-Match"] = etag

    require_web_url(url, source)
    last_error = "unknown error"
    for attempt in range(1, attempts + 1):
        try:
            # Scheme is validated by require_web_url before the loop, so only
            # http/https can reach here.
            req = urllib.request.Request(url, headers=merged, method="GET")  # noqa: S310
            with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310
                raw = response.read()
                head = {key: value for key, value in response.headers.items()}
                if head.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                return Response(status=response.status, body=raw, headers=head)
        except urllib.error.HTTPError as error:
            if error.code == NOT_MODIFIED:
                raise NotModified(url) from None
            last_error = f"HTTP {error.code}"
            if error.code not in RETRY_STATUS:
                raise SourceError(source, f"{last_error} for {_safe(url)}") from None
            retry_after = _retry_after(error.headers)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = f"{type(error).__name__}: {error}"
            retry_after = None
        except json.JSONDecodeError as error:  # pragma: no cover - defensive
            raise SourceError(source, f"invalid JSON from {_safe(url)}: {error}") from None

        if attempt == attempts:
            break
        delay = retry_after if retry_after is not None else backoff ** attempt
        delay += random.uniform(0, 0.4)  # noqa: S311 - jitter, not a secret
        LOGGER.warning(
            "%s: %s (attempt %d/%d), retrying in %.1fs", source, last_error, attempt, attempts, delay
        )
        sleeper(min(delay, 60.0))

    raise SourceError(source, f"{last_error} after {attempts} attempts for {_safe(url)}")


def get_json(url: str, **kwargs) -> tuple[object, Response]:
    response = request(url, **kwargs)
    try:
        return response.json(), response
    except json.JSONDecodeError as error:
        raise SourceError(
            kwargs.get("source", "http"), f"invalid JSON from {_safe(url)}: {error}"
        ) from None


def get_text(url: str, **kwargs) -> tuple[str, Response]:
    response = request(url, **kwargs)
    return response.text, response


def _retry_after(headers) -> float | None:
    value = headers.get("Retry-After") if headers else None
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe(url: str) -> str:
    """Strip the query string so a key in a query parameter never reaches a log."""
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
