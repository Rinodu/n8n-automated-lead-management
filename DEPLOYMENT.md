# Deployment & Operations Guide (`DEPLOYMENT.md`)
## Automated Lead Management System

---

## 1. Local Demo Mode (No External Accounts Required)

Requires **Python 3.12+**:

```powershell
# 1. Run the automated verification suite (9 tests)
python -m unittest tests/positive-tests/test_positive.py tests/negative-tests/test_negative.py tests/regression-tests/test_regression.py -v

# 2. Run the end-to-end demo script and generate demo-result.json
python run_demo.py
```

## 2. Running the HTTP Service + n8n Locally

### Step 1: Start the Lead Service
```powershell
$env:SERVICE_KEYS = "replace_with_32_char_random_service_key_012345"
$env:MODE = "demo"
python src/lead_service.py
```
The service listens on `http://127.0.0.1:8788` (`/health`, `/metrics`, and `POST /leads`).

### Step 2: Import Workflow into n8n
1. Open your local n8n editor (`http://127.0.0.1:5678`).
2. Import `workflows/lead-management.json`.
3. Configure two **Header Auth** credentials in n8n Credential Manager:
   - On **Lead Intake Webhook**: Header Name `X-Webhook-Key`, Value `<your_webhook_secret>`.
   - On **Normalize Validate & Sync CRM**: Header Name `X-Service-Key`, Value `<same_as_SERVICE_KEYS>`.
4. Test with a sample payload:
```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8788/leads" `
  -Headers @{ "X-Service-Key" = $env:SERVICE_KEYS } `
  -ContentType "application/json" `
  -Body '{"lead_id":"lead_local_01","name":"Budi Santoso","email":"budi@example.test","phone":"08123456789"}'
```

## 3. Backup & Recovery Procedure
- Database file is stored at `data/leads-<mode>.sqlite3`.
- To create a consistent hot backup while the service is running:
```powershell
python -c "import sqlite3; src=sqlite3.connect('data/leads-demo.sqlite3'); dst=sqlite3.connect('data/backup-leads.sqlite3'); src.backup(dst); dst.close(); src.close()"
```
