# Test Plan & Execution Evidence (`TEST_PLAN.md`)
## Automated Lead Management System

- **Execution Date:** 2026-11-03
- **Environment:** Windows, Python 3.12.10
- **Command:** `python -m unittest tests/positive-tests/test_positive.py tests/negative-tests/test_negative.py tests/regression-tests/test_regression.py -v`
- **Status:** **9 / 9 Tests Passing (`OK` in 1.233s)**

---

## 1. Test Coverage Matrix

| Suite | Test Case | Scenario & Acceptance Criteria | Result |
|---|---|---|---|
| **Positive** | `test_normalization_trims_and_standardizes_fields` | Whitespace trimmed, multiple spaces collapsed, email lowercased, `08...` phone normalized to `+628...`. | **PASS** |
| **Positive** | `test_valid_lead_intake_and_crm_delivery` | Valid payload returns `202` (`masuk`), worker transitions through `processing` → `sent`, creates CRM record & notification. | **PASS** |
| **Negative** | `test_invalid_email_formats_rejected_with_clear_reason` | Malformed emails, header-injection newlines, and disposable domains (`mailinator.com`, `invalid.test`) rejected with `422` + clear reason. | **PASS** |
| **Negative** | `test_invalid_lead_id_and_sql_injection_payload_rejected` | Empty IDs, path traversal, and SQL injection payloads (`lead_1; DROP TABLE leads;--`) rejected with `422`. | **PASS** |
| **Negative** | `test_conflicting_payload_on_same_lead_id_returns_409` | Submitting a different email under an existing `lead_id` returns `409 Conflict` (`lead_id_conflict`). | **PASS** |
| **Regression** | `test_idempotency_by_lead_id_and_email_prevents_duplicate_crm_records` | Duplicate `lead_id` and duplicate normalized `email` both return `200 duplicate`; CRM API called strictly once. | **PASS** |
| **Regression** | `test_timeout_transitions_and_idempotent_retry_recovery` | First CRM timeout transitions status to `timeout`; retry recovers to `sent` using identical `Idempotency-Key`. | **PASS** |
| **Regression** | `test_permanent_api_failure_transitions_to_error_and_logs_alert` | Upstream 500 failure sets status to `error`, sets `attention_required = true`, and writes error alert notification. | **PASS** |
| **Regression** | `test_http_server_auth_and_end_to_end_contract` | Live local HTTP server rejects unauthenticated requests (`401`), accepts valid payload (`202`), and deduplicates repeat (`200`). | **PASS** |

## 2. How to Re-Run Tests

```powershell
python -m unittest tests/positive-tests/test_positive.py tests/negative-tests/test_negative.py tests/regression-tests/test_regression.py -v
python run_demo.py
```
