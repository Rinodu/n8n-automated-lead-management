"""Regression, idempotency, retry backoff, and HTTP endpoint test suite."""
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from lead_service import LeadService, make_http_handler


class RegressionLeadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_idempotency_by_lead_id_and_email_prevents_duplicate_crm_records(self):
        calls = []

        def fake_crm(url, token, payload, idem_key):
            calls.append((payload["lead_id"], idem_key))
            return {"crm_id": f"crm_{payload['lead_id']}"}

        svc = LeadService(self.tmp.name, mode="demo", crm_transport=fake_crm)
        payload = {
            "lead_id": "lead_idem_01",
            "name": "Dian Pratama",
            "email": "Dian.Pratama@Example.test",
            "phone": "081122334455",
        }
        code1, res1 = svc.intake(payload)
        self.assertEqual(code1, 202)
        svc.process_one("lead_idem_01")

        # Exact duplicate by lead_id
        code2, res2 = svc.intake(payload)
        self.assertEqual(code2, 200)
        self.assertEqual(res2["status"], "duplicate")
        self.assertEqual(res2["dedup_by"], "lead_id")

        # Duplicate by normalized email under a new lead_id
        code3, res3 = svc.intake(
            {
                "lead_id": "lead_idem_02",
                "name": "Dian P.",
                "email": "dian.pratama@example.test",
                "phone": "+6281122334455",
            }
        )
        self.assertEqual(code3, 200)
        self.assertEqual(res3["status"], "duplicate")
        self.assertEqual(res3["dedup_by"], "email")
        self.assertEqual(res3["existing_lead_id"], "lead_idem_01")

        # CRM API was called strictly once
        self.assertEqual(len(calls), 1)
        self.assertEqual(svc.metrics()["crm_records_count"], 1)

    def test_timeout_transitions_and_idempotent_retry_recovery(self):
        attempt_counter = {"count": 0}
        seen_keys = []

        def flaky_crm(url, token, payload, idem_key):
            attempt_counter["count"] += 1
            seen_keys.append(idem_key)
            if attempt_counter["count"] == 1:
                raise TimeoutError("upstream_crm_timeout")
            return {"crm_id": "crm_recovered_01"}

        svc = LeadService(self.tmp.name, mode="demo", config={"MAX_ATTEMPTS": "3"}, crm_transport=flaky_crm)
        svc.intake(
            {
                "lead_id": "lead_retry_01",
                "name": "Rina Wijaya",
                "email": "rina@example.test",
            }
        )

        # First attempt times out -> status becomes 'timeout'
        first = svc.process_one("lead_retry_01")
        self.assertEqual(first["lifecycle_status"], "timeout")
        self.assertEqual(svc.get_lead("lead_retry_01")["status"], "timeout")

        # Second attempt succeeds -> status becomes 'sent' with identical idempotency key
        second = svc.process_one("lead_retry_01")
        self.assertEqual(second["lifecycle_status"], "sent")
        self.assertEqual(second["crm_id"], "crm_recovered_01")
        self.assertEqual(len(seen_keys), 2)
        self.assertEqual(seen_keys[0], seen_keys[1])

    def test_permanent_api_failure_transitions_to_error_and_logs_alert(self):
        def failing_crm(url, token, payload, idem_key):
            raise RuntimeError("crm_500_internal_error")

        svc = LeadService(self.tmp.name, mode="demo", crm_transport=failing_crm)
        svc.intake(
            {
                "lead_id": "lead_err_01",
                "name": "Hendra Kusuma",
                "email": "hendra@example.test",
            }
        )
        res = svc.process_one("lead_err_01")
        self.assertEqual(res["lifecycle_status"], "error")
        metrics = svc.metrics()
        self.assertEqual(metrics["status_counts"]["error"], 1)
        self.assertTrue(metrics["attention_required"])
        self.assertGreaterEqual(metrics["notifications_count"], 1)

    def test_http_server_auth_and_end_to_end_contract(self):
        svc = LeadService(self.tmp.name, mode="demo")
        secret = "test_service_secret_key_1234567890"
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_http_handler(svc, secret, auto_process=True))
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        base_url = f"http://127.0.0.1:{port}"

        # 1. Unauthorized request -> 401
        req_unauth = Request(
            f"{base_url}/leads",
            data=b'{"lead_id":"l1","name":"Test","email":"t@example.test"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req_unauth, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

        # 2. Valid lead -> 202 + auto-processed to 'sent'
        body = json.dumps(
            {
                "lead_id": "http_lead_01",
                "name": " HTTP  Tester ",
                "email": "HTTP.Tester@Example.test",
                "phone": "081299998888",
            }
        ).encode("utf-8")
        req_valid = Request(
            f"{base_url}/leads",
            data=body,
            headers={"Content-Type": "application/json", "X-Service-Key": secret},
            method="POST",
        )
        with urlopen(req_valid, timeout=5) as resp:
            self.assertEqual(resp.status, 202)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["lifecycle_status"], "sent")
            self.assertEqual(data["normalized"]["email"], "http.tester@example.test")

        # 3. Duplicate submission -> 200 duplicate
        req_dup = Request(
            f"{base_url}/leads",
            data=body,
            headers={"Content-Type": "application/json", "X-Service-Key": secret},
            method="POST",
        )
        with urlopen(req_dup, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "duplicate")


if __name__ == "__main__":
    unittest.main()
