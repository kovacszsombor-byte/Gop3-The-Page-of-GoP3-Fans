# server/app.py
#
# GOP3 Fan Page - Python (Flask) backend example
# Purpose:
# - Provide endpoints to receive contact form submissions from the front-end.
# - Send e-mails via SMTP (configured from environment variables).
# - Accept JSON and multipart/form-data (with optional file attachments).
# - Include logging, basic rate-limiting, validation, retry logic and health check.
# - This file is intentionally verbose and commented to meet the ">=300 lines" request.
#
# Usage (development):
# 1. Create virtualenv and install dependencies:
#    python3 -m venv venv
#    source venv/bin/activate
#    pip install Flask flask-cors python-dotenv
#
# 2. Set environment variables (examples):
#    export SMTP_HOST=smtp.example.com
#    export SMTP_PORT=587
#    export SMTP_USER=your_smtp_user
#    export SMTP_PASS=your_smtp_password
#    export SMTP_FROM="GOP3 Fan <no-reply@example.com>"
#    export SMTP_TO="site-owner@example.com"
#
# 3. Run:
#    export FLASK_APP=server/app.py
#    flask run --host=0.0.0.0 --port=5000
#
# Security notes:
# - Never commit real credentials.
# - For production use, run behind HTTPS, use a proper email service provider, add CAPTCHA and stronger rate limiting / auth.
#

import os
import io
import time
import json
import logging
import smtplib
import threading
from email.message import EmailMessage
from datetime import datetime
from typing import Optional, Dict, Any
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.utils import secure_filename

# ----------------------------
# Configuration and constants
# ----------------------------
APP_NAME = "GOP3 Fan Page Backend (Flask)"
VERSION = "1.0.0"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# SMTP configuration read from environment variables
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "").strip()
SMTP_FROM = os.environ.get("SMTP_FROM", "gop3-fan@example.com")
SMTP_TO = os.environ.get("SMTP_TO", SMTP_FROM)  # destination; can be same as from in test

# Limits and settings
MAX_ATTACHMENT_SIZE = 8 * 1024 * 1024  # 8 MiB per attachment
ALLOWED_ATTACHMENT_EXT = {"png", "jpg", "jpeg", "gif", "pdf", "txt", "md"}
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 6  # max submissions per IP per window

# Temp upload folder (in-memory usage recommended; we simply keep attachments in memory)
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "/tmp/gop3_uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("gop3-server")

# ----------------------------
# Flask app initialization
# ----------------------------
app = Flask(__name__, static_folder=None)
CORS(app, resources={r"/*": {"origins": "*"}})  # Allow CORS for testing (restrict in prod)

# ----------------------------
# Simple in-memory rate limiter
# ----------------------------
# This is not cluster-safe. For production use a distributed store (Redis).
_rate_lock = threading.Lock()
_rate_store: Dict[str, Dict[str, Any]] = {}  # { ip: { "count": int, "window_start": timestamp } }


def is_rate_limited(ip: str) -> bool:
    """Check and update rate limiter for the given IP. Returns True if rate limited."""
    now = int(time.time())
    with _rate_lock:
        data = _rate_store.get(ip)
        if not data:
            _rate_store[ip] = {"count": 1, "window_start": now}
            logger.debug("Rate limiter: new ip %s", ip)
            return False
        # If window expired -> reset
        if now - data["window_start"] > RATE_LIMIT_WINDOW:
            _rate_store[ip] = {"count": 1, "window_start": now}
            logger.debug("Rate limiter: reset ip %s", ip)
            return False
        # Window still active
        if data["count"] >= RATE_LIMIT_MAX:
            logger.warning("Rate limiter: ip %s exceeded limit", ip)
            return True
        data["count"] += 1
        logger.debug("Rate limiter: ip %s count %s", ip, data["count"])
        return False


# ----------------------------
# Utilities
# ----------------------------
def allowed_filename(filename: str) -> bool:
    """Check allowed extension."""
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_ATTACHMENT_EXT


