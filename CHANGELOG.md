# Changelog (`CHANGELOG.md`)

All notable changes to the **Automated Lead Management System** portfolio project are documented in this file.

## [1.0.0] - 2026-11-03
### Added
- Credential-free n8n workflow export (`workflows/lead-management.json`) with Header Auth webhook ingress, HTTP Request processor node, and structured webhook response node.
- Python companion REST service & SQLite state machine (`src/lead_service.py`) supporting lifecycle states: `masuk`, `processing`, `timeout`, `sent`, and `error`.
- Contact normalization (whitespace trimming, email lowercasing, E.164 phone standardization) and email validation (format + disposable domain blocking).
- Dual-layer deduplication by `lead_id` (with SHA-256 HMAC conflict detection) and normalized `email`.
- Deterministic outbound `Idempotency-Key` generation and exponential backoff retry handling.
- Repeatable positive, negative, and regression test suites (`9/9 OK`) and offline demo runner (`run_demo.py` -> `demo-result.json`).
- Full project documentation (`PRD.md`, `ARCHITECTURE.md`, `TEST_PLAN.md`, `SECURITY_REVIEW.md`, `DEPLOYMENT.md`, `FINAL_QA_REPORT.md`).
