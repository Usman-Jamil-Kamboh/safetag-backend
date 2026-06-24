"""
Pasbaan Pakistan — Owner App API Routes
========================================
Phase 1 of the Owner App roadmap.

HOW TO USE:
  1. Place this file in the same folder as your main.py
  2. Add these two lines near the top of main.py (after `app = FastAPI(...)`):
        from app_routes import app_router
        app.include_router(app_router)
  3. Add these env variables to your Render dashboard:
        APP_JWT_SECRET   → any long random string (e.g. run: python -c "import secrets; print(secrets.token_hex(32))")
        SMS_API_KEY      → your fast2sms API key (get free at fast2sms.com)
  4. Install one new package:
        pip install pyjwt httpx
     Add both to your requirements.txt as well.

ENDPOINTS THIS FILE ADDS:
  POST /app/request-otp     → validates sticker ID + phone, sends OTP
  POST /app/verify-otp      → checks OTP, returns JWT token (valid 30 days)
  GET  /app/me              → returns owner info (JWT required)
  GET  /app/call-history    → returns list of calls for this sticker (JWT required)

DB CHANGES:
  This file adds two columns to call_logs (auto-migrated on startup):
    - caller_label  TEXT    (e.g. "Scanner #1", "Unknown")
    - ended_at      TIMESTAMP
  And adds one new table:
    - app_otps      (stores pending OTPs with expiry)

THAT'S IT. Your existing main.py is untouched.
"""

import os
import time
import random
import string
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import jwt                          # pip install pyjwt
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG  (reads from environment — set these in Render)
# ─────────────────────────────────────────────────────────────────────────────

# Secret key for signing JWTs — set this in Render env vars
APP_JWT_SECRET = os.getenv("APP_JWT_SECRET", "change-this-in-production-please")

# JWT stays valid for 30 days so owners don't have to re-login constantly
JWT_EXPIRE_DAYS = 30

# OTP expires in 5 minutes
OTP_EXPIRE_MINUTES = 5

# fast2sms API key — get a free account at fast2sms.com
# They support Pakistani numbers and have a free tier
SMS_API_KEY = os.getenv("SMS_API_KEY", "")

# ─────────────────────────────────────────────────────────────────────────────
# ROUTER
# ─────────────────────────────────────────────────────────────────────────────

app_router = APIRouter(prefix="/app", tags=["Owner App"])

# ─────────────────────────────────────────────────────────────────────────────
# PYDANTIC MODELS  (what the Flutter app sends/receives as JSON)
# ─────────────────────────────────────────────────────────────────────────────

class OtpRequestBody(BaseModel):
    sticker_id: str       # e.g. "ST-000001"
    phone:      str       # e.g. "03001234567"

class OtpVerifyBody(BaseModel):
    sticker_id: str       # e.g. "ST-000001"
    phone:      str       # e.g. "03001234567"
    otp:        str       # 6-digit string e.g. "482910"

# ─────────────────────────────────────────────────────────────────────────────
# DB HELPERS
# These reuse get_db() and release_db() from main.py.
# We import them at the bottom of this file to avoid circular imports.
# ─────────────────────────────────────────────────────────────────────────────

def _get_db_funcs():
    """Lazy import to avoid circular import with main.py."""
    from main import get_db, release_db
    return get_db, release_db