def send_email(subject: str, body: str, reply_to: Optional[str] = None,
               attachments: Optional[Dict[str, bytes]] = None, max_attempts: int = 3) -> None:
    """
    Send an email via SMTP with optional attachments.
    attachments: dict mapping filename -> bytes
    Retries on failure with exponential backoff.
    Raises exception on failure.
    """
    attempts = 0
    last_exc = None
    while attempts < max_attempts:
        attempts += 1
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = SMTP_FROM
            msg["To"] = SMTP_TO
            if reply_to:
                msg["Reply-To"] = reply_to
            msg.set_content(body)

            # Attach files if provided
            if attachments:
                for fname, content in attachments.items():
                    maintype = "application"
                    subtype = "octet-stream"
                    # best effort to guess common types by extension
                    if fname.lower().endswith((".png", ".jpg", ".jpeg", ".gif")):
                        maintype = "image"
                        subtype = "png" if fname.lower().endswith(".png')") else ("jpeg" if fname.lower().endswith((".jpg", ".jpeg")) else "gif")
                    elif fname.lower().endswith(".pdf"):
                        maintype = "application"
                        subtype = "pdf"
                    elif fname.lower().endswith(".txt"):
                        maintype = "text"
                        subtype = "plain"

                    try:
                        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=fname)
                    except TypeError:
                        # Some Python versions expect bytes and maintype/subtype strings
                        msg.add_attachment(content, filename=fname)

            logger.info("Connecting to SMTP %s:%s (attempt %d)", SMTP_HOST, SMTP_PORT, attempts)
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
                smtp.ehlo()
                # Try STARTTLS
                try:
                    smtp.starttls()
                    smtp.ehlo()
                except Exception as e:
                    logger.debug("STARTTLS not available or failed: %s", e)
                if SMTP_USER and SMTP_PASS:
                    smtp.login(SMTP_USER, SMTP_PASS)
                smtp.send_message(msg)
            logger.info("Email sent successfully to %s", SMTP_TO)
            return
        except Exception as e:
            last_exc = e
            logger.exception("Failed to send email on attempt %d: %s", attempts, e)
            # simple backoff
            time.sleep(1.0 * (2 ** (attempts - 1)))
    # If we reach here, all attempts failed
    raise RuntimeError(f"Failed to send email after {max_attempts} attempts") from last_exc


def safe_text(value: Optional[str]) -> str:
    """Return a trimmed, safe string for inclusion in emails."""
    if not value:
        return ""
    return str(value).strip()


def build_email_body(data: Dict[str, Any]) -> str:
    """Create a plain-text email body from received form data."""
    lines = []
    lines.append("New message from GOP3 Fan Page contact form")
    lines.append("")
    lines.append(f"Sent at: {datetime.utcnow().isoformat()}Z")
    lines.append("")
    lines.append("----")
    name = safe_text(data.get("name"))
    email = safe_text(data.get("email"))
    subject = safe_text(data.get("subject"))
    message = safe_text(data.get("message"))
    if name:
        lines.append(f"Name: {name}")
    if email:
        lines.append(f"Email: {email}")
    if subject:
        lines.append(f"Subject: {subject}")
    lines.append("")
    lines.append("Message:")
    lines.append(message or "(no message)")
    lines.append("")
    lines.append("----")
    # include raw payload if present for debugging (sanitized)
    if "meta" in data:
        lines.append("Meta:")
        lines.append(json.dumps(data.get("meta"), indent=2))
    return "\n".join(lines)


# ----------------------------
# Routes
# ----------------------------
@app.route("/", methods=["GET"])
def index():
    return jsonify(status="ok", message=f"{APP_NAME} v{VERSION}"), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="healthy", time=datetime.utcnow().isoformat()), 200


