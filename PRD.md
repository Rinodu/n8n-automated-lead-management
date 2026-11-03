# Product Requirements Document (PRD)
## Automated Lead Management System (n8n + REST API Companion)

- **Version:** 1.0.0
- **Date:** 2026-11-03
- **Author:** Edhu (AI-Assisted Production Workflow)
- **Status:** Verified Portfolio Project

---

## 1. Problem Statement
Small and medium businesses collecting leads from landing pages, web forms, and ad webhooks frequently face four operational issues:
1. **Inconsistent Contact Data:** Unformatted names, mixed-case emails, and unstandardized phone numbers pollute the CRM.
2. **Invalid Emails:** Typos or invalid email formats trigger bounce penalties in downstream email tools.
3. **Duplicate Submissions:** Users double-clicking submit buttons or webhook senders retrying on network glitches create duplicate CRM records.
4. **Silent CRM API Failures:** When a downstream CRM API times out or returns 5xx/429 errors, leads are lost without retry tracking or operator alerts.

## 2. Project Objective
Build a verifiable, asynchronous **Automated Lead Management System** using **n8n** as the workflow orchestration and webhook ingress layer, backed by a lightweight, zero-dependency **Python REST API & SQLite persistence service** (`lead_service.py`) that guarantees normalization, validation, idempotency/deduplication, status tracking, CRM delivery, and failure notifications.

## 3. Target Workflow & Lifecycle States

### End-to-End Flow
`Form / Webhook Ingress (n8n)` → `Normalization (name, email, lead_id, phone, source)` → `Email & Schema Validation` → `Deduplication Check` → `SQLite Queue / Review Storage` → `CRM REST API Integration` → `Notification & Monitoring`

### Planned Process Statuses
Every accepted lead record transitions through explicit lifecycle states:
- `masuk` — Lead received, normalized, validated, and persisted in the local queue.
- `processing` — Worker has locked the lead and is actively syncing with the target CRM REST API.
- `timeout` — Upstream CRM API timed out; lead is scheduled for exponential backoff retry.
- `sent` — Lead successfully created/upserted in the CRM and notification logged.
- `error` — Permanent rejection or retry limit exhausted; flagged for operator action.

## 4. Functional Requirements

| ID | Requirement | Acceptance Criteria |
|---|---|---|
| **FR-01** | **Data Normalization** | Trim leading/trailing whitespace, collapse multiple spaces in `name`, lowercase `email`, normalize `phone` digits/prefix, and validate `lead_id` (`[A-Za-z0-9_-]{1,80}`). |
| **FR-02** | **Email Validation** | Reject missing `@`/domain, control characters, length > 254, or blocked disposable/invalid test domains with HTTP `422` and explicit `reason`. |
| **FR-03** | **Idempotency & Deduplication** | Repeated submission with identical `lead_id` and payload fingerprint returns HTTP `200` (`status: duplicate`). Different payload on existing `lead_id` returns HTTP `409` (`lead_id_conflict`). Duplicate normalized email within active records is flagged and deduplicated without creating redundant CRM contacts. |
| **FR-04** | **CRM Sync & Retry** | Worker sends normalized payload to CRM endpoint with `Idempotency-Key`. Network timeouts transition state to `timeout`; 5xx errors retry with backoff; permanent failures transition to `error`. |
| **FR-05** | **Notification & Monitoring** | Successful syncs and actionable failures (`timeout` / `error`) generate structured notification events (Email/Telegram outbox simulation in demo mode) and expose queue metrics via `/metrics`. |

## 5. Non-Functional & Security Requirements
- **No Hardcoded Secrets:** API keys (`X-Webhook-Key`, `X-Service-Key`, `CRM_API_KEY`) must be injected via environment variables or n8n Credential Manager, never stored in repository files or exported workflows.
- **SQL Injection Prevention:** All SQLite queries must use parameterized statements (`?` placeholders).
- **Bounded Inputs & Rate Limiting:** Maximum request body size of 16 KiB, strict JSON parsing (rejecting duplicate keys), and per-minute rate limiting (`429 Too Many Requests`).
- **Safe Logging:** Audit logs record HMAC reference hashes rather than raw customer emails or secret tokens.

## 6. Out of Scope (Initial Version)
- Multi-host distributed clustering (designed for single-host local/VPS deployment).
- Real third-party production CRM billing/charging.
