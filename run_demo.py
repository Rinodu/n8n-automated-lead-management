"""Run an end-to-end local demonstration of the Automated Lead Management System and save demo-result.json."""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from lead_service import LeadError, LeadService


def main():
    with tempfile.TemporaryDirectory() as tmp:
        call_count = {"crm": 0}

        def simulated_crm(url, token, payload, idempotency_key):
            call_count["crm"] += 1
            if payload["lead_id"] == "lead_demo_timeout" and call_count["crm"] == 2:
                raise TimeoutError("simulated_crm_timeout")
            return {"crm_id": f"crm_live_sim_{payload['lead_id']}"}

        service = LeadService(tmp, mode="demo", crm_transport=simulated_crm)
        results = []

        # 1. Valid lead intake + sync
        code1, res1 = service.intake(
            {
                "lead_id": "lead_demo_001",
                "name": "  Andi   Wijaya ",
                "email": "Andi.Wijaya@Example.test",
                "phone": "0812-3456-7890",
                "source": "Upwork_Landing_Page",
                "company": "Biruni Demo Corp",
            }
        )
        sync1 = service.process_one("lead_demo_001")
        results.append({"scenario": "valid_lead_intake_and_sync", "http": code1, "intake": res1, "delivery": sync1})

        # 2. Exact duplicate submission
        code2, res2 = service.intake(
            {
                "lead_id": "lead_demo_001",
                "name": "Andi Wijaya",
                "email": "andi.wijaya@example.test",
                "phone": "+6281234567890",
                "source": "upwork_landing_page",
                "company": "Biruni Demo Corp",
            }
        )
        results.append({"scenario": "duplicate_lead_id_ignored", "http": code2, "response": res2})

        # 3. Invalid email rejected
        try:
            service.intake(
                {
                    "lead_id": "lead_demo_bad_email",
                    "name": "Invalid User",
                    "email": "spammer@mailinator.com",
                }
            )
        except LeadError as err:
            results.append(
                {
                    "scenario": "disposable_email_rejected",
                    "http": err.http_status,
                    "error": err.code,
                    "reason": err.reason,
                }
            )

        # 4. Timeout then idempotent retry recovery
        service.intake(
            {
                "lead_id": "lead_demo_timeout",
                "name": "Siti Rahma",
                "email": "siti.rahma@example.test",
                "phone": "+6281398765432",
            }
        )
        attempt1 = service.process_one("lead_demo_timeout")
        attempt2 = service.process_one("lead_demo_timeout")
        results.append(
            {
                "scenario": "timeout_then_idempotent_retry",
                "attempt_1": attempt1,
                "attempt_2": attempt2,
            }
        )

        summary = {
            "project": "Automated Lead Management System v1.0",
            "scenarios": results,
            "final_metrics": service.metrics(),
        }
        out_path = ROOT / "demo-result.json"
        out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
