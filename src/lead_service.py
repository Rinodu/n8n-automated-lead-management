"""
Automated Lead Management System — Core Service & Worker.
Implements normalization, email validation, deduplication (lead_id & email),
lifecycle statuses (masuk, processing, timeout, sent, error), CRM sync,
and privacy-safe HMAC audit logging.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

LOG = logging.getLogger("lead_management")

VALID_STATUSES = ("masuk", "processing", "timeout", "sent", "error")
DISPOSABLE_DOMAINS = frozenset({
    "mailinator.com",
    "tempmail.com",
    "10minutemail.com",
    "guerrillamail.com",
    "invalid.test",
    "disposable.test",
})
EMAIL_REGEX = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,63}$"
)
LEAD_ID_REGEX = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class LeadError(Exception):
    def __init__(self, http_status: int, code: str, reason: str = ""):
        super().__init__(code)
        self.http_status = http_status
        self.code = code
        self.reason = reason or code


def normalize_lead(raw: dict) -> dict:
    """Normalize raw webhook input fields into canonical form."""
    if not isinstance(raw, dict):
        raise LeadError(422, "invalid_payload_object", "Payload must be a JSON object.")

    def clean_str(val, default=""):
        if val is None:
            return default
        if not isinstance(val, str):
            raise LeadError(422, "invalid_field_type", "Text fields must be strings.")
        return val.strip()

    lead_id = clean_str(raw.get("lead_id"))
    name = re.sub(r"\s+", " ", clean_str(raw.get("name")))
    email = clean_str(raw.get("email")).lower()
    raw_phone = clean_str(raw.get("phone"))
    phone = re.sub(r"[^\d+]", "", raw_phone)
    if phone.startswith("08"):
        phone = "+62" + phone[1:]
    source = clean_str(raw.get("source"), default="web_form").lower()
    company = re.sub(r"\s+", " ", clean_str(raw.get("company")))
    notes = clean_str(raw.get("notes"))

    return {
        "lead_id": lead_id,
        "name": name,
        "email": email,
        "phone": phone,
        "source": source,
        "company": company,
        "notes": notes,
    }


def validate_lead(raw: dict) -> dict:
    """Normalize and validate lead fields; raise LeadError(422, ...) with clear reason on failure."""
    lead = normalize_lead(raw)

    if not lead["lead_id"] or not LEAD_ID_REGEX.fullmatch(lead["lead_id"]):
        raise LeadError(
            422,
            "invalid_lead_id",
            "lead_id must be 1-80 characters using letters, digits, underscores, or hyphens.",
        )

    if not lead["name"] or len(lead["name"]) < 2 or len(lead["name"]) > 120:
        raise LeadError(
            422,
            "invalid_name",
            "name is required and must be between 2 and 120 characters.",
        )
    if any(ord(ch) < 32 for ch in lead["name"]):
        raise LeadError(422, "invalid_name_characters", "name contains control characters.")

    email = lead["email"]
    if not email or len(email) > 254 or any(ord(ch) < 32 for ch in email):
        raise LeadError(422, "invalid_email_format", "Email address is missing or exceeds 254 characters.")
    if not EMAIL_REGEX.fullmatch(email):
        raise LeadError(422, "invalid_email_format", f"Email '{email}' is not a valid email format.")

    domain = email.rsplit("@", 1)[-1]
    if domain in DISPOSABLE_DOMAINS:
        raise LeadError(
            422,
            "disposable_email_rejected",
            f"Email domain '{domain}' is blocked as disposable or invalid.",
        )

    if lead["phone"] and not re.fullmatch(r"^\+?[0-9]{7,18}$", lead["phone"]):
        raise LeadError(
            422,
            "invalid_phone",
            "phone must contain 7 to 18 digits (optional leading +).",
        )

    if len(lead["company"]) > 120 or len(lead["notes"]) > 1000 or len(lead["source"]) > 60:
        raise LeadError(422, "field_too_long", "Optional field exceeds maximum allowed length.")

    return lead


def default_crm_transport(url: str, token: str, payload: dict, idempotency_key: str) -> dict:
    """Send validated lead to an external HTTPS CRM REST endpoint."""
    if not url.startswith("https://"):
        raise ValueError("Live CRM endpoint must use HTTPS")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
    }
    req = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with build_opener(NoRedirect).open(req, timeout=10) as resp:
        body = resp.read(65537)
        if len(body) > 65536:
            raise ValueError("Oversized CRM response")
        return json.loads(body.decode("utf-8"))


class LeadService:
    """Stateful lead processor with SQLite persistence, deduplication, and retry worker."""

    def __init__(
        self,
        data_dir: str | Path,
        mode: str = "demo",
        config: dict | None = None,
        crm_transport=None,
    ):
        if mode not in ("demo", "live"):
            raise ValueError("mode must be 'demo' or 'live'")
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.config = config or {}
        self.crm_transport = crm_transport or default_crm_transport
        self.db_path = self.data_dir / f"leads-{mode}.sqlite3"
        self.index_key = (self.config.get("INDEX_KEY") or "demo-local-hmac-index-key-0123456789").encode("utf-8")
        self.max_attempts = int(self.config.get("MAX_ATTEMPTS", "3"))
        self._lock = threading.Lock()
        self._init_db()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA secure_delete=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self.connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS leads (
                    lead_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    source TEXT NOT NULL,
                    company TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    crm_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status, next_retry_at)")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS crm_demo (
                    crm_id TEXT PRIMARY KEY,
                    lead_id TEXT UNIQUE NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL,
                    synced_at REAL NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_ref TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at REAL NOT NULL,
                    ref TEXT NOT NULL,
                    event TEXT NOT NULL
                )
                """
            )

    def fingerprint(self, value: dict | str) -> str:
        serialized = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        return hmac.new(self.index_key, serialized.encode("utf-8"), hashlib.sha256).hexdigest()

    def _audit(self, db: sqlite3.Connection, lead_id: str, event: str):
        ref = self.fingerprint(lead_id)[:16]
        db.execute("INSERT INTO audit_log(at, ref, event) VALUES (?, ?, ?)", (time.time(), ref, event))
        LOG.info("event=%s ref=%s", event, ref)

    def _notify(self, db: sqlite3.Connection, lead_id: str, severity: str, event: str, detail: str):
        ref = self.fingerprint(lead_id)[:16]
        channel = self.config.get("NOTIFICATION_CHANNEL", "telegram_and_email_demo")
        db.execute(
            "INSERT INTO notifications(lead_ref, channel, severity, event, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (ref, channel, severity, event, detail, time.time()),
        )

    def intake(self, raw_payload: dict) -> tuple[int, dict]:
        """
        Validate, normalize, and deduplicate an incoming lead.
        Returns (http_status_code, response_dict).
        """
        lead = validate_lead(raw_payload)
        lead_id = lead["lead_id"]
        fp = self.fingerprint(
            {
                "lead_id": lead["lead_id"],
                "name": lead["name"],
                "email": lead["email"],
                "phone": lead["phone"],
                "source": lead["source"],
                "company": lead["company"],
            }
        )
        now = time.time()

        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing_by_id = db.execute(
                "SELECT lead_id, fingerprint, status, crm_id FROM leads WHERE lead_id = ?",
                (lead_id,),
            ).fetchone()

            if existing_by_id:
                if existing_by_id["fingerprint"] != fp:
                    self._audit(db, lead_id, "conflict_lead_id")
                    raise LeadError(
                        409,
                        "lead_id_conflict",
                        "A lead with this lead_id already exists with different contact attributes.",
                    )
                self._audit(db, lead_id, "duplicate_lead_id")
                return 200, {
                    "status": "duplicate",
                    "dedup_by": "lead_id",
                    "lead_id": lead_id,
                    "lifecycle_status": existing_by_id["status"],
                    "crm_id": existing_by_id["crm_id"],
                }

            existing_by_email = db.execute(
                "SELECT lead_id, status, crm_id FROM leads WHERE email = ? AND status IN ('masuk', 'processing', 'timeout', 'sent')",
                (lead["email"],),
            ).fetchone()

            if existing_by_email:
                self._audit(db, lead_id, "duplicate_email")
                return 200, {
                    "status": "duplicate",
                    "dedup_by": "email",
                    "lead_id": lead_id,
                    "existing_lead_id": existing_by_email["lead_id"],
                    "lifecycle_status": existing_by_email["status"],
                    "crm_id": existing_by_email["crm_id"],
                }

            db.execute(
                """
                INSERT INTO leads (
                    lead_id, name, email, phone, source, company, notes,
                    fingerprint, status, attempts, next_retry_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'masuk', 0, ?, ?, ?)
                """,
                (
                    lead["lead_id"],
                    lead["name"],
                    lead["email"],
                    lead["phone"],
                    lead["source"],
                    lead["company"],
                    lead["notes"],
                    fp,
                    now,
                    now,
                    now,
                ),
            )
            self._audit(db, lead_id, "masuk")

        return 202, {
            "status": "accepted",
            "lifecycle_status": "masuk",
            "lead_id": lead_id,
            "normalized": {
                "name": lead["name"],
                "email": lead["email"],
                "phone": lead["phone"],
                "source": lead["source"],
            },
        }

    def process_one(self, lead_id: str | None = None) -> dict | None:
        """
        Process a single queued lead (`masuk` or due `timeout`) through `processing` -> `sent` | `timeout` | `error`.
        """
        now = time.time()
        with self._lock, self.connect() as db:
            if lead_id:
                row = db.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            else:
                row = db.execute(
                    """
                    SELECT * FROM leads
                    WHERE status IN ('masuk', 'timeout') AND next_retry_at <= ?
                    ORDER BY created_at ASC LIMIT 1
                    """,
                    (now,),
                ).fetchone()

            if not row:
                return None

            target_id = row["lead_id"]
            if row["status"] == "sent":
                return {
                    "lead_id": target_id,
                    "lifecycle_status": "sent",
                    "crm_id": row["crm_id"],
                    "attempts": row["attempts"],
                }

            attempts = row["attempts"] + 1
            db.execute(
                "UPDATE leads SET status = 'processing', attempts = ?, updated_at = ? WHERE lead_id = ?",
                (attempts, now, target_id),
            )
            self._audit(db, target_id, "processing")

        payload = {
            "lead_id": row["lead_id"],
            "name": row["name"],
            "email": row["email"],
            "phone": row["phone"],
            "source": row["source"],
            "company": row["company"],
            "notes": row["notes"],
        }
        idempotency_key = "lead-" + hashlib.sha256(target_id.encode("utf-8")).hexdigest()[:32]

        try:
            if self.mode == "demo" and self.crm_transport is default_crm_transport:
                crm_id = f"crm_demo_{target_id}"
                with self._lock, self.connect() as db:
                    db.execute(
                        """
                        INSERT INTO crm_demo(crm_id, lead_id, email, payload, synced_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(lead_id) DO UPDATE SET synced_at = excluded.synced_at
                        """,
                        (crm_id, target_id, row["email"], json.dumps(payload, sort_keys=True), time.time()),
                    )
            else:
                resp = self.crm_transport(
                    self.config.get("CRM_API_URL", "https://crm.example.test/api/v1/leads"),
                    self.config.get("CRM_API_KEY", "demo-token"),
                    payload,
                    idempotency_key,
                )
                crm_id = resp.get("crm_id") or resp.get("id")
                if not crm_id or not isinstance(crm_id, str):
                    raise RuntimeError("invalid_crm_response")
                if self.mode == "demo":
                    with self._lock, self.connect() as db:
                        db.execute(
                            """
                            INSERT INTO crm_demo(crm_id, lead_id, email, payload, synced_at)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(lead_id) DO UPDATE SET synced_at = excluded.synced_at
                            """,
                            (crm_id, target_id, row["email"], json.dumps(payload, sort_keys=True), time.time()),
                        )

            with self._lock, self.connect() as db:
                db.execute(
                    """
                    UPDATE leads
                    SET status = 'sent', crm_id = ?, last_error = NULL, updated_at = ?
                    WHERE lead_id = ?
                    """,
                    (crm_id, time.time(), target_id),
                )
                self._audit(db, target_id, "sent")
                self._notify(db, target_id, "info", "lead_synced", f"Synced to CRM as {crm_id}")
            return {
                "lead_id": target_id,
                "lifecycle_status": "sent",
                "crm_id": crm_id,
                "attempts": attempts,
            }

        except TimeoutError as exc:
            err_code = str(exc) or "crm_timeout"
            next_status = "error" if attempts >= self.max_attempts else "timeout"
            delay = min(300, 2 ** attempts)
            with self._lock, self.connect() as db:
                db.execute(
                    """
                    UPDATE leads
                    SET status = ?, last_error = ?, next_retry_at = ?, updated_at = ?
                    WHERE lead_id = ?
                    """,
                    (next_status, err_code, time.time() + delay, time.time(), target_id),
                )
                self._audit(db, target_id, f"{next_status}:{err_code}")
                self._notify(
                    db,
                    target_id,
                    "error" if next_status == "error" else "warning",
                    f"crm_{next_status}",
                    f"Attempt {attempts}/{self.max_attempts} failed with {err_code}",
                )
            return {
                "lead_id": target_id,
                "lifecycle_status": next_status,
                "error": err_code,
                "attempts": attempts,
            }

        except Exception as exc:
            err_code = str(exc) or "crm_api_error"
            with self._lock, self.connect() as db:
                db.execute(
                    """
                    UPDATE leads
                    SET status = 'error', last_error = ?, updated_at = ?
                    WHERE lead_id = ?
                    """,
                    (err_code, time.time(), target_id),
                )
                self._audit(db, target_id, f"error:{err_code}")
                self._notify(
                    db,
                    target_id,
                    "error",
                    "crm_error",
                    f"Attempt {attempts} failed with error: {err_code}",
                )
            return {
                "lead_id": target_id,
                "lifecycle_status": "error",
                "error": err_code,
                "attempts": attempts,
            }

    def get_lead(self, lead_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT lead_id, name, email, phone, source, company, status,
                       crm_id, attempts, last_error, created_at, updated_at
                FROM leads WHERE lead_id = ?
                """,
                (lead_id,),
            ).fetchone()
            return dict(row) if row else None

    def metrics(self) -> dict:
        with self.connect() as db:
            counts = dict(db.execute("SELECT status, COUNT(*) FROM leads GROUP BY status").fetchall())
            crm_count = db.execute("SELECT COUNT(*) FROM crm_demo").fetchone()[0]
            notif_count = db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        return {
            "status_counts": {st: counts.get(st, 0) for st in VALID_STATUSES},
            "crm_records_count": crm_count,
            "notifications_count": notif_count,
            "attention_required": bool(counts.get("error", 0) or counts.get("timeout", 0)),
        }


def make_http_handler(service: LeadService, service_keys: str, auto_process: bool = True):
    valid_tokens = tuple(k.strip() for k in service_keys.split(",") if k.strip())
    rate_lock = threading.Lock()
    hits: deque[float] = deque()
    limit = int(service.config.get("RATE_LIMIT_PER_MINUTE", "60"))

    class LeadHTTPHandler(BaseHTTPRequestHandler):
        server_version = "LeadManagementService"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_args):
            pass

        def reply(self, status_code: int, body: dict):
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(encoded)
            self.wfile.flush()
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        def _authenticated(self) -> bool:
            supplied = self.headers.get("X-Service-Key", "").encode("utf-8")
            matched = False
            for token in valid_tokens:
                matched |= hmac.compare_digest(supplied, token.encode("utf-8"))
            return matched

        def do_GET(self):
            if self.path == "/health":
                return self.reply(200, {"service": "lead-management", "status": "ok"})
            if self.path == "/metrics":
                if not self._authenticated():
                    return self.reply(401, {"error": "unauthorized"})
                return self.reply(200, service.metrics())
            return self.reply(404, {"error": "not_found"})

        def do_POST(self):
            try:
                with rate_lock:
                    now = time.monotonic()
                    while hits and hits[0] < now - 60:
                        hits.popleft()
                    if len(hits) >= limit:
                        raise LeadError(429, "rate_limited", "Too many requests per minute.")
                    hits.append(now)

                if self.path != "/leads":
                    raise LeadError(404, "not_found", "Endpoint not found.")
                if not self._authenticated():
                    raise LeadError(401, "unauthorized", "Missing or invalid X-Service-Key.")

                lengths = self.headers.get_all("Content-Length", [])
                if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
                    raise LeadError(400, "invalid_framing", "Single Content-Length header required.")
                try:
                    length = int(lengths[0])
                except ValueError:
                    raise LeadError(400, "invalid_content_length", "Invalid Content-Length.")
                if not 0 < length <= 16384:
                    raise LeadError(413, "payload_too_large", "Request body must be between 1 and 16384 bytes.")
                if self.headers.get_content_type() != "application/json":
                    raise LeadError(415, "json_required", "Content-Type must be application/json.")

                raw_bytes = self.rfile.read(length)
                if len(raw_bytes) != length:
                    raise LeadError(400, "incomplete_body", "Incomplete HTTP body.")

                def reject_duplicate_keys(pairs):
                    obj = {}
                    for k, v in pairs:
                        if k in obj:
                            raise ValueError("duplicate_json_key")
                        obj[k] = v
                    return obj

                try:
                    body = json.loads(raw_bytes.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
                except (ValueError, UnicodeDecodeError):
                    raise LeadError(400, "invalid_json", "Malformed JSON or duplicate keys.")

                http_status, result = service.intake(body)
                if http_status == 202 and auto_process:
                    delivery = service.process_one(result["lead_id"])
                    if delivery:
                        result["lifecycle_status"] = delivery["lifecycle_status"]
                        if delivery.get("crm_id"):
                            result["crm_id"] = delivery["crm_id"]
                return self.reply(http_status, result)

            except LeadError as err:
                return self.reply(err.http_status, {"error": err.code, "reason": err.reason})
            except Exception:
                return self.reply(500, {"error": "internal_server_error"})

    return LeadHTTPHandler


def serve():
    keys = os.environ.get("SERVICE_KEYS", "")
    if not keys or any(len(k.strip()) < 16 for k in keys.split(",") if k.strip()):
        raise SystemExit("Set SERVICE_KEYS environment variable (>= 16 chars).")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    service = LeadService(
        data_dir=os.environ.get("DATA_DIR", "data"),
        mode=os.environ.get("MODE", "demo"),
        config=dict(os.environ),
    )
    host = os.environ.get("BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8788"))
    server = ThreadingHTTPServer((host, port), make_http_handler(service, keys))
    try:
        LOG.info("lead_service_started host=%s port=%d mode=%s", host, port, service.mode)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()
