"""Pond Protocol V1 control plane.

Exposes the monitor as a Pond-compatible agent:

    GET  /manifest      public; declares actions and limits
    POST /runs          authenticated; executes an action
    GET  /tasks/{id}    authenticated; polls an asynchronous run

Built on http.server so the project keeps its zero-dependency promise. The
access key is read from POND_ACCESS_KEY and compared in constant time; it is
never logged, echoed, or included in an error body.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .config import database_path
from .notify import SlackNotifier
from .service import health_report
from .store import Store

LOGGER = logging.getLogger("launchsignal.pond")

MAX_REQUEST_BYTES = 64 * 1024
MAX_EXECUTION_SECONDS = 900

MANIFEST: dict[str, Any] = {
    "protocol": "pond/v1",
    "name": "launchsignal",
    "display_name": "LaunchSignal — YC & Speedrun Launch Monitor",
    "description": (
        "Monitors the YC company directory, the a16z Speedrun directory, and "
        "public X/LinkedIn search for founder launch announcements, then posts "
        "deduplicated alerts to Slack. Flags founder posts that appear before "
        "the programme's own official announcement."
    ),
    "version": __version__,
    "category": "monitoring",
    "capabilities": {"sync": True, "async": True, "streaming": False},
    "limits": {
        "max_request_bytes": MAX_REQUEST_BYTES,
        "max_execution_seconds": MAX_EXECUTION_SECONDS,
    },
    "actions": [
        {
            "name": "get_health",
            "description": "Per-source availability, last successful scan, "
            "alert and review-queue depth. Contains no secrets.",
            "mode": "sync",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "output_schema": {"type": "object"},
        },
        {
            "name": "run_scan",
            "description": "Run one monitoring cycle now. 'fast' restricts the "
            "run to the recent-founder-post lane.",
            "mode": "async",
            "input_schema": {
                "type": "object",
                "properties": {"fast": {"type": "boolean", "default": False}},
                "additionalProperties": False,
            },
            "output_schema": {"type": "object"},
        },
        {
            "name": "review_signal",
            "description": "List candidates the classifier refused to resolve, "
            "with the reason each was held back.",
            "mode": "sync",
            "input_schema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}},
                "additionalProperties": False,
            },
            "output_schema": {"type": "object"},
        },
    ],
}


class TaskRegistry:
    """In-memory async task table with run-id idempotency."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._by_run_id: dict[str, str] = {}

    def existing(self, run_id: str | None) -> dict[str, Any] | None:
        if not run_id:
            return None
        with self._lock:
            task_id = self._by_run_id.get(run_id)
            return dict(self._tasks[task_id]) if task_id else None

    def create(self, action: str, run_id: str | None) -> dict[str, Any]:
        task_id = uuid.uuid4().hex
        record = {"task_id": task_id, "action": action, "status": "running", "result": None}
        with self._lock:
            self._tasks[task_id] = record
            if run_id:
                self._by_run_id[run_id] = task_id
        return dict(record)

    def finish(self, task_id: str, status: str, result: Any) -> None:
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].update(status=status, result=result)

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._tasks.get(task_id)
            return dict(record) if record else None


REGISTRY = TaskRegistry()


def _access_key() -> str | None:
    return os.environ.get("POND_ACCESS_KEY") or None


def _authorised(header: str | None) -> bool:
    expected = _access_key()
    if not expected:
        # Fail closed. An unset key must not mean "open to everyone".
        return False
    if not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:].strip(), expected)