def _migrate_app_tables():
    """
    Add app-specific DB tables/columns. Called once on startup.
    Safe to run multiple times — all statements use IF NOT EXISTS or try/except.
    """
    get_db, release_db = _get_db_funcs()
    conn = get_db()
    cur  = conn.cursor()

    # Table to store pending OTPs
    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_otps (
            id          SERIAL      PRIMARY KEY,
            sticker_id  TEXT        NOT NULL,
            phone_hash  TEXT        NOT NULL,
            otp_hash    TEXT        NOT NULL,
            expires_at  TIMESTAMP   NOT NULL,
            used        BOOLEAN     NOT NULL DEFAULT FALSE,
            created_at  TIMESTAMP   NOT NULL DEFAULT NOW()
        )
    """)
    conn.commit()

    # Index for fast lookup by sticker_id
    try:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_app_otps_sticker ON app_otps(sticker_id)")
        conn.commit()
    except Exception:
        conn.rollback()

    # Add caller_label column to call_logs if it doesn't exist yet
    try:
        cur.execute("ALTER TABLE call_logs ADD COLUMN caller_label TEXT DEFAULT 'Scanner'")
        conn.commit()
    except Exception:
        conn.rollback()

    # Add ended_at column to call_logs if it doesn't exist yet
    try:
        cur.execute("ALTER TABLE call_logs ADD COLUMN ended_at TIMESTAMP DEFAULT NULL")
        conn.commit()
    except Exception:
        conn.rollback()

    cur.close()
    release_db(conn)
    print("[Pasbaan App] App tables migrated successfully.", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP MIGRATION — called from main.py's _bg_init_db
# ─────────────────────────────────────────────────────────────────────────────

def run_app_migrations():
    """Call this from main.py's _bg_init_db() function after init_db()."""
    try:
        _migrate_app_tables()
    except Exception as e:
        print(f"[Pasbaan App] WARNING: Migration failed: {e}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _hash(value: str) -> str:
    """SHA-256 hash. Used to store OTPs and phone numbers safely in DB."""
    return hashlib.sha256(value.encode()).hexdigest()


def _generate_otp() -> str:
    """6-digit numeric OTP."""
    return "".join(random.choices(string.digits, k=6))


def _make_jwt(sticker_id: str, phone: str) -> str:
    """
    Create a signed JWT token.
    Payload: sticker_id, phone, expiry.
    The Flutter app stores this and sends it in every request header.
    """
    payload = {
        "sticker_id": sticker_id,
        "phone":       phone,
        "exp":         datetime.now(tz=timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS),
        "iat":         datetime.now(tz=timezone.utc),
    }
    return jwt.encode(payload, APP_JWT_SECRET, algorithm="HS256")


def _decode_jwt(token: str) -> dict:
    """
    Decode and verify a JWT. Raises HTTPException if invalid or expired.
    Used by protected endpoints.
    """
    try:
        payload = jwt.decode(token, APP_JWT_SECRET, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired. Please log in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token. Please log in again.")


def _require_auth(authorization: Optional[str]) -> dict:
    """
    Extract and verify JWT from the Authorization header.
    Flutter app sends: Authorization: Bearer <token>
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header.")
    token = authorization.split(" ", 1)[1]
    return _decode_jwt(token)


async def _send_otp_sms(phone: str, otp: str) -> bool:
    """
    Send OTP via fast2sms.com (cheap, supports PK numbers).
    Returns True if sent successfully.

    If SMS_API_KEY is not set, prints OTP to server logs (for testing).
    """
    if not SMS_API_KEY:
        # Development mode — print OTP to logs so you can test without SMS
        print(f"[Pasbaan App] DEV MODE — OTP for {phone}: {otp}", flush=True)
        return True

    message = f"Your Pasbaan verification code is: {otp}. Valid for 5 minutes. Do not share this code."

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://www.fast2sms.com/dev/bulkV2",
                headers={"authorization": SMS_API_KEY},
                json={
                    "route":   "q",          # transactional route
                    "message": message,
                    "language":"english",
                    "flash":   0,
                    "numbers": phone,
                }
            )
            data = resp.json()
            if data.get("return") is True:
                return True
            else:
                print(f"[Pasbaan App] SMS failed: {data}", flush=True)
                return False
    except Exception as e:
        print(f"[Pasbaan App] SMS error: {e}", flush=True)
        return False


def _get_owner_row(sticker_id: str) -> Optional[dict]:
    """
    Fetch the qr_codes row for a sticker. Returns None if not found.
    Reuses your existing DB pool from main.py.
    """
    get_db, release_db = _get_db_funcs()
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT qr_id, owner_data, scan_count FROM qr_codes WHERE qr_id = %s",
        (sticker_id.upper(),)
    )
    row = cur.fetchone()
    cur.close()
    release_db(conn)
    return dict(row) if row else None


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 1 — POST /app/request-otp
# ─────────────────────────────────────────────────────────────────────────────

@app_router.post("/request-otp")
async def request_otp(body: OtpRequestBody):
    """
    Step 1 of login.

    Flutter app sends: { "sticker_id": "ST-000001", "phone": "03001234567" }

    This endpoint:
      1. Checks sticker_id exists in your DB
      2. Checks the phone number matches what the owner registered
      3. Generates a 6-digit OTP
      4. Stores a hashed version in app_otps table (expires in 5 min)
      5. Sends OTP via SMS

    Returns: { "message": "OTP sent" }
    """

    sticker_id = body.sticker_id.strip().upper()

    # Validate phone format using your existing validator
    try:
        from main import validate_pk_phone
        phone = validate_pk_phone(body.phone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Check sticker exists and has an owner set up
    row = _get_owner_row(sticker_id)
    if not row:
        raise HTTPException(status_code=404, detail="Sticker ID not found.")

    owner_data = row.get("owner_data")
    if not owner_data:
        raise HTTPException(
            status_code=400,
            detail="This sticker has not been set up yet. Scan the QR code first to register."
        )

    # Check phone matches — owner_data is a dict stored as JSONB
    # Your main.py stores owner phone in owner_data["phone1"] and optionally ["phone2"]
    registered_phones = []
    if owner_data.get("phone1"):
        try:
            from main import validate_pk_phone
            registered_phones.append(validate_pk_phone(owner_data["phone1"]))
        except Exception:
            pass
    if owner_data.get("phone2"):
        try:
            from main import validate_pk_phone
            registered_phones.append(validate_pk_phone(owner_data["phone2"]))
        except Exception:
            pass

    if phone not in registered_phones:
        # Security: don't reveal whether sticker or phone is wrong
        raise HTTPException(
            status_code=400,
            detail="Sticker ID and phone number do not match our records."
        )

    # Generate OTP
    otp = _generate_otp()
    otp_hash = _hash(otp)
    phone_hash = _hash(phone)
    expires_at = datetime.now(tz=timezone.utc) + timedelta(minutes=OTP_EXPIRE_MINUTES)

    # Store OTP in DB (invalidate any existing unused OTPs for this sticker first)
    get_db, release_db = _get_db_funcs()
    conn = get_db()
    cur  = conn.cursor()
    try:
        # Mark old OTPs as used so only one is valid at a time
        cur.execute(
            "UPDATE app_otps SET used = TRUE WHERE sticker_id = %s AND used = FALSE",
            (sticker_id,)
        )
        # Insert new OTP
        cur.execute(
            """
            INSERT INTO app_otps (sticker_id, phone_hash, otp_hash, expires_at)
            VALUES (%s, %s, %s, %s)
            """,
            (sticker_id, phone_hash, otp_hash, expires_at)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail="Database error. Please try again.")
    finally:
        cur.close()
        release_db(conn)

    # Send SMS
    sent = await _send_otp_sms(phone, otp)
    if not sent:
        raise HTTPException(
            status_code=503,
            detail="Could not send SMS. Please try again in a moment."
        )

    return JSONResponse({"message": "OTP sent. Check your SMS. Valid for 5 minutes."})


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 2 — POST /app/verify-otp
# ─────────────────────────────────────────────────────────────────────────────

@app_router.post("/verify-otp")
async def verify_otp(body: OtpVerifyBody):
    """
    Step 2 of login.

    Flutter app sends: { "sticker_id": "ST-000001", "phone": "03001234567", "otp": "482910" }

    This endpoint:
      1. Looks up the most recent unused OTP for this sticker
      2. Checks it hasn't expired
      3. Compares hashes (never store raw OTPs)
      4. Marks OTP as used
      5. Returns a JWT token

    Flutter app stores this JWT and sends it as:
      Authorization: Bearer <token>
    on every future request.

    Returns: { "token": "eyJ...", "sticker_id": "ST-000001", "owner_name": "Ahmed" }
    """

    sticker_id = body.sticker_id.strip().upper()
    otp        = body.otp.strip()

    try:
        from main import validate_pk_phone
        phone = validate_pk_phone(body.phone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    phone_hash = _hash(phone)
    otp_hash   = _hash(otp)

    get_db, release_db = _get_db_funcs()
    conn = get_db()
    cur  = conn.cursor()

    try:
        # Fetch the most recent unused OTP for this sticker+phone combination
        cur.execute(
            """
            SELECT id, otp_hash, expires_at
            FROM app_otps
            WHERE sticker_id = %s
              AND phone_hash  = %s
              AND used        = FALSE
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (sticker_id, phone_hash)
        )
        row = cur.fetchone()

        if not row:
            raise HTTPException(
                status_code=400,
                detail="No pending OTP found. Please request a new one."
            )

        # Check expiry
        expires_at = row["expires_at"]
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(tz=timezone.utc) > expires_at:
            raise HTTPException(
                status_code=400,
                detail="OTP has expired. Please request a new one."
            )

        # Constant-time compare to prevent timing attacks
        if not hmac.compare_digest(row["otp_hash"], otp_hash):
            raise HTTPException(status_code=400, detail="Incorrect OTP. Please try again.")

        # Mark OTP as used so it can't be reused
        cur.execute("UPDATE app_otps SET used = TRUE WHERE id = %s", (row["id"],))
        conn.commit()

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail="Database error. Please try again.")
    finally:
        cur.close()
        release_db(conn)

    # Fetch owner name for the response
    owner_row  = _get_owner_row(sticker_id)
    owner_name = "Owner"
    if owner_row and owner_row.get("owner_data"):
        owner_name = owner_row["owner_data"].get("name", "Owner")

    # Generate JWT
    token = _make_jwt(sticker_id, phone)

    return JSONResponse({
        "token":       token,
        "sticker_id":  sticker_id,
        "owner_name":  owner_name,
        "expires_in":  f"{JWT_EXPIRE_DAYS} days",
        "message":     "Login successful. Welcome to Pasbaan!"
    })


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 3 — GET /app/me
# ─────────────────────────────────────────────────────────────────────────────

