import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.http_api import make_handler
from src.repository import Repository
from src.service import Service


def request(port, method, path, body=None, actor="u", role="viewer"):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"X-Actor": actor, "X-Role": role}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    payload = json.loads(resp.read().decode("utf-8"))
    conn.close()
    return resp.status, payload


class ReinforcementHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = Repository(str(Path(cls.tmp.name) / "http.db"))
        cls.service = Service(cls.repo)
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(cls.service, str(Path("static").resolve())))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.repo.close()
        cls.tmp.cleanup()

    def payload(self, **overrides):
        data = dict(
            building_name="HTTP楼", owner_name="产权甲", owner_consent=False,
            estimate=1000000, funds_available=800000, zone="Z",
            construction_start="2026-10-01", construction_end="2026-11-30",
            resettlement_count=50)
        data.update(overrides)
        return data

    def test_full_http_flow(self):
        status, body = request(self.port, "GET", "/health")
        self.assertEqual(status, 200)

        status, body = request(self.port, "POST", "/api/reinforcement",
                               self.payload(), "u1", "assessor")
        self.assertEqual(status, 201)
        self.assertIn("funding", body["blocker_codes"])
        project_id = body["id"]
        version = body["version"]

        # 容量不足时拒绝登记容量（viewer 越权）
        status, _ = request(self.port, "POST", "/api/zones",
                            {"zone": "Z", "capacity": 60}, "u", "viewer")
        self.assertEqual(status, 403)
        status, _ = request(self.port, "POST", "/api/zones",
                            {"zone": "Z", "capacity": 60}, "board", "review_board")
        self.assertEqual(status, 200)

        # 批准前仍有卡点 -> 409 带建议/卡点
        status, body = request(self.port, "POST",
                               f"/api/reinforcement/{project_id}/approve",
                               {"expected_version": version},
                               "board", "review_board")
        self.assertEqual(status, 409)
        self.assertIn("details", body)

        # 补齐同意与资金
        status, body = request(self.port, "POST",
                               f"/api/reinforcement/{project_id}",
                               {"owner_consent": True, "funds_available": 1000000,
                                "expected_version": version},
                               "u1", "assessor")
        self.assertEqual(status, 200)
        self.assertEqual(body["blockers"], [])

        status, body = request(self.port, "POST",
                               f"/api/reinforcement/{project_id}/approve",
                               {"expected_version": body["version"]},
                               "board", "review_board")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "approved")

        # 列表与分区接口
        status, body = request(self.port, "GET", "/api/reinforcement")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["projects"]), 1)
        status, body = request(self.port, "GET", "/api/reinforcement?status=approved")
        self.assertEqual(len(body["projects"]), 1)
        status, body = request(self.port, "GET", "/api/zones")
        self.assertEqual(body["zones"][0]["capacity"], 60)

    def test_validation_http_status(self):
        status, body = request(self.port, "POST", "/api/reinforcement",
                               self.payload(construction_end="bad-date"),
                               "u1", "assessor")
        self.assertEqual(status, 422)


if __name__ == "__main__":
    unittest.main()
