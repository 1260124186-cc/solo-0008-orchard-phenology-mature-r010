from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app.config import RuntimeConfig
from app.persistence import Database, Repository
from app.transport.server import create_server


class HttpFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        data_dir = Path(self.temporary.name)
        self.repository = Repository(Database(data_dir / "atlas.sqlite3"))
        self.repository.open()
        self.server = create_server(
            RuntimeConfig(
                host="127.0.0.1",
                port=0,
                data_dir=data_dir,
                request_limit=1_048_576,
                max_workers=8,
            ),
            self.repository,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/api"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.repository.close()
        self.temporary.cleanup()

    def test_http_auth_idempotency_audit_and_jobs(self) -> None:
        status, payload = self._request("GET", "/plots")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "authentication_required")

        headers = {
            "X-Actor-Id": "local-admin",
            "X-Idempotency-Key": "http-plot-key",
        }
        body = {
            "code": "OR-8101",
            "name": "HTTP 验证园",
            "locality": "测试地",
            "cultivar_focus": "测试品种",
            "steward": "测试组",
            "planting_year": 2010,
            "note": "",
        }
        first_status, first = self._request("PUT", "/plots", body, headers)
        second_status, second = self._request("PUT", "/plots", body, headers)
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(first["id"], second["id"])

        audit_status, audit = self._request(
            "GET",
            f"/audit?resource_id={first['id']}",
            headers={"X-Actor-Id": "local-admin"},
        )
        self.assertEqual(audit_status, 200)
        self.assertEqual(audit["total"], 1)

        outbox_status, outbox = self._request(
            "GET",
            "/outbox?status=pending",
            headers={"X-Actor-Id": "local-admin"},
        )
        self.assertEqual(outbox_status, 200)
        self.assertEqual(outbox["total"], 1)
        publish_status, published = self._request(
            "PUT",
            f"/outbox/{outbox['items'][0]['event_id']}/publish",
            {},
            {"X-Actor-Id": "local-admin"},
        )
        self.assertEqual(publish_status, 200)
        self.assertEqual(published["status"], "published")

        job_status, job = self._request(
            "PUT",
            "/jobs",
            {
                "job_type": "integrity_scan",
                "payload": {},
                "max_attempts": 3,
                "priority": 100,
                "available_at": None,
            },
            {
                "X-Actor-Id": "local-admin",
                "X-Idempotency-Key": "http-job-key",
            },
        )
        self.assertEqual(job_status, 200)
        self.assertEqual(job["status"], "queued")

    def test_http_migration_plan_lifecycle_and_capability(self) -> None:
        admin = {"X-Actor-Id": "local-admin"}
        observer = {"X-Actor-Id": "local-observer"}

        # 普通观察者没有迁移管理能力。
        status, payload = self._request("GET", "/migration/status", headers=observer)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "forbidden")

        # 观察者至少需要迁移读能力；这里直接验证管理员路径。
        status, status_payload = self._request(
            "GET", "/migration/status", headers=admin
        )
        self.assertEqual(status, 200)
        self.assertEqual(status_payload["generation"], 1)

        # 准备一个最小园区。
        plot_body = {
            "code": "OR-8202",
            "name": "迁移园区",
            "locality": "地",
            "cultivar_focus": "品种",
            "steward": "组",
            "planting_year": 2010,
            "note": "",
        }
        plot_status, plot = self._request("PUT", "/plots", plot_body, admin)
        self.assertEqual(plot_status, 200)

        status, plan = self._request(
            "PUT",
            "/migration/plans",
            {"name": "HTTP 迁移", "batch_size": 10},
            admin,
        )
        self.assertEqual(status, 200)
        self.assertEqual(plan["status"], "active")
        plan_id = plan["id"]

        status, detail = self._request(
            "GET", f"/migration/plans/{plan_id}", headers=admin
        )
        self.assertEqual(status, 200)
        self.assertTrue(detail["batches"])
        seq = detail["batches"][0]["seq"]

        status, verified = self._request(
            "PUT",
            f"/migration/plans/{plan_id}/batches/{seq}/verify",
            {},
            admin,
        )
        self.assertEqual(status, 200)
        self.assertEqual(verified["status"], "verified")

        status, applied = self._request(
            "PUT",
            f"/migration/plans/{plan_id}/batches/{seq}/apply",
            {},
            admin,
        )
        self.assertEqual(status, 200)
        self.assertEqual(applied["status"], "applied")

        status, report = self._request(
            "GET", f"/migration/plans/{plan_id}/report", headers=admin
        )
        self.assertEqual(status, 200)
        self.assertTrue(report["business_result_parity"]["ok"])
        self.assertTrue(report["version_lineage"]["ok"])
        self.assertTrue(report["audit_outbox_parity"]["ok"])

        status, finalized = self._request(
            "PUT", f"/migration/plans/{plan_id}/finalize", {}, admin
        )
        self.assertEqual(status, 200)
        self.assertEqual(finalized["status"], "finalized")

        status, health = self._request("GET", "/health", headers=admin)
        self.assertEqual(health["domain_generation"], 2)

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method,
            headers={
                "Content-Type": "application/json",
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read().decode("utf-8"))
            finally:
                error.close()


if __name__ == "__main__":
    unittest.main()