def run_action(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one action. Imported lazily so the CLI stays independent."""
    from .cli import scan_once

    store = Store(database_path())
    try:
        notifier = SlackNotifier()
        if action == "get_health":
            return health_report(store, notifier)
        if action == "run_scan":
            return scan_once(store, notifier, fast=bool(payload.get("fast")))
        if action == "review_signal":
            limit = int(payload.get("limit") or 50)
            return {"items": store.review_items(limit=max(1, min(limit, 200)))}
        raise ValueError(f"unknown action: {action}")
    finally:
        store.close()


class Handler(BaseHTTPRequestHandler):
    server_version = f"LaunchSignal/{__version__}"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        LOGGER.info("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------ replies

    def _send(self, status: HTTPStatus | int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, default=str).encode()
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("X-Pond-Protocol", "v1")
        self.end_headers()
        self.wfile.write(encoded)

    def _error(self, status: HTTPStatus | int, code: str, message: str) -> None:
        self._send(status, {"error": {"code": code, "message": message}})

    # -------------------------------------------------------------------- verbs

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/manifest":
            self._send(HTTPStatus.OK, MANIFEST)
            return
        if path in {"/healthz", "/readyz"}:
            self._send(HTTPStatus.OK, {"status": "ok", "version": __version__})
            return
        if path.startswith("/tasks/"):
            if not _authorised(self.headers.get("Authorization")):
                self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", "A valid Pond access key is required.")
                return
            task = REGISTRY.get(path.rsplit("/", 1)[-1])
            if task is None:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "No such task.")
                return
            self._send(HTTPStatus.OK, task)
            return
        self._error(HTTPStatus.NOT_FOUND, "not_found", "Unknown endpoint.")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path != "/runs":
            self._error(HTTPStatus.NOT_FOUND, "not_found", "Unknown endpoint.")
            return
        if not _authorised(self.headers.get("Authorization")):
            self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", "A valid Pond access key is required.")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "bad_request", "Invalid Content-Length.")
            return
        if length > MAX_REQUEST_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large",
                        f"Request exceeds {MAX_REQUEST_BYTES} bytes.")
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._error(HTTPStatus.BAD_REQUEST, "bad_request", "Body must be JSON.")
            return
        if not isinstance(body, dict):
            self._error(HTTPStatus.BAD_REQUEST, "bad_request", "Body must be a JSON object.")
            return

        action = str(body.get("action") or "")
        payload = body.get("input") if isinstance(body.get("input"), dict) else {}
        run_id = body.get("run_id")
        known = {entry["name"]: entry for entry in MANIFEST["actions"]}
        if action not in known:
            self._error(HTTPStatus.BAD_REQUEST, "unknown_action", f"Unknown action: {action or '(none)'}")
            return

        # Idempotency: the same run_id returns the original task, never a rerun.
        duplicate = REGISTRY.existing(run_id if isinstance(run_id, str) else None)
        if duplicate is not None:
            self._send(HTTPStatus.OK, duplicate)
            return

        if known[action]["mode"] == "sync":
            try:
                self._send(HTTPStatus.OK, {"status": "succeeded", "action": action,
                                           "result": run_action(action, payload)})
            except Exception as error:  # noqa: BLE001
                LOGGER.exception("action %s failed", action)
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "action_failed", type(error).__name__)
            return

        task = REGISTRY.create(action, run_id if isinstance(run_id, str) else None)
        threading.Thread(
            target=_execute, args=(task["task_id"], action, payload), daemon=True
        ).start()
        self._send(HTTPStatus.ACCEPTED, task)


def _execute(task_id: str, action: str, payload: dict[str, Any]) -> None:
    try:
        REGISTRY.finish(task_id, "succeeded", run_action(action, payload))
    except Exception as error:  # noqa: BLE001
        LOGGER.exception("async action %s failed", action)
        REGISTRY.finish(task_id, "failed", {"error": type(error).__name__})


def serve(host: str = "0.0.0.0", port: int = 8080) -> None:
    if not _access_key():
        LOGGER.warning(
            "POND_ACCESS_KEY is not set: /manifest and /healthz stay public, but "
            "/runs and /tasks will reject every request until a key is configured."
        )
    server = ThreadingHTTPServer((host, port), Handler)
    LOGGER.info("pond control plane listening on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