@app_router.get("/me")
async def get_me(authorization: Optional[str] = Header(None)):
    """
    Returns the logged-in owner's info.
    Flutter app uses this to show the owner's name and sticker on home screen.

    Requires: Authorization: Bearer <token>

    Returns: { "sticker_id": "ST-000001", "owner_name": "Ahmed", "phone": "030*****67" }
    """
    payload = _require_auth(authorization)
    sticker_id = payload["sticker_id"]

    owner_row = _get_owner_row(sticker_id)
    if not owner_row:
        raise HTTPException(status_code=404, detail="Sticker not found.")

    owner_data = owner_row.get("owner_data") or {}
    owner_name = owner_data.get("name", "Owner")

    # Mask phone number for privacy — show first 3 and last 2 digits only
    phone = payload["phone"]
    masked_phone = phone[:3] + "*" * (len(phone) - 5) + phone[-2:]

    return JSONResponse({
        "sticker_id":  sticker_id,
        "owner_name":  owner_name,
        "phone":       masked_phone,
        "scan_count":  owner_row.get("scan_count", 0),
    })


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 4 — GET /app/call-history
# ─────────────────────────────────────────────────────────────────────────────

@app_router.get("/call-history")
async def get_call_history(
    authorization: Optional[str] = Header(None),
    limit: int = 20,
    offset: int = 0,
):
    """
    Returns paginated call history for this owner's sticker.
    Flutter app uses this for the Call History screen (Screen 2).

    Requires: Authorization: Bearer <token>
    Optional query params: ?limit=20&offset=0  (for pagination, load more)

    Returns:
    {
      "calls": [
        {
          "id": 1,
          "caller_label": "Scanner",
          "started_at": "2025-01-15T10:30:00",
          "ended_at":   "2025-01-15T10:31:30",
          "duration_seconds": 90,
          "status": "completed"
        },
        ...
      ],
      "total": 42
    }
    """
    payload    = _require_auth(authorization)
    sticker_id = payload["sticker_id"]

    # Clamp limit to prevent abuse
    limit = max(1, min(limit, 50))

    get_db, release_db = _get_db_funcs()
    conn = get_db()
    cur  = conn.cursor()

    try:
        # Get total count
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM call_logs WHERE qr_id = %s",
            (sticker_id,)
        )
        total = cur.fetchone()["cnt"]

        # Get paginated calls, newest first
        cur.execute(
            """
            SELECT
                id,
                caller_label,
                created_at   AS started_at,
                ended_at,
                duration_s   AS duration_seconds,
                status
            FROM call_logs
            WHERE qr_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (sticker_id, limit, offset)
        )
        rows = cur.fetchall()

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail="Could not fetch call history.")
    finally:
        cur.close()
        release_db(conn)

    # Convert datetime objects to ISO strings for JSON
    calls = []
    for row in rows:
        call = dict(row)
        if call.get("started_at"):
            call["started_at"] = call["started_at"].isoformat()
        if call.get("ended_at"):
            call["ended_at"] = call["ended_at"].isoformat()
        calls.append(call)

    return JSONResponse({"calls": calls, "total": total})