"""Positive test suite for Automated Lead Management System."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from lead_service import LeadService, normalize_lead, validate_lead


class PositiveLeadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = LeadService(self.tmp.name, mode="demo")

    def test_normalization_trims_and_standardizes_fields(self):
        raw = {
            "lead_id": "  lead_001  ",
            "name": "  Budi   Santoso  ",
            "email": "  Budi.Santoso@Example.TEST ",
            "phone": "0812-3456-7890",
            "source": "  Landing_Page ",
            "company": "  PT   Maju  Jaya ",
        }
        normalized = normalize_lead(raw)
        self.assertEqual(normalized["lead_id"], "lead_001")
        self.assertEqual(normalized["name"], "Budi Santoso")
        self.assertEqual(normalized["email"], "budi.santoso@example.test")
        self.assertEqual(normalized["phone"], "+6281234567890")
        self.assertEqual(normalized["source"], "landing_page")
        self.assertEqual(normalized["company"], "PT Maju Jaya")

    def test_valid_lead_intake_and_crm_delivery(self):
        payload = {
            "lead_id": "lead_pos_101",
            "name": "Sarah Connor",
            "email": "sarah.connor@example.test",
            "phone": "+14155550199",
            "source": "webinar",
            "company": "Cyberdyne Demo",
            "notes": "Interested in workflow automation package.",
        }
        status_code, response = self.service.intake(payload)
        self.assertEqual(status_code, 202)
        self.assertEqual(response["status"], "accepted")
        self.assertEqual(response["lifecycle_status"], "masuk")

        result = self.service.process_one("lead_pos_101")
        self.assertIsNotNone(result)
        self.assertEqual(result["lifecycle_status"], "sent")
        self.assertEqual(result["crm_id"], "crm_demo_lead_pos_101")

        stored = self.service.get_lead("lead_pos_101")
        self.assertEqual(stored["status"], "sent")
        self.assertEqual(stored["attempts"], 1)

        metrics = self.service.metrics()
        self.assertEqual(metrics["status_counts"]["sent"], 1)
        self.assertEqual(metrics["crm_records_count"], 1)
        self.assertEqual(metrics["notifications_count"], 1)
        self.assertFalse(metrics["attention_required"])


if __name__ == "__main__":
    unittest.main()
