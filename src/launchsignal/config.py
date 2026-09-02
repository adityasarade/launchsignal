"""Configuration.

The project has no third-party dependencies, so it carries its own tiny .env
reader. Without one the documented setup ("copy .env.example to .env, then
run") silently does nothing: every value is read from os.environ, so editing
.env has no effect and Slack stays in dry-run while appearing configured.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_LINE = re.compile(r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$""")


def load_env(path: str | os.PathLike[str] = ".env", *, override: bool = False) -> list[str]:
    """Load KEY=VALUE pairs from `path` into os.environ.

    Real environment variables win by default, so a deployment's injected
    secrets are never clobbered by a stale local file. Returns the names loaded
    (names only -- values are secrets and are never returned or logged).
    """
    file = Path(path)
    if not file.is_file():
        return []
    loaded: list[str] = []
    # Track what this file has set so far, separately from the real environment.
    # Using os.environ as the guard conflates the two, which makes a duplicate
    # key inside the file first-wins -- so appending a line to override an
    # earlier one silently does nothing.
    from_file: set[str] = set()
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if value and value[0] in "\"'" and value[-1] == value[0] and len(value) > 1:
            value = value[1:-1]
        else:
            value = value.split(" #")[0].rstrip()
        if key in os.environ and key not in from_file and not override:
            continue
        os.environ[key] = value
        from_file.add(key)
        if key not in loaded:
            loaded.append(key)
    return loaded


def database_path() -> str:
    return os.environ.get("LAUNCHSIGNAL_DB_PATH", "data/launchsignal.sqlite3")


def interval_minutes(default: int = 480) -> int:
    try:
        return max(1, int(os.environ.get("LAUNCHSIGNAL_INTERVAL_MINUTES", default)))
    except ValueError:
        return default


def fast_lane_minutes(default: int = 45) -> int:
    try:
        return max(0, int(os.environ.get("LAUNCHSIGNAL_FAST_LANE_MINUTES", default)))
    except ValueError:
        return default