@app.route("/send-email", methods=["POST"])
def receive_send_email():
    """
    Accepts:
    - JSON application/json (payload contains name,email,subject,message)
    - multipart/form-data (fields + optional file uploads)
    Validates input, enforces rate limiting, and attempts to send an email.
    """
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
    logger.info("Incoming /send-email request from %s", ip)

    # Rate limit
    if is_rate_limited(ip):
        logger.warning("Rejecting request from %s due to rate limit", ip)
        return jsonify(error="Too many requests. Try again later."), 429

    # Parse input
    payload = {}
    attachments = {}

    # Accept JSON
    if request.content_type and request.content_type.startswith("application/json"):
        try:
            payload = request.get_json(force=True)
            if payload is None:
                raise ValueError("Empty JSON")
        except Exception as e:
            logger.debug("Invalid JSON payload: %s", e)
            return jsonify(error="Invalid JSON payload"), 400
    else:
        # Support form-encoded and multipart/form-data
        payload["name"] = request.form.get("name")
        payload["email"] = request.form.get("email")
        payload["subject"] = request.form.get("subject")
        payload["message"] = request.form.get("message")
        # support a meta JSON field
        meta = request.form.get("meta")
        if meta:
            try:
                payload["meta"] = json.loads(meta)
            except Exception:
                payload["meta"] = meta

        # handle file attachments (optional)
        for key in request.files:
            file_storage = request.files.get(key)
            if file_storage and file_storage.filename:
                fname = secure_filename(file_storage.filename)
                if not allowed_filename(fname):
                    logger.warning("Rejected attachment with disallowed extension: %s", fname)
                    return jsonify(error=f"Attachment type not allowed: {fname}"), 400
                file_storage.seek(0, io.SEEK_END)
                size = file_storage.tell()
                file_storage.seek(0)
                if size > MAX_ATTACHMENT_SIZE:
                    logger.warning("Rejected attachment too large: %s (%s bytes)", fname, size)
                    return jsonify(error=f"Attachment too large: {fname}"), 400
                content = file_storage.read()
                attachments[fname] = content
                logger.info("Received attachment %s (%d bytes)", fname, len(content))

    # Minimal validation
    name = safe_text(payload.get("name"))
    email = safe_text(payload.get("email"))
    subject = safe_text(payload.get("subject") or "GOP3 Fan Message")
    message = safe_text(payload.get("message"))
    if not (name and email and message):
        logger.debug("Validation failed: name/email/message required: %s", {"name": name, "email": email, "message_len": len(message)})
        return jsonify(error="Missing required fields: name, email, and message are required"), 422

    # Build email
    body = build_email_body({
        "name": name,
        "email": email,
        "subject": subject,
        "message": message,
        "meta": payload.get("meta")
    })

    # Attempt to send
    try:
        send_email(f"[GOP3 Fan] {subject} (from {name})", body, reply_to=email, attachments=attachments or None)
        logger.info("Processed /send-email from %s <%s>", name, email)
        return jsonify(message="Email sent successfully"), 200
    except Exception as e:
        logger.exception("Error sending email: %s", e)
        return jsonify(error="Failed to send email", detail=str(e)), 500


# ----------------------------
# Static helper: optional - serve a small test page if needed
# ----------------------------
@app.route("/_test_page", methods=["GET"])
def test_page():
    """
    A minimal HTML test page for manual testing. Not required in production.
    """
    html = f"""
    <!doctype html>
    <html>
      <head><meta charset="utf-8"><title>{APP_NAME} - Test</title></head>
      <body>
        <h1>{APP_NAME} v{VERSION}</h1>
        <p>Use the POST /send-email endpoint to send messages.</p>
        <form method="post" action="/send-email" enctype="multipart/form-data">
          <label>Name: <input name="name" required></label><br>
          <label>Email: <input name="email" required></label><br>
          <label>Subject: <input name="subject"></label><br>
          <label>Message: <textarea name="message" required></textarea></label><br>
          <label>Attachment: <input type="file" name="file1"></label><br>
          <button type="submit">Send</button>
        </form>
      </body>
    </html>
    """
    return html, 200


# ----------------------------
# Background maintenance (cleanup rate limiter periodically)
# ----------------------------
def _cleanup_rate_store():
    """Cleanup old entries periodically to prevent memory growth."""
    while True:
        time.sleep(RATE_LIMIT_WINDOW * 2)
        cutoff = int(time.time()) - (RATE_LIMIT_WINDOW * 2)
        with _rate_lock:
            keys_to_delete = [k for k, v in _rate_store.items() if v.get("window_start", 0) < cutoff]
            for k in keys_to_delete:
                del _rate_store[k]
        logger.debug("Rate store cleanup removed %d entries", len(keys_to_delete))


_cleanup_thread = threading.Thread(target=_cleanup_rate_store, daemon=True)
_cleanup_thread.start()

# ----------------------------
# If run as main for local dev
# ----------------------------
if __name__ == "__main__":
    logger.info("Starting %s (debug mode)", APP_NAME)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=os.environ.get("FLASK_DEBUG", "1") == "1")
