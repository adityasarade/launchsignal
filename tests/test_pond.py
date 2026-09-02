"""Pond Protocol V1 contract, exercised over a real HTTP socket."""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from launchsignal import pond

KEY = "test-access-key-not-a-real-secret"


class PondContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._dir = tempfile.TemporaryDirectory()
        cls._previous = {
            "POND_ACCESS_KEY": os.environ.get("POND_ACCESS_KEY"),
            "LAUNCHSIGNAL_DB_PATH": os.environ.get("LAUNCHSIGNAL_DB_PATH"),
            "SLACK_DRY_RUN": os.environ.get("SLACK_DRY_RUN"),
        }
        os.environ["POND_ACCESS_KEY"] = KEY
        os.environ["LAUNCHSIGNAL_DB_PATH"] = f"{cls._dir.name}/pond.sqlite3"
        os.environ["SLACK_DRY_RUN"] = "true"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), pond.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls._dir.cleanup()
        for key, value in cls._previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def call(self, path, *, method="GET", body=None, key=KEY):
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Authorization"] = f"Bearer {key}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            payload = json.loads(error.read().decode() or "{}")
            error.close()
            return error.code, payload

    # -------------------------------------------------------------- manifest

    def test_manifest_is_public(self) -> None:
        status, body = self.call("/manifest", key=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["protocol"], "pond/v1")
        self.assertEqual(body["name"], "launchsignal")

    def test_manifest_declares_actions_and_limits(self) -> None:
        _, body = self.call("/manifest", key=None)
        names = {action["name"] for action in body["actions"]}
        self.assertEqual(names, {"get_health", "run_scan", "review_signal"})
        self.assertIn("max_request_bytes", body["limits"])
        self.assertIn("max_execution_seconds", body["limits"])
        for action in body["actions"]:
            self.assertIn("input_schema", action)
            self.assertIn("output_schema", action)
            self.assertIn(action["mode"], {"sync", "async"})

    def test_healthz_is_public(self) -> None:
        status, body = self.call("/healthz", key=None)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    # ------------------------------------------------------------------ auth

    def test_runs_rejects_a_missing_key(self) -> None:
        status, body = self.call("/runs", method="POST", body={"action": "get_health"}, key=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_runs_rejects_a_wrong_key(self) -> None:
        status, _ = self.call("/runs", method="POST", body={"action": "get_health"}, key="nope")
        self.assertEqual(status, 401)

    def test_tasks_requires_auth(self) -> None:
        status, _ = self.call("/tasks/anything", key=None)
        self.assertEqual(status, 401)

    def test_error_body_never_echoes_the_key(self) -> None:
        status, body = self.call("/runs", method="POST", body={"action": "get_health"}, key="wrong")
        self.assertNotIn(KEY, json.dumps(body))

    # --------------------------------------------------------------- actions

    def test_sync_action_returns_a_result(self) -> None:
        status, body = self.call("/runs", method="POST", body={"action": "get_health"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "succeeded")
        self.assertIn("observations", body["result"])
        self.assertIn("sources", body["result"])

    def test_health_result_contains_no_secrets(self) -> None:
        _, body = self.call("/runs", method="POST", body={"action": "get_health"})
        serialised = json.dumps(body)
        self.assertNotIn(KEY, serialised)
        self.assertNotIn("SLACK_BOT_TOKEN", serialised)
        self.assertNotIn("xoxb", serialised)

    def test_review_action_accepts_a_limit(self) -> None:
        status, body = self.call(
            "/runs", method="POST", body={"action": "review_signal", "input": {"limit": 5}}
        )
        self.assertEqual(status, 200)
        self.assertIn("items", body["result"])

    def test_unknown_action_is_rejected(self) -> None:
        status, body = self.call("/runs", method="POST", body={"action": "delete_everything"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "unknown_action")

    def test_non_json_body_is_rejected(self) -> None:
        request = urllib.request.Request(
            f"{self.base}/runs", data=b"not json",
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=10)
            self.fail("expected a 400")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 400)
            error.close()

    def test_async_action_returns_a_task_then_completes(self) -> None:
        import time

        status, task = self.call(
            "/runs", method="POST",
            body={"action": "run_scan", "input": {"fast": True}, "run_id": "run-async-1"},
        )
        self.assertEqual(status, 202)
        self.assertEqual(task["status"], "running")
        task_id = task["task_id"]
        for _ in range(60):
            _, polled = self.call(f"/tasks/{task_id}")
            if polled["status"] != "running":
                break
            time.sleep(0.25)
        self.assertIn(polled["status"], {"succeeded", "failed"})

    def test_repeated_run_id_is_idempotent(self) -> None:
        first = self.call("/runs", method="POST",
                          body={"action": "run_scan", "run_id": "run-idem-1"})[1]
        second = self.call("/runs", method="POST",
                           body={"action": "run_scan", "run_id": "run-idem-1"})[1]
        self.assertEqual(first["task_id"], second["task_id"])

    def test_unknown_task_is_a_404(self) -> None:
        status, body = self.call("/tasks/does-not-exist")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

    def test_unknown_endpoint_is_a_404(self) -> None:
        self.assertEqual(self.call("/nope", key=None)[0], 404)


class PondAuthFailClosedTest(unittest.TestCase):
    def test_unset_key_denies_rather_than_opens(self) -> None:
        """An unset access key must not mean 'open to the world'."""
        previous = os.environ.pop("POND_ACCESS_KEY", None)
        try:
            self.assertFalse(pond._authorised("Bearer anything"))
            self.assertFalse(pond._authorised(None))
        finally:
            if previous:
                os.environ["POND_ACCESS_KEY"] = previous


if __name__ == "__main__":
    unittest.main()
