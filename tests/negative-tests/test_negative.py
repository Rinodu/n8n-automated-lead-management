"""Negative & security boundary test suite for Automated Lead Management System."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from lead_service import LeadError, LeadService


class NegativeLeadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = LeadService(self.tmp.name, mode="demo")

    def test_invalid_email_formats_rejected_with_clear_reason(self):
        bad_emails = [
            "plainaddress",
            "@missing-local.org",
            "user@.invalid-domain",
            "user@domain..com",
            "user@mailinator.com",
            "user@invalid.test",
            "bad\nheader@example.test",
        ]
        for bad_email in bad_emails:
            with self.subTest(email=bad_email):
                with self.assertRaises(LeadError) as ctx:
                    self.service.intake(
                        {
                            "lead_id": "lead_neg_001",
                            "name": "Test User",
                            "email": bad_email,
                        }
                    )
                self.assertEqual(ctx.exception.http_status, 422)
                self.assertTrue(len(ctx.exception.reason) > 5)

    def test_invalid_lead_id_and_sql_injection_payload_rejected(self):
        bad_ids = [
            "",
            "lead_1; DROP TABLE leads;--",
            "../etc/passwd",
            "a" * 85,
        ]
        for bad_id in bad_ids:
            with self.subTest(lead_id=bad_id):
                with self.assertRaises(LeadError) as ctx:
                    self.service.intake(
                        {
                            "lead_id": bad_id,
                            "name": "Valid Name",
                            "email": "valid@example.test",
                        }
                    )
                self.assertEqual(ctx.exception.http_status, 422)
                self.assertEqual(ctx.exception.code, "invalid_lead_id")

    def test_conflicting_payload_on_same_lead_id_returns_409(self):
        code1, _ = self.service.intake(
            {
                "lead_id": "lead_conflict_01",
                "name": "Alice Smith",
                "email": "alice@example.test",
            }
        )
        self.assertEqual(code1, 202)

        with self.assertRaises(LeadError) as ctx:
            self.service.intake(
                {
                    "lead_id": "lead_conflict_01",
                    "name": "Alice Smith",
                    "email": "different.email@example.test",
                }
            )
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertEqual(ctx.exception.code, "lead_id_conflict")


if __name__ == "__main__":
    unittest.main()
