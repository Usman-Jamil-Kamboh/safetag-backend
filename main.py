"""
Pasbaan Pakistan — FastAPI Backend (PostgreSQL + Sequential IDs + Admin UI)
===========================================================================

QR ID FORMAT:  ST-000001, ST-000002, ST-000003 ... auto-increments forever
ADMIN UI:      GET /admin  → beautiful login + dashboard (no browser tools needed)
SCAN LOGIC:    scan_count==0 → plan selection → owner setup | scan_count>=1 → contact page

PLANS:
  basic   — Rs. 399/6 months — public phone numbers shown on contact page
  premium — Rs. 399/6 months + call packs — masked calling via Twilio (coming soon)
"""

import hashlib, hmac, io, json, os, secrets, sys, time, zipfile
from typing import Optional
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Form, HTTPException, Request, Cookie
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles

import qrcode
import qrcode.constants
from PIL import Image, ImageDraw, ImageFont

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

DATABASE_URL    = os.getenv("DATABASE_URL")
BASE_URL        = os.getenv("BASE_URL",  "http://localhost:8000")
ADMIN_KEY       = os.getenv("ADMIN_KEY")
if not ADMIN_KEY:
    raise RuntimeError("ADMIN_KEY environment variable is not set.")


if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set.")

os.makedirs("static",    exist_ok=True)
os.makedirs("templates", exist_ok=True)

# In-memory session store  {token: expiry_datetime}
SESSIONS = {}  # type: dict
SESSION_HOURS = 8

# ─────────────────────────────────────────────────────────────────────────────
# PIN HELPERS
# ─────────────────────────────────────────────────────────────────────────────

# Secret used to sign owner-session cookies (pin verified tokens)
PIN_COOKIE_SECRET = os.getenv("PIN_COOKIE_SECRET", secrets.token_hex(32))

# ─────────────────────────────────────────────────────────────────────────────
# MANUAL PAYMENT CONFIG  (update these in Render environment variables)
# ─────────────────────────────────────────────────────────────────────────────
OWNER_JAZZCASH   = os.getenv("OWNER_JAZZCASH",   "03XX-XXXXXXX")   # your JazzCash number
OWNER_EASYPAISA  = os.getenv("OWNER_EASYPAISA",  "03XX-XXXXXXX")   # your Easypaisa number
OWNER_WHATSAPP   = os.getenv("OWNER_WHATSAPP",   "923XXXXXXXXX")   # your WhatsApp (no +, no dashes)
OWNER_NAME       = os.getenv("OWNER_NAME",        "Pasbaan Support")

SUBSCRIPTION_PRICE = 399   # PKR per 6 months

# Call pack definitions: label -> (calls, price_pkr)
CALL_PACKS = {
    "starter":  (20,  400),
    "standard": (50,  1000),
    "popular":  (100, 2000),
    "heavy":    (200, 4000),
}
# Cookie lasts 2 hours — enough to fill the update form without staying logged in forever
PIN_SESSION_HOURS = 2


def hash_pin(pin: str) -> str:
    """SHA-256 hash of the 4-digit PIN. Stored in owner_data JSON."""
    return hashlib.sha256(pin.encode()).hexdigest()


def verify_pin(raw_pin: str, stored_hash: str) -> bool:
    """Constant-time comparison to prevent timing attacks."""
    return hmac.compare_digest(hash_pin(raw_pin), stored_hash)


def make_pin_token(qr_id: str) -> str:
    """
    Create a signed token:  {qr_id}:{expiry_unix}:{hmac}
    Stored in the 'owner_session' cookie after correct PIN entry.
    """
    expiry = int(time.time()) + PIN_SESSION_HOURS * 3600
    msg    = f"{qr_id}:{expiry}"
    sig    = hmac.new(PIN_COOKIE_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}:{sig}"


def verify_pin_token(token: str, qr_id: str) -> bool:
    """Return True if the cookie token is valid and not expired for this qr_id."""
    if not token:
        return False
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return False
        tok_id, expiry_str, sig = parts
        if tok_id != qr_id:
            return False
        if int(time.time()) > int(expiry_str):
            return False
        msg      = f"{tok_id}:{expiry_str}"
        expected = hmac.new(PIN_COOKIE_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False

import re

PK_MOBILE_RE = re.compile(
    r"^(?:\+92|0092|92|0)"   # country code or leading zero
    r"(3[0-9]{2})"           # network prefix: 300-399
    r"[0-9]{7}$"             # 7 subscriber digits
)

def validate_pk_phone(raw: str) -> str:
    """
    Validate and normalise a Pakistani mobile number.
    Returns cleaned digits (e.g. '03001234567') or raises ValueError.
    Accepts: 03xx-xxxxxxx, +923xxxxxxxxx, 923xxxxxxxxx, 003xx-xxxxxxx
    """
    cleaned = re.sub(r"[\s\-\(\)]", "", raw.strip())
    if not PK_MOBILE_RE.match(cleaned):
        raise ValueError(
            f"'{raw}' is not a valid Pakistani mobile number. "
            "Format must be 03XX-XXXXXXX or +923XXXXXXXXX."
        )
    # Normalise to 0-prefix local format
    if cleaned.startswith("+92"):
        cleaned = "0" + cleaned[3:]
    elif cleaned.startswith("0092"):
        cleaned = "0" + cleaned[4:]
    elif cleaned.startswith("92") and not cleaned.startswith("0"):
        cleaned = "0" + cleaned[2:]
    return cleaned

# ─────────────────────────────────────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────────────────────────────────────

from contextlib import asynccontextmanager
import threading

def _bg_init_db(retries: int = 15, delay: float = 3.0):
    """Run init_pool + init_db in a background thread so uvicorn is not blocked."""
    # Step 1: create the connection pool
    init_pool(retries=retries, delay=delay)
    # Step 2: run schema migrations
    for attempt in range(1, retries + 1):
        try:
            init_db()
            print("[Pasbaan] Database initialised successfully.", file=sys.stderr)
            return
        except Exception as _e:
            print(f"[Pasbaan] init_db attempt {attempt}/{retries} failed: {_e}", file=sys.stderr)
            if attempt < retries:
                time.sleep(delay)
    print("[Pasbaan] WARNING: DB init failed after all retries.", file=sys.stderr)

@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=_bg_init_db, daemon=True).start()
    yield

app = FastAPI(title="Pasbaan Pakistan", version="3.0.0", docs_url="/api-docs", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="static"), name="static")


# Permissions-Policy: allow geolocation so browsers don't block it
from fastapi import Response as _Resp
from starlette.middleware.base import BaseHTTPMiddleware

class PermissionsPolicyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Permissions-Policy"] = "geolocation=(*)"
        return response

app.add_middleware(PermissionsPolicyMiddleware)


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────

from psycopg2 import pool as _pg_pool

_db_pool: "_pg_pool.ThreadedConnectionPool | None" = None


def init_pool(retries: int = 10, delay: float = 3.0):
    """Create the global connection pool. Retried in background at startup."""
    global _db_pool
    for attempt in range(1, retries + 1):
        try:
            _db_pool = _pg_pool.ThreadedConnectionPool(
                minconn=2,
                maxconn=10,          # safe for Supabase free tier (max ~20)
                dsn=DATABASE_URL,
                cursor_factory=psycopg2.extras.RealDictCursor,
                connect_timeout=10,
            )
            print("[Pasbaan] Connection pool created.", file=sys.stderr)
            return
        except Exception as e:
            print(f"[Pasbaan] Pool init attempt {attempt}/{retries} failed: {e}", file=sys.stderr)
            if attempt < retries:
                time.sleep(delay)
    print("[Pasbaan] WARNING: Could not create DB pool after all retries.", file=sys.stderr)


def get_db():
    """Borrow a connection from the pool."""
    if _db_pool is None:
        # Fallback for edge cases during startup
        return psycopg2.connect(
            DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor,
            connect_timeout=10,
        )
    return _db_pool.getconn()


def release_db(conn):
    """Return a connection to the pool (or close it if pool is gone)."""
    if _db_pool is None:
        try:
            conn.close()
        except Exception:
            pass
        return
    try:
        _db_pool.putconn(conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def init_db():
    """
    Safe idempotent setup — runs on every startup.
    Each statement is wrapped so one failure does not block others.
    """
    conn = get_db()
    cur  = conn.cursor()

    # 1. Create qr_codes table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS qr_codes (
            qr_id        TEXT      PRIMARY KEY,
            scan_count   INTEGER   NOT NULL DEFAULT 0,
            owner_data   JSONB     DEFAULT NULL,
            theme        TEXT      NOT NULL DEFAULT 'classic',
            created_at   TIMESTAMP NOT NULL DEFAULT NOW(),
            activated_at TIMESTAMP DEFAULT NULL
        )
    """)
    conn.commit()

    # 2. Add theme column if missing (safe on older deployments)
    try:
        cur.execute("ALTER TABLE qr_codes ADD COLUMN theme TEXT NOT NULL DEFAULT 'classic'")
        conn.commit()
    except Exception:
        conn.rollback()   # column already exists — that is fine

    # 2b. Add owner_pin column if missing (safe on older deployments)
    try:
        cur.execute("ALTER TABLE qr_codes ADD COLUMN owner_pin TEXT DEFAULT NULL")
        conn.commit()
    except Exception:
        conn.rollback()   # column already exists — that is fine

    # 2c. Add is_active column if missing (safe on older deployments)
    try:
        cur.execute("ALTER TABLE qr_codes ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE")
        conn.commit()
    except Exception:
        conn.rollback()   # column already exists — that is fine

    # 3. Create counter table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS qr_counter (
            id       INTEGER PRIMARY KEY,
            next_num INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.commit()

    # 4. Seed counter row
    cur.execute("""
        INSERT INTO qr_counter (id, next_num)
        VALUES (1, 1)
        ON CONFLICT (id) DO NOTHING
    """)
    conn.commit()

    # 5. Create subscriptions table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id           SERIAL    PRIMARY KEY,
            qr_id        TEXT      NOT NULL REFERENCES qr_codes(qr_id) ON DELETE CASCADE,
            plan         TEXT      NOT NULL DEFAULT 'basic',
            status       TEXT      NOT NULL DEFAULT 'trial',
            started_at   TIMESTAMP NOT NULL DEFAULT NOW(),
            expires_at   TIMESTAMP DEFAULT NULL,
            created_at   TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    conn.commit()

    # 6. Create call_packs table (Premium only — future use)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS call_packs (
            id              SERIAL    PRIMARY KEY,
            qr_id           TEXT      NOT NULL REFERENCES qr_codes(qr_id) ON DELETE CASCADE,
            calls_purchased INTEGER   NOT NULL DEFAULT 0,
            calls_remaining INTEGER   NOT NULL DEFAULT 0,
            purchased_at    TIMESTAMP NOT NULL DEFAULT NOW(),
            pack_label      TEXT      DEFAULT NULL
        )
    """)
    conn.commit()

    # 7. Create call_logs table (Premium only — future use)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS call_logs (
            id          SERIAL    PRIMARY KEY,
            qr_id       TEXT      NOT NULL,
            contact_idx INTEGER   NOT NULL DEFAULT 0,
            duration_s  INTEGER   DEFAULT NULL,
            status      TEXT      DEFAULT 'initiated',
            created_at  TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    conn.commit()

    # 8. Create payments table — tracks manual payment confirmations
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id           SERIAL    PRIMARY KEY,
            qr_id        TEXT      NOT NULL,
            payment_type TEXT      NOT NULL DEFAULT 'subscription',
            amount_pkr   INTEGER   NOT NULL,
            method       TEXT      DEFAULT NULL,
            status       TEXT      NOT NULL DEFAULT 'pending',
            confirmed_at TIMESTAMP DEFAULT NULL,
            notes        TEXT      DEFAULT NULL,
            created_at   TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    conn.commit()

    cur.close()
    release_db(conn)

# DB init runs inside FastAPI lifespan so uvicorn starts immediately


def db_next_seq() -> int:
    """Atomically get and increment the global QR counter. Thread-safe."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE qr_counter SET next_num = next_num + 1
        WHERE id = 1
        RETURNING next_num
    """)
    row = cur.fetchone()
    num = row["next_num"] - 1   # subtract 1 to get the value BEFORE increment
    conn.commit()
    cur.close()
    release_db(conn)
    return num


def db_get_record(qr_id: str):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM qr_codes WHERE qr_id = %s", (qr_id,))
    row = cur.fetchone()
    cur.close()
    release_db(conn)
    return dict(row) if row else None


def db_increment_scan(qr_id: str):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("UPDATE qr_codes SET scan_count = scan_count + 1 WHERE qr_id = %s", (qr_id,))
    conn.commit()
    cur.close()
    release_db(conn)


def db_save_owner(qr_id: str, payload: dict, pin_hash: str):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE qr_codes SET owner_data=%s, owner_pin=%s, activated_at=NOW() WHERE qr_id=%s",
        (json.dumps(payload), pin_hash, qr_id)
    )
    conn.commit()
    cur.close()
    release_db(conn)


def db_insert_qr(qr_id: str, theme: str = "classic"):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("INSERT INTO qr_codes (qr_id, theme) VALUES (%s, %s)", (qr_id, theme))
    conn.commit()
    cur.close()
    release_db(conn)


def db_list_all(page: int = 1, per_page: int = 25):
    """Return paginated QR codes and total count."""
    conn   = get_db()
    cur    = conn.cursor()
    offset = (page - 1) * per_page
    cur.execute(
        "SELECT qr_id, scan_count, activated_at, created_at, theme "
        "FROM qr_codes ORDER BY created_at DESC LIMIT %s OFFSET %s",
        (per_page, offset),
    )
    rows = cur.fetchall()
    cur.execute("SELECT COUNT(*) AS cnt FROM qr_codes")
    total = int(cur.fetchone()["cnt"])
    cur.close()
    release_db(conn)
    return [dict(r) for r in rows], total


def db_delete_qr(qr_id: str):
    """Permanently delete a QR code record."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("DELETE FROM qr_codes WHERE qr_id = %s", (qr_id,))
    conn.commit()
    cur.close()
    release_db(conn)


def db_reset_qr(qr_id: str):
    """Reset a QR code to unclaimed state (keeps the sticker ID, clears owner data)."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE qr_codes SET owner_data=NULL, owner_pin=NULL, activated_at=NULL, scan_count=0 WHERE qr_id=%s",
        (qr_id,),
    )
    conn.commit()
    cur.close()
    release_db(conn)




def db_update_owner(qr_id: str, payload: dict, pin_hash: str = None):
    """Update owner data. If pin_hash provided, update PIN too."""
    conn = get_db()
    cur  = conn.cursor()
    if pin_hash:
        cur.execute(
            "UPDATE qr_codes SET owner_data=%s, owner_pin=%s WHERE qr_id=%s",
            (json.dumps(payload), pin_hash, qr_id),
        )
    else:
        cur.execute(
            "UPDATE qr_codes SET owner_data=%s WHERE qr_id=%s",
            (json.dumps(payload), qr_id),
        )
    conn.commit()
    cur.close()
    release_db(conn)


def db_get_subscription(qr_id: str):
    """Return the active subscription row for a qr_id, or None."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT * FROM subscriptions WHERE qr_id = %s ORDER BY created_at DESC LIMIT 1",
        (qr_id,)
    )
    row = cur.fetchone()
    cur.close()
    release_db(conn)
    return dict(row) if row else None


def db_create_subscription(qr_id: str, plan: str = "basic"):
    """Create a pending_payment subscription for a newly activated QR code."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        """INSERT INTO subscriptions (qr_id, plan, status, started_at, expires_at)
           VALUES (%s, %s, 'pending_payment', NOW(), NULL)
           ON CONFLICT DO NOTHING""",
        (qr_id, plan)
    )
    conn.commit()
    cur.close()
    release_db(conn)


def db_activate_subscription(qr_id: str):
    """Admin manually activates a subscription — sets active + 180 day expiry from now."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        """UPDATE subscriptions
           SET status='active',
               started_at=NOW(),
               expires_at=NOW() + INTERVAL '180 days'
           WHERE qr_id=%s""",
        (qr_id,)
    )
    conn.commit()
    cur.close()
    release_db(conn)


def db_deactivate_subscription(qr_id: str):
    """Admin suspends a subscription (e.g. non-payment on renewal)."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE subscriptions SET status='suspended' WHERE qr_id=%s",
        (qr_id,)
    )
    conn.commit()
    cur.close()
    release_db(conn)


def db_get_all_subscriptions_for_admin() -> list:
    """Return all subscriptions with qr info for admin dashboard."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT s.*, q.owner_data, q.scan_count
        FROM subscriptions s
        LEFT JOIN qr_codes q ON q.qr_id = s.qr_id
        ORDER BY s.created_at DESC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    release_db(conn)
    return rows


def db_get_call_credits(qr_id: str) -> int:
    """Return total remaining call credits across all packs for this qr_id."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(calls_remaining), 0) AS total FROM call_packs WHERE qr_id = %s",
        (qr_id,)
    )
    row = cur.fetchone()
    cur.close()
    release_db(conn)
    return int(row["total"]) if row else 0


def db_stats() -> dict:
    """Run all stats in ONE query to avoid multi-cursor issues."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT
            (SELECT COUNT(*)                         FROM qr_codes) AS total,
            (SELECT COUNT(*) FROM qr_codes           WHERE activated_at IS NOT NULL) AS active,
            (SELECT COALESCE(SUM(scan_count),0)      FROM qr_codes) AS scans,
            (SELECT COALESCE(next_num, 1)            FROM qr_counter WHERE id = 1) AS nxt
    """)
    row    = cur.fetchone()
    cur.close()
    release_db(conn)
    total  = int(row["total"]  or 0)
    active = int(row["active"] or 0)
    scans  = int(row["scans"]  or 0)
    nxt    = int(row["nxt"]    or 1)
    return {
        "total":       total,
        "active":      active,
        "unclaimed":   total - active,
        "total_scans": scans,
        "next_id":     f"ST-{nxt:06d}",
    }


# ─────────────────────────────────────────────────────────────────────────────
# SESSION AUTH
# ─────────────────────────────────────────────────────────────────────────────

def create_session() -> str:
    token = secrets.token_hex(32)
    SESSIONS[token] = datetime.utcnow() + timedelta(hours=SESSION_HOURS)
    return token


def is_valid_session(token) -> bool:
    if not token:
        return False
    expiry = SESSIONS.get(token)
    if not expiry or datetime.utcnow() > expiry:
        SESSIONS.pop(token, None)
        return False
    return True


def require_admin(session):
    if not is_valid_session(session):
        raise HTTPException(status_code=303, headers={"Location": "/admin"})


# ─────────────────────────────────────────────────────────────────────────────
# COLOUR THEMES FOR STICKERS
# ─────────────────────────────────────────────────────────────────────────────

THEMES = {
    "yellow": {
        "label":     "Classic Yellow",
        "panel_rgb": (255, 193,   7),
        "title_rgb": ( 17,  17,  17),
        "body_rgb":  (  0,   0,   0),
        "sub_rgb":   (102, 102, 102),
        "id_rgb":    (  0,   0,   0),
        "preview":   "#FFC107",
    },
    "classic": {
        "label":     "Classic Black",
        "panel_rgb": (  8,   8,   8),
        "title_rgb": ( 17,  17,  17),
        "body_rgb":  (  0,   0,   0),
        "sub_rgb":   (102, 102, 102),
        "id_rgb":    (255, 255, 255),
        "preview":   "#1a1a1a",
    },
    "navy": {
        "label":     "Navy Blue",
        "panel_rgb": ( 10,  36,  82),
        "title_rgb": ( 17,  17,  17),
        "body_rgb":  (  0,   0,   0),
        "sub_rgb":   (102, 102, 102),
        "id_rgb":    (255, 255, 255),
        "preview":   "#0a2452",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# STICKER GENERATION
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# STICKER GENERATION  —  de.html design rendered with Pillow
# SVG viewBox 500×310 pts, rendered at SCALE=3 → 1500×930 px output
# ─────────────────────────────────────────────────────────────────────────────

_STICKER_SCALE = 3          # pixels per SVG pt
_STK_W = 500 * _STICKER_SCALE   # 1500
_STK_H = 310 * _STICKER_SCALE   # 930

# ── Font bootstrap ────────────────────────────────────────────────────────────
# Downloads Poppins + DejaVuSansMono from Google Fonts / GitHub on first run
# and caches them in ./static/fonts/ so the sticker always renders correctly
# regardless of what fonts are installed on the host system.
# ──────────────────────────────────────────────────────────────────────────────

_FONTS_DIR = os.path.join("static", "fonts")
os.makedirs(_FONTS_DIR, exist_ok=True)

_FONT_SOURCES = {
    # Multiple mirror URLs per font — tried in order until one succeeds
    "Poppins-Bold.ttf": [
        "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Bold.ttf",
        "https://fonts.gstatic.com/s/poppins/v21/pxiByp8kv8JHgFVrLCz7Z1xlFQ.woff2",  # woff2 fallback
    ],
    "Poppins-Regular.ttf": [
        "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Regular.ttf",
        "https://fonts.gstatic.com/s/poppins/v21/pxiEyp8kv8JHgFVrJJfecg.woff2",
    ],
    "DejaVuSansMono-Bold.ttf": [
        # dompdf mirror — stable, direct TTF download
        "https://raw.githubusercontent.com/dompdf/dompdf/master/lib/fonts/DejaVuSansMono-Bold.ttf",
        # Official dejavu-fonts release tarball (v2.37) — fallback
        "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.tar.bz2",
    ],
}


def _ensure_fonts():
    """Download any missing fonts to static/fonts/ (runs once, silently)."""
    import urllib.request
    import tarfile

    for fname, urls in _FONT_SOURCES.items():
        dest = os.path.join(_FONTS_DIR, fname)
        if os.path.exists(dest) and os.path.getsize(dest) > 10_000:
            continue  # already cached and valid
        for url in urls:
            try:
                print(f"[Pasbaan] Downloading font {fname} from {url} …", file=sys.stderr)
                # Handle .tar.bz2 release archives — extract just the one font file
                if url.endswith(".tar.bz2"):
                    tmp = dest + ".tar.bz2"
                    urllib.request.urlretrieve(url, tmp)
                    with tarfile.open(tmp, "r:bz2") as tar:
                        for member in tar.getmembers():
                            if member.name.endswith(fname):
                                member.name = os.path.basename(member.name)
                                tar.extract(member, _FONTS_DIR)
                                break
                    os.remove(tmp)
                else:
                    urllib.request.urlretrieve(url, dest)
                size = os.path.getsize(dest) if os.path.exists(dest) else 0
                if size > 10_000:
                    print(f"[Pasbaan] Font {fname} ready ({size} bytes)", file=sys.stderr)
                    break  # success — move on to next font
                else:
                    print(f"[Pasbaan] Font {fname} too small ({size}b), trying next URL", file=sys.stderr)
                    if os.path.exists(dest):
                        os.remove(dest)
            except Exception as e:
                print(f"[Pasbaan] WARNING: {fname} from {url} failed: {e}", file=sys.stderr)
        else:
            print(f"[Pasbaan] ERROR: all URLs failed for {fname} — sticker text may render small", file=sys.stderr)


# Run at import time (fast no-op if fonts already present)
_ensure_fonts()

_LOCAL_BOLD  = os.path.join(_FONTS_DIR, "Poppins-Bold.ttf")
_LOCAL_REG   = os.path.join(_FONTS_DIR, "Poppins-Regular.ttf")
_LOCAL_MONO  = os.path.join(_FONTS_DIR, "DejaVuSansMono-Bold.ttf")

_BOLD_FALLBACKS = [
    _LOCAL_BOLD,
    "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
_REG_FALLBACKS = [
    _LOCAL_REG,
    "/usr/share/fonts/truetype/google-fonts/Poppins-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]
_MONO_FALLBACKS = [
    _LOCAL_MONO,
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
]


def _font(size: int, bold: bool = False, medium: bool = False) -> ImageFont.FreeTypeFont:
    """Load Poppins (or best available fallback) at SVG-pt size scaled to pixels."""
    sz = round(size * _STICKER_SCALE)
    chain = _BOLD_FALLBACKS if bold else _REG_FALLBACKS
    for p in chain:
        if os.path.exists(p) and os.path.getsize(p) > 1000:
            try:
                return ImageFont.truetype(p, sz)
            except Exception:
                continue
    # Should never reach here, but log if we do
    print(f"[Pasbaan] WARNING: _font(size={size}, bold={bold}) fell to load_default!", file=sys.stderr)
    return ImageFont.load_default()


def _font_mono(size: int) -> ImageFont.FreeTypeFont:
    sz = round(size * _STICKER_SCALE)
    for p in _MONO_FALLBACKS:
        if os.path.exists(p) and os.path.getsize(p) > 1000:
            try:
                return ImageFont.truetype(p, sz)
            except Exception:
                continue
    return _font(size, bold=True)


def _px(v: float) -> int:
    """Convert SVG pt coordinate to pixel."""
    return round(v * _STICKER_SCALE)


def _draw_cx(draw, text, cx_svg, y_svg, font, color):
    """Draw text centred on cx_svg, baseline at y_svg (SVG coords).

    Pillow's textbbox returns (left, top, right, bottom) relative to the
    draw origin, where top>0 means the glyph top is *below* the origin and
    bottom>0 means the descender bottom is below it.  SVG 'y' is the text
    baseline.  We position so that bb[3] (descender bottom) lands at _px(y_svg),
    which places the visible text body just above the SVG y — matching SVG
    baseline behaviour.
    """
    bb = draw.textbbox((0, 0), text, font=font)
    tw = bb[2] - bb[0]
    x  = _px(cx_svg) - tw // 2 - bb[0]   # correct for any left-offset
    y  = _px(y_svg)  - bb[3]              # align descender bottom to SVG y
    draw.text((x, y), text, font=font, fill=color)


def _draw_lx(draw, text, x_svg, y_svg, font, color):
    """Draw text left-aligned, baseline at y_svg (SVG coords)."""
    bb = draw.textbbox((0, 0), text, font=font)
    x  = _px(x_svg) - bb[0]   # correct for any left-offset
    y  = _px(y_svg) - bb[3]   # align descender bottom to SVG y
    draw.text((x, y), text, font=font, fill=color)


def make_sticker(qr_id: str, theme_name: str = "yellow") -> bytes:
    """
    Render the Pasbaan sticker exactly matching the de.html SVG design.
    Left panel: white — PASBAAN branding + scan message
    Right panel: coloured — QR code + ID + trust footer
    Output: 1500×930 px PNG at 300 DPI
    """
    t     = THEMES.get(theme_name, THEMES["yellow"])
    WHITE = (255, 255, 255)
    LGRAY = (224, 224, 224)

    card = Image.new("RGB", (_STK_W, _STK_H), WHITE)
    draw = ImageDraw.Draw(card)

    # ── Right colour panel  (SVG: x=270 w=230 h=310) ─────────────────────────
    draw.rectangle([_px(270), 0, _STK_W, _STK_H], fill=t["panel_rgb"])

    # ── LEFT SECTION ─────────────────────────────────────────────────────────

    # "PASBAAN"  — x=135 y=50 font-size=32 font-weight=800 text-anchor=middle
    _draw_cx(draw, "PASBAAN", 135, 50, _font(32, bold=True), t["title_rgb"])

    # "PAKISTAN"  — x=135 y=75 font-size=14 letter-spacing=3
    _draw_cx(draw, "PAKISTAN", 135, 75, _font(14), t["sub_rgb"])

    # Divider  — x1=30 y1=90 x2=240 y2=90 stroke=#E0E0E0
    draw.rectangle([_px(30), _px(90), _px(240), _px(90) + max(1, _STICKER_SCALE // 2)],
                   fill=LGRAY)

    # Main message — bold, 3 lines
    _draw_lx(draw, "Scan in case of",   30, 135, _font(24, bold=True), t["body_rgb"])
    _draw_lx(draw, "emergency or to",   30, 165, _font(24, bold=True), t["body_rgb"])
    _draw_lx(draw, "contact the owner", 30, 195, _font(24, bold=True), t["body_rgb"])

    # Sub-text
    f11 = _font(11)
    _draw_lx(draw, "Use phone camera or QR scanner",  30, 255, f11, t["sub_rgb"])
    _draw_lx(draw, "to scan this QR code.",            30, 270, f11, t["sub_rgb"])
    _draw_lx(draw, "Visit pasbaan.com for more info.", 30, 285, f11, t["sub_rgb"])

    # ── RIGHT SECTION ─────────────────────────────────────────────────────────

    # QR container box  — x=290 y=25 w=170 h=170 rx=14
    draw.rounded_rectangle(
        [_px(290), _px(25), _px(460), _px(195)],
        radius=_px(14), fill=WHITE,
        outline=(0, 0, 0), width=max(1, _STICKER_SCALE),
    )

    # Generate QR code  — fills inner box (162×162 pt = 486×486 px)
    scan_url = f"{BASE_URL}/scan/{qr_id}"
    _qr = qrcode.QRCode(version=None,
                        error_correction=qrcode.constants.ERROR_CORRECT_H,
                        box_size=10, border=2)
    _qr.add_data(scan_url)
    _qr.make(fit=True)
    qr_img = _qr.make_image(fill_color=(0, 0, 0), back_color=(255, 255, 255)).convert("RGB")
    qr_px  = _px(162)
    qr_img = qr_img.resize((qr_px, qr_px), Image.LANCZOS)
    card.paste(qr_img, (_px(294), _px(29)))

    # QR ID  — x=375 y=210 font-size=14 font-weight=600 text-anchor=middle monospace
    _draw_cx(draw, qr_id, 375, 210, _font_mono(14), t["id_rgb"])

    # Thin divider below ID  — x1=300 y1=225 x2=450
    draw.rectangle([_px(300), _px(225), _px(450), _px(225) + max(1, _STICKER_SCALE // 3)],
                   fill=t["id_rgb"])

    # Trust text  — x=385 y=245 font-size=11 text-anchor=middle
    f11r = _font(11)
    _draw_cx(draw, "Secure | Pakistan Based | Instant Contact", 385, 245, f11r, t["id_rgb"])

    # Powered-by with underline  — x=385 y=265
    pby = "Powered by PASBAAN PAKISTAN"
    bb  = draw.textbbox((0, 0), pby, font=f11r)
    tw  = bb[2] - bb[0]
    tx  = _px(385) - tw // 2 - bb[0]   # centred, left-offset corrected
    ty  = _px(265) - bb[3]             # baseline-anchored
    draw.text((tx, ty), pby, font=f11r, fill=t["id_rgb"])
    ul_y = ty + bb[3] + 2
    draw.rectangle([tx, ul_y, tx + tw, ul_y + max(1, _STICKER_SCALE // 2)],
                   fill=t["id_rgb"])

    buf = io.BytesIO()
    card.save(buf, format="PNG", dpi=(300, 300))
    return buf.getvalue()


def make_a4_sheet(codes) -> bytes:
    """A4 sheet of stickers at 300 DPI. codes = [(qr_id, theme_name), ...]"""
    A4_W, A4_H   = 2480, 3508
    GAP, MGN     = 40,   60
    HEADER_H     = 120

    sheet = Image.new("RGB", (A4_W, A4_H), (238, 236, 230))
    draw  = ImageDraw.Draw(sheet)

    draw.text((MGN, MGN), "Pasbaan Pakistan — Print Sheet",
              font=_font(15, bold=True), fill=(15, 15, 15))
    draw.text((MGN, MGN + 52),
              f"Generated {datetime.now().strftime('%d %b %Y')}  ·  {len(codes)} sticker(s)",
              font=_font(10), fill=(120, 118, 112))

    top = MGN + HEADER_H
    for i, (qr_id, theme) in enumerate(codes[:8]):
        col = i % 2
        row = i // 2
        x   = MGN + col * (_STK_W + GAP)
        y   = top + row * (_STK_H + GAP)
        s   = Image.open(io.BytesIO(make_sticker(qr_id, theme)))
        sheet.paste(s, (x, y))

    buf = io.BytesIO()
    sheet.save(buf, format="PNG", dpi=(300, 300))
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN UI ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
def admin_home(
    request: Request,
    page: int = 1,
    per_page: int = 25,
    session: str = Cookie(default=None),
    key: str = "",
):
    """Admin dashboard — shows login if not authenticated."""
    authed = is_valid_session(session) or key == ADMIN_KEY
    if not authed:
        return HTMLResponse(admin_login_page())
    if per_page not in (25, 50, 100):
        per_page = 25
    page = max(1, page)
    try:
        stats      = db_stats()
        rows, total = db_list_all(page, per_page)
        return HTMLResponse(admin_dashboard_page(
            stats, rows, total, page, per_page,
            key if key == ADMIN_KEY else "",
        ))
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        return HTMLResponse(f"<pre style='background:#1a1a1a;color:#f87171;padding:2rem;font-size:13px'>{err}</pre>", status_code=500)


@app.post("/admin/login", response_class=HTMLResponse)
async def admin_login(password: str = Form(...)):
    """Handle admin login form submission."""
    if password != ADMIN_KEY:
        return HTMLResponse(admin_login_page(error=True))
    token    = create_session()
    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(
        "session", token,
        httponly=True,
        max_age=SESSION_HOURS * 3600,
        samesite="none",
        secure=True,
    )
    return response


@app.get("/admin/logout")
def admin_logout():
    response = RedirectResponse(url="/admin", status_code=303)
    response.delete_cookie("session")
    return response


@app.get("/admin/generate-qr")
def admin_generate(
    count: int = 1,
    theme: str = "classic",
    session: str = Cookie(default=None),
    key: str = "",
):
    """
    Generate sequential QR codes.
    Called by admin UI (uses session cookie) or directly (uses ?key=).
    """
    if not is_valid_session(session) and key != ADMIN_KEY:
        raise HTTPException(403, "Not authorised")
    if not 1 <= count <= 500:
        raise HTTPException(400, "count must be 1–500")
    if theme not in THEMES:
        theme = "yellow"

    results = []
    for _ in range(count):
        num   = db_next_seq()
        qr_id = f"ST-{num:06d}"
        db_insert_qr(qr_id, theme)
        results.append({
            "qr_id":       qr_id,
            "scan_url":    f"{BASE_URL}/scan/{qr_id}",
            "sticker_url": f"{BASE_URL}/admin/sticker/{qr_id}",
            "theme":       theme,
        })

    return {"generated": len(results), "codes": results}


@app.get("/admin/qr-list")
def admin_list(
    page: int = 1,
    per_page: int = 25,
    session: str = Cookie(default=None),
    key: str = "",
):
    if not is_valid_session(session) and key != ADMIN_KEY:
        raise HTTPException(403, "Not authorised")
    if per_page not in (25, 50, 100):
        per_page = 25
    page = max(1, page)
    rows, total = db_list_all(page, per_page)
    return {
        "page":     page,
        "per_page": per_page,
        "total":    total,
        "pages":    (total + per_page - 1) // per_page,
        "codes": [
            {
                "qr_id":        r["qr_id"],
                "scan_count":   r["scan_count"],
                "theme":        r["theme"],
                "status":       "active" if r["activated_at"] else "unclaimed",
                "activated_at": str(r["activated_at"]) if r["activated_at"] else None,
                "created_at":   str(r["created_at"]),
            }
            for r in rows
        ],
    }


@app.delete("/admin/qr/{qr_id}")
def admin_delete_qr(
    qr_id: str,
    session: str = Cookie(default=None),
    key: str = "",
):
    """Permanently delete a QR code from the database."""
    if not is_valid_session(session) and key != ADMIN_KEY:
        raise HTTPException(403, "Not authorised")
    record = db_get_record(qr_id)
    if not record:
        raise HTTPException(404, "QR code not found")
    db_delete_qr(qr_id)
    return {"deleted": qr_id}


@app.post("/admin/qr/{qr_id}/reset")
def admin_reset_qr(
    qr_id: str,
    session: str = Cookie(default=None),
    key: str = "",
):
    """Reset a QR code to unclaimed — clears owner data, keeps the sticker ID."""
    if not is_valid_session(session) and key != ADMIN_KEY:
        raise HTTPException(403, "Not authorised")
    record = db_get_record(qr_id)
    if not record:
        raise HTTPException(404, "QR code not found")
    db_reset_qr(qr_id)
    return {"reset": qr_id}




@app.get("/admin/sticker/{qr_id}")
def admin_sticker(
    qr_id: str,
    theme: str = "",
    session: str = Cookie(default=None),
    key: str = "",
):
    if not is_valid_session(session) and key != ADMIN_KEY:
        raise HTTPException(403, "Not authorised")
    record = db_get_record(qr_id)
    if not record:
        raise HTTPException(404, "QR code not found")
    use_theme = theme or record.get("theme", "yellow")
    png = make_sticker(qr_id, use_theme)
    return Response(
        content=png, media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{qr_id}.png"'},
    )


@app.get("/admin/sheet")
def admin_sheet(
    limit: int = 8,
    theme: str = "classic",
    session: str = Cookie(default=None),
    key: str = "",
):
    if not is_valid_session(session) and key != ADMIN_KEY:
        raise HTTPException(403, "Not authorised")
    rows, _ = db_list_all(page=1, per_page=8)
    if not rows:
        raise HTTPException(404, "No QR codes found.")
    codes = [(r["qr_id"], r.get("theme", theme)) for r in rows[:min(limit, 8)]]
    sheet = make_a4_sheet(codes)
    return Response(
        content=sheet, media_type="image/png",
        headers={"Content-Disposition": 'attachment; filename="pasbaan_sheet.png"'},
    )



@app.get("/admin/download-zip")
def admin_download_zip(
    qr_ids: str = "",          # comma-separated list of IDs just generated
    session: str = Cookie(default=None),
    key: str = "",
):
    """
    Return a ZIP archive containing one PNG sticker per qr_id.
    qr_ids = comma-separated, e.g. ST-000001,ST-000002
    """
    if not is_valid_session(session) and key != ADMIN_KEY:
        raise HTTPException(403, "Not authorised")

    ids = [i.strip() for i in qr_ids.split(",") if i.strip()]
    if not ids:
        raise HTTPException(400, "No QR IDs provided")
    if len(ids) > 500:
        raise HTTPException(400, "Too many IDs (max 500)")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for qr_id in ids:
            record = db_get_record(qr_id)
            if not record:
                continue
            theme = record.get("theme", "classic")
            png   = make_sticker(qr_id, theme)
            zf.writestr(f"{qr_id}.png", png)

    zip_buf.seek(0)
    return Response(
        content=zip_buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="pasbaan_stickers.zip"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN — SUBSCRIPTION MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/admin/subscriptions", response_class=HTMLResponse)
def admin_subscriptions(session: str = Cookie(default=None)):
    """Admin page showing all subscriptions with activate/suspend controls."""
    if not is_valid_session(session):
        return RedirectResponse(url="/admin", status_code=302)
    rows = db_get_all_subscriptions_for_admin()
    return HTMLResponse(page_admin_subscriptions(rows))


@app.post("/admin/subscriptions/{qr_id}/activate")
async def admin_activate_subscription(qr_id: str, session: str = Cookie(default=None)):
    if not is_valid_session(session):
        raise HTTPException(403, "Not authorised")
    db_activate_subscription(qr_id)
    return RedirectResponse(url="/admin/subscriptions", status_code=303)


@app.post("/admin/subscriptions/{qr_id}/suspend")
async def admin_suspend_subscription(qr_id: str, session: str = Cookie(default=None)):
    if not is_valid_session(session):
        raise HTTPException(403, "Not authorised")
    db_deactivate_subscription(qr_id)
    return RedirectResponse(url="/admin/subscriptions", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/admin", status_code=302)

@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    try:
        conn = get_db(); release_db(conn); db_status = "connected"
    except Exception as e:
        db_status = f"error: {e}"
    return {"status": "ok", "database": db_status, "service": "Pasbaan Pakistan v3"}


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC SCAN ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/scan/{qr_id}", response_class=HTMLResponse)
def scan_qr(qr_id: str, request: Request):
    record = db_get_record(qr_id)
    if not record:
        return Response(content=page_not_found(qr_id).encode("utf-8"), media_type="text/html; charset=utf-8", status_code=404)
    db_increment_scan(qr_id)
    owner_data = record["owner_data"]
    if not owner_data:
        # First scan — send to plan selection before setup
        return RedirectResponse(url=f"/scan/{qr_id}/choose-plan", status_code=302)
    # Check deactivation AFTER confirming owner_data exists
    if record.get("is_active") is False:
        return Response(content=page_deactivated(qr_id).encode("utf-8"), media_type="text/html; charset=utf-8")
    if isinstance(owner_data, str):
        owner_data = json.loads(owner_data)
    html = page_contact(qr_id, owner_data, record["scan_count"] + 1)
    return Response(content=html.encode("utf-8"), media_type="text/html; charset=utf-8")


@app.get("/scan/{qr_id}/choose-plan", response_class=HTMLResponse)
def choose_plan(qr_id: str):
    """Plan selection page — shown before the setup form on first scan."""
    record = db_get_record(qr_id)
    if not record:
        return Response(content=page_not_found(qr_id).encode("utf-8"),
                        media_type="text/html; charset=utf-8", status_code=404)
    if record["owner_data"] is not None:
        return RedirectResponse(url=f"/scan/{qr_id}", status_code=302)
    return HTMLResponse(page_choose_plan(qr_id))


@app.post("/scan/{qr_id}/select-plan", response_class=HTMLResponse)
async def select_plan(qr_id: str, plan: str = Form("basic")):
    """Save chosen plan in session cookie, redirect to setup form."""
    if plan not in ("basic", "premium"):
        plan = "basic"
    record = db_get_record(qr_id)
    if not record:
        raise HTTPException(404, "QR code not found")
    if record["owner_data"] is not None:
        return RedirectResponse(url=f"/scan/{qr_id}", status_code=302)
    response = RedirectResponse(url=f"/scan/{qr_id}/setup", status_code=303)
    # Store chosen plan in a short-lived cookie so the setup form knows
    response.set_cookie(
        f"plan_{qr_id}", plan,
        httponly=True, max_age=3600,
        samesite="none", secure=True,
    )
    return response


@app.get("/scan/{qr_id}/setup", response_class=HTMLResponse)
def setup_form(qr_id: str, request: Request):
    """Show the setup form — only reachable after choosing a plan."""
    record = db_get_record(qr_id)
    if not record:
        return Response(content=page_not_found(qr_id).encode("utf-8"),
                        media_type="text/html; charset=utf-8", status_code=404)
    if record["owner_data"] is not None:
        return RedirectResponse(url=f"/scan/{qr_id}", status_code=302)
    chosen_plan = request.cookies.get(f"plan_{qr_id}", "basic")
    return HTMLResponse(page_setup(qr_id, chosen_plan))


@app.post("/scan/{qr_id}/setup", response_class=HTMLResponse)
async def save_setup(
    qr_id:             str,
    request:           Request,
    owner_name:        str = Form(""),
    vehicle_number:    str = Form(""),
    city:              str = Form(""),
    owner_phone:       str = Form(""),
    owner_whatsapp:    str = Form(""),
    owner_pin:         str = Form(...),
    owner_pin_confirm: str = Form(...),
    contact1_relation:  str = Form(""),
    contact1_name:      str = Form(""),
    contact1_phone:     str = Form(""),
    contact1_whatsapp:  str = Form(""),
    contact2_relation:  str = Form(""),
    contact2_name:      str = Form(""),
    contact2_phone:     str = Form(""),
    contact2_whatsapp:  str = Form(""),
    contact3_relation:  str = Form(""),
    contact3_name:      str = Form(""),
    contact3_phone:     str = Form(""),
    contact3_whatsapp:  str = Form(""),
    message:            str = Form(""),
):
    record = db_get_record(qr_id)
    if not record:
        raise HTTPException(404, "QR code not found")
    if record["owner_data"] is not None:
        raise HTTPException(403, "Already activated.")

    # ── PIN validation ────────────────────────────────────────────────────
    pin_errors = []
    if not re.fullmatch(r"\d{4}", owner_pin):
        pin_errors.append("PIN must be exactly 4 digits (numbers only).")
    elif owner_pin != owner_pin_confirm:
        pin_errors.append("PINs do not match. Please re-enter.")
    if pin_errors:
        error_list = "".join(f"<li>{e}</li>" for e in pin_errors)
        return HTMLResponse(f"""<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8">{_css()}<title>PIN Error</title></head>
<body><div class="wrap">{_logo()}
<div class="card" style="border:1.5px solid #fecaca;background:#fff5f5">
  <div class="sec-title" style="color:#dc2626">🔒 PIN Error</div>
  <ul style="font-size:14px;color:#9f1239;line-height:2;padding-left:18px">{error_list}</ul>
</div>
<a href="/scan/{qr_id}" class="btn btn-ghost" style="display:block;text-align:center;text-decoration:none;margin-top:8px">
  ← Go back and fix
</a>
</div></body></html>""", status_code=422)

    # ── Server-side phone validation ──────────────────────────────────────
    validation_errors = []
    try:
        owner_phone = validate_pk_phone(owner_phone) if owner_phone.strip() else ""
    except ValueError as e:
        validation_errors.append(f"Your phone number: {e}")
    try:
        contact1_phone = validate_pk_phone(contact1_phone) if contact1_phone.strip() else ""
    except ValueError as e:
        validation_errors.append(f"Contact 1 phone: {e}")
    if contact2_name.strip() and contact2_phone.strip():
        try:
            contact2_phone = validate_pk_phone(contact2_phone)
        except ValueError as e:
            validation_errors.append(f"Contact 2 phone: {e}")
    if contact3_name.strip() and contact3_phone.strip():
        try:
            contact3_phone = validate_pk_phone(contact3_phone)
        except ValueError as e:
            validation_errors.append(f"Contact 3 phone: {e}")
    if validation_errors:
        error_list = "".join(f"<li>{e}</li>" for e in validation_errors)
        return HTMLResponse(f"""<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8">{_css()}<title>Validation Error</title></head>
<body><div class="wrap">{_logo()}
<div class="card" style="border:1.5px solid #fecaca;background:#fff5f5">
  <div class="sec-title" style="color:#dc2626">⚠️ Phone Number Error</div>
  <ul style="font-size:14px;color:#9f1239;line-height:2;padding-left:18px">{error_list}</ul>
  <p style="font-size:13px;color:#666;margin-top:12px">
    Please use Pakistani mobile format: <strong>03XX-XXXXXXX</strong> or <strong>+923XXXXXXXXX</strong>
  </p>
</div>
<a href="/scan/{qr_id}" class="btn btn-ghost" style="display:block;text-align:center;text-decoration:none;margin-top:8px">
  ← Go back and fix
</a>
</div></body></html>""", status_code=422)

    contacts = []
    if contact1_name.strip() and contact1_phone.strip():
        contacts.append({"relation": contact1_relation,
                         "name": contact1_name.strip(),
                         "phone": contact1_phone.strip(),
                         "whatsapp": bool(contact1_whatsapp)})
    if contact2_name.strip() and contact2_phone.strip():
        contacts.append({"relation": contact2_relation,
                         "name": contact2_name.strip(),
                         "phone": contact2_phone.strip(),
                         "whatsapp": bool(contact2_whatsapp)})
    if contact3_name.strip() and contact3_phone.strip():
        contacts.append({"relation": contact3_relation,
                         "name": contact3_name.strip(),
                         "phone": contact3_phone.strip(),
                         "whatsapp": bool(contact3_whatsapp)})

    payload = {
        "owner_name":     owner_name.strip(),
        "owner_phone":    owner_phone.strip(),
        "owner_whatsapp": bool(owner_whatsapp),
        "vehicle_number": vehicle_number.strip().upper(),
        "city":           city.strip(),
        "contacts":       contacts,
        "message":        message.strip(),
    }
    db_save_owner(qr_id, payload, hash_pin(owner_pin))

    # Create subscription record using plan chosen on plan-selection page
    chosen_plan = request.cookies.get(f"plan_{qr_id}", "basic")
    if chosen_plan not in ("basic", "premium"):
        chosen_plan = "basic"
    db_create_subscription(qr_id, chosen_plan)

    response = HTMLResponse(page_payment_instructions(qr_id, owner_name.strip(), chosen_plan))
    response.delete_cookie(f"plan_{qr_id}")
    return response


@app.post("/scan/{qr_id}/deactivate", response_class=HTMLResponse)
async def deactivate_qr(qr_id: str, request: Request, pin: str = Form(...)):
    """PIN-verified deactivation."""
    record = db_get_record(qr_id)
    if not record or not record["owner_data"]:
        raise HTTPException(404, "Not found")
    if record.get("owner_pin") and not verify_pin(pin, record["owner_pin"]):
        return HTMLResponse(_pin_error_page(qr_id, "Incorrect PIN. QR code not deactivated."))
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE qr_codes SET is_active=FALSE WHERE qr_id=%s", (qr_id,))
    conn.commit(); cur.close(); release_db(conn)
    resp = HTMLResponse(f"""<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8">{_css()}<title>QR Deactivated</title>
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body><div class="wrap">{_logo()}
<div class="card" style="text-align:center;padding:40px 20px">
  <div style="font-size:60px;margin-bottom:16px">🔴</div>
  <h2 style="font-size:22px;font-weight:700;margin-bottom:10px">QR Code Deactivated</h2>
  <p style="color:#666;font-size:15px;line-height:1.65">
    Your QR code <strong>{qr_id}</strong> is now inactive.<br>
    Anyone who scans it will see a notice with the option to reactivate.
  </p>
</div>
<a href="/scan/{qr_id}" class="btn btn-ghost" style="display:block;text-align:center;text-decoration:none">
  View QR page &#8594;</a>
</div></body></html>""")
    resp.delete_cookie(f"owner_session_{qr_id}")
    return resp


@app.post("/scan/{qr_id}/activate", response_class=HTMLResponse)
async def activate_qr(qr_id: str, request: Request, pin: str = Form(...)):
    """PIN-verified reactivation."""
    record = db_get_record(qr_id)
    if not record or not record["owner_data"]:
        raise HTTPException(404, "Not found")
    if record.get("owner_pin") and not verify_pin(pin, record["owner_pin"]):
        return HTMLResponse(_pin_error_page(qr_id, "Incorrect PIN. QR code not reactivated."))
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE qr_codes SET is_active=TRUE WHERE qr_id=%s", (qr_id,))
    conn.commit(); cur.close(); release_db(conn)
    return HTMLResponse(f"""<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8">{_css()}<title>QR Activated</title>
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body><div class="wrap">{_logo()}
<div class="card" style="text-align:center;padding:40px 20px">
  <div style="font-size:60px;margin-bottom:16px">🟢</div>
  <h2 style="font-size:22px;font-weight:700;margin-bottom:10px">QR Code Reactivated!</h2>
  <p style="color:#666;font-size:15px;line-height:1.65">
    Your QR code <strong>{qr_id}</strong> is active again.<br>
    Scanners will now see your full contact page as normal.
  </p>
</div>
<a href="/scan/{qr_id}" class="btn btn-ghost" style="display:block;text-align:center;text-decoration:none">
  View contact page &#8594;</a>
</div></body></html>""")


@app.post("/scan/{qr_id}/verify-pin", response_class=HTMLResponse)
async def verify_pin_route(
    qr_id:   str,
    pin:     str = Form(...),
    next_url: str = Form(""),
):
    """Check the owner PIN. On success set a signed cookie and redirect to update form."""
    record = db_get_record(qr_id)
    if not record or not record["owner_data"]:
        raise HTTPException(404, "QR code not found or not activated")

    stored_hash = record.get("owner_pin")

    # Backwards-compat: if no PIN stored yet (old records), accept any 4-digit entry
    # and set it as the PIN going forward — one-time migration for existing owners.
    if not stored_hash:
        if not re.fullmatch(r"\d{4}", pin):
            return HTMLResponse(_pin_error_page(qr_id, "Please enter a valid 4-digit PIN."))
        # Store PIN for the first time
        conn = get_db(); cur = conn.cursor()
        cur.execute("UPDATE qr_codes SET owner_pin=%s WHERE qr_id=%s", (hash_pin(pin), qr_id))
        conn.commit(); cur.close(); release_db(conn)
    elif not verify_pin(pin, stored_hash):
        return HTMLResponse(_pin_error_page(qr_id, "Incorrect PIN. Please try again."))

    token    = make_pin_token(qr_id)
    redirect = next_url or f"/scan/{qr_id}/update"
    response = RedirectResponse(url=redirect, status_code=303)
    response.set_cookie(
        f"owner_session_{qr_id}", token,
        httponly=True,
        max_age=PIN_SESSION_HOURS * 3600,
        samesite="none",
        secure=True,
    )
    return response


def _pin_error_page(qr_id: str, msg: str) -> str:
    return f"""<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8">{_css()}<title>Wrong PIN</title></head>
<body><div class="wrap">{_logo()}
<div class="card" style="text-align:center;padding:32px 20px;border:1.5px solid #fecaca;background:#fff5f5">
  <div style="font-size:48px;margin-bottom:12px">🔒</div>
  <h2 style="font-size:18px;font-weight:700;color:#9f1239;margin-bottom:10px">Access Denied</h2>
  <p style="font-size:14px;color:#7f1d1d;line-height:1.65">{msg}</p>
</div>
<a href="/scan/{qr_id}" class="btn btn-ghost" style="display:block;text-align:center;text-decoration:none;margin-top:10px">
  ← Back to contact page
</a>
</div></body></html>"""


@app.get("/scan/{qr_id}/update", response_class=HTMLResponse)
def owner_update_form(qr_id: str, request: Request):
    """Show a pre-filled update form — only accessible after PIN verification."""
    record = db_get_record(qr_id)
    if not record:
        return Response(content=page_not_found(qr_id).encode("utf-8"), media_type="text/html; charset=utf-8", status_code=404)
    if not record["owner_data"]:
        return RedirectResponse(url=f"/scan/{qr_id}", status_code=303)

    # Check PIN cookie
    token = request.cookies.get(f"owner_session_{qr_id}")
    if not verify_pin_token(token, qr_id):
        return HTMLResponse(page_pin_prompt(qr_id))
    data = record["owner_data"]
    if isinstance(data, str):
        data = json.loads(data)
    return HTMLResponse(page_update(qr_id, data))


@app.post("/scan/{qr_id}/update", response_class=HTMLResponse)
async def save_update(
    qr_id:             str,
    request:           Request,
    owner_name:        str = Form(...),
    vehicle_number:    str = Form(...),
    city:              str = Form(...),
    owner_phone:       str = Form(""),
    owner_whatsapp:    str = Form(""),
    new_pin:           str = Form(""),
    new_pin_confirm:   str = Form(""),
    contact1_relation:  str = Form(...),
    contact1_name:      str = Form(...),
    contact1_phone:     str = Form(...),
    contact1_whatsapp:  str = Form(""),
    contact2_relation:  str = Form(""),
    contact2_name:      str = Form(""),
    contact2_phone:     str = Form(""),
    contact2_whatsapp:  str = Form(""),
    contact3_relation:  str = Form(""),
    contact3_name:      str = Form(""),
    contact3_phone:     str = Form(""),
    contact3_whatsapp:  str = Form(""),
    message:            str = Form(""),
):
    record = db_get_record(qr_id)
    if not record:
        raise HTTPException(404, "QR code not found")
    if not record["owner_data"]:
        raise HTTPException(403, "This QR code has not been activated yet.")

    # ── PIN cookie gate ───────────────────────────────────────────────────
    token = request.cookies.get(f"owner_session_{qr_id}")
    if not verify_pin_token(token, qr_id):
        raise HTTPException(403, "Session expired. Please verify your PIN again.")

    # ── Optional new PIN ──────────────────────────────────────────────────
    new_pin_hash = None
    if new_pin.strip():
        if not re.fullmatch(r"\d{4}", new_pin):
            return HTMLResponse(_pin_error_page(qr_id, "New PIN must be exactly 4 digits."))
        if new_pin != new_pin_confirm:
            return HTMLResponse(_pin_error_page(qr_id, "New PINs do not match."))
        new_pin_hash = hash_pin(new_pin)

    # ── Server-side phone validation ──────────────────────────────────────
    validation_errors = []
    try:
        owner_phone = validate_pk_phone(owner_phone) if owner_phone.strip() else ""
    except ValueError as e:
        validation_errors.append(f"Your phone number: {e}")
    try:
        contact1_phone = validate_pk_phone(contact1_phone)
    except ValueError as e:
        validation_errors.append(f"Contact 1 phone: {e}")
    if contact2_name.strip() and contact2_phone.strip():
        try:
            contact2_phone = validate_pk_phone(contact2_phone)
        except ValueError as e:
            validation_errors.append(f"Contact 2 phone: {e}")
    if contact3_name.strip() and contact3_phone.strip():
        try:
            contact3_phone = validate_pk_phone(contact3_phone)
        except ValueError as e:
            validation_errors.append(f"Contact 3 phone: {e}")
    if validation_errors:
        error_list = "".join(f"<li>{e}</li>" for e in validation_errors)
        return HTMLResponse(f"""<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8">{_css()}<title>Validation Error</title></head>
<body><div class="wrap">{_logo()}
<div class="card" style="border:1.5px solid #fecaca;background:#fff5f5">
  <div class="sec-title" style="color:#dc2626">⚠️ Phone Number Error</div>
  <ul style="font-size:14px;color:#9f1239;line-height:2;padding-left:18px">{error_list}</ul>
  <p style="font-size:13px;color:#666;margin-top:12px">
    Please use Pakistani mobile format: <strong>03XX-XXXXXXX</strong> or <strong>+923XXXXXXXXX</strong>
  </p>
</div>
<a href="/scan/{qr_id}/update" class="btn btn-ghost" style="display:block;text-align:center;text-decoration:none;margin-top:8px">
  ← Go back and fix
</a>
</div></body></html>""", status_code=422)

    contacts = [{"relation": contact1_relation,
                 "name": contact1_name.strip(),
                 "phone": contact1_phone.strip(),
                 "whatsapp": bool(contact1_whatsapp)}]
    if contact2_name.strip() and contact2_phone.strip():
        contacts.append({"relation": contact2_relation,
                         "name": contact2_name.strip(),
                         "phone": contact2_phone.strip(),
                         "whatsapp": bool(contact2_whatsapp)})
    if contact3_name.strip() and contact3_phone.strip():
        contacts.append({"relation": contact3_relation,
                         "name": contact3_name.strip(),
                         "phone": contact3_phone.strip(),
                         "whatsapp": bool(contact3_whatsapp)})

    payload = {
        "owner_name":     owner_name.strip(),
        "owner_phone":    owner_phone.strip(),
        "owner_whatsapp": bool(owner_whatsapp),
        "vehicle_number": vehicle_number.strip().upper(),
        "city":           city.strip(),
        "contacts":       contacts,
        "message":        message.strip(),
    }
    db_update_owner(qr_id, payload, new_pin_hash)
    return HTMLResponse(page_update_success(qr_id, owner_name.strip()))


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN HTML PAGES
# ─────────────────────────────────────────────────────────────────────────────

def admin_login_page(error: bool = False) -> str:
    err_html = """
    <div style="background:#fff1f2;border:1px solid #fecdd3;color:#9f1239;
                padding:10px 14px;border-radius:10px;font-size:14px;margin-bottom:16px">
      ❌ Wrong password. Please try again.
    </div>""" if error else ""

    return f"""<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pasbaan Admin Login</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#0f0f0f;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}}
.box{{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:20px;padding:40px 36px;width:100%;max-width:400px}}
.logo{{text-align:center;margin-bottom:32px}}
.logo-name{{font-size:28px;font-weight:700;color:#fff;letter-spacing:-.5px}}
.logo-dot{{color:#ef4444}}
.logo-sub{{font-size:12px;color:#666;margin-top:4px;letter-spacing:.05em;text-transform:uppercase}}
label{{display:block;font-size:13px;color:#888;margin-bottom:6px}}
input[type=password]{{width:100%;padding:12px 16px;background:#111;border:1.5px solid #333;
  border-radius:11px;font-size:15px;color:#fff;outline:none;transition:border-color .15s}}
input[type=password]:focus{{border-color:#555}}
.btn{{width:100%;padding:13px;background:#fff;color:#111;border:none;border-radius:11px;
      font-size:15px;font-weight:600;cursor:pointer;margin-top:14px;transition:opacity .15s}}
.btn:hover{{opacity:.88}}
.hint{{font-size:12px;color:#444;text-align:center;margin-top:16px}}
</style></head>
<body>
<div class="box">
  <div class="logo">
    <div class="logo-name">Pas<span class="logo-dot">baan</span></div>
    <div class="logo-sub">Admin Panel</div>
  </div>
  {err_html}
  <form method="POST" action="/admin/login">
    <label>Admin Password</label>
    <input type="password" name="password" placeholder="Enter your password" autofocus required>
    <button type="submit" class="btn">Login →</button>
  </form>
  <p class="hint">Pasbaan Pakistan · Secure Admin Access</p>
</div>
</body></html>"""


def admin_dashboard_page(stats, rows, total, page, per_page, key="") -> str:
    def _theme_card(k, v):
        preview = v["preview"]
        label = v["label"]
        return (f"<div class=\"theme-card\" data-theme=\"{k}\" onclick=\"selectTheme('{k}')\" style=\"border-color:{preview}\">"
                f"<div class=\"theme-dot\" style=\"background:{preview};border:2px solid rgba(255,255,255,.15)\"></div>"
                f"<span>{label}</span></div>")
    theme_options = "".join(
        _theme_card(k, v)
        for k, v in THEMES.items()
    )

    total_pages = (total + per_page - 1) // per_page

    rows_html = ""
    for r in rows:
        active = r["activated_at"] is not None
        badge  = ('<span class="badge-active">Active</span>' if active
                  else '<span class="badge-unclaimed">Unclaimed</span>')
        qr_id  = r['qr_id']
        reset_btn = (
            "<button class='tbl-btn tbl-btn-warn' onclick=\"resetCode('" + qr_id + "')\">↺ Reset</button>"
            if active else ""
        )
        rows_html += f"""
        <tr id="row-{qr_id}">
          <td class="mono">{qr_id}</td>
          <td>{badge}</td>
          <td>{r['scan_count']}</td>
          <td>{r.get('theme','classic')}</td>
          <td>
            <a href="/admin/sticker/{qr_id}" class="tbl-btn" download>⬇ PNG</a>
            <a href="/scan/{qr_id}" class="tbl-btn" target="_blank">👁 View</a>
            {reset_btn}
            <button class='tbl-btn tbl-btn-danger' onclick="deleteCode('{qr_id}')">🗑 Delete</button>
          </td>
        </tr>"""

    # Pagination controls
    def pg_btn(p, label, disabled=False):
        dis = "disabled" if disabled else ""
        return (f'<button class="pg-btn" onclick="goPage({p})" {dis}>{label}</button>'
                if not disabled else
                f'<button class="pg-btn" disabled>{label}</button>')

    pagination_html = ""
    if total_pages > 1:
        pagination_html = f"""
        <div class="pg-bar">
          {pg_btn(1, "«", page == 1)}
          {pg_btn(page - 1, "‹ Prev", page == 1)}
          <span class="pg-info">Page {page} of {total_pages} &nbsp;·&nbsp; {total} codes</span>
          {pg_btn(page + 1, "Next ›", page == total_pages)}
          {pg_btn(total_pages, "»", page == total_pages)}
          <select class="pg-select" onchange="changePerPage(this.value)">
            {"".join(f'<option value="{n}" {"selected" if n == per_page else ""}>{n} / page</option>' for n in [25, 50, 100])}
          </select>
        </div>"""

    return f"""<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pasbaan Admin Dashboard</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#0f0f0f;color:#e5e5e5;min-height:100vh}}
.topbar{{display:flex;align-items:center;justify-content:space-between;
         padding:16px 28px;border-bottom:1px solid #1e1e1e;background:#111}}
.logo-name{{font-size:20px;font-weight:700;color:#fff}}
.logo-dot{{color:#ef4444}}
.logo-sub{{font-size:11px;color:#555;margin-top:1px}}
.logout{{font-size:13px;color:#666;text-decoration:none;padding:6px 14px;
         border:1px solid #2a2a2a;border-radius:8px}}
.logout:hover{{color:#fff;border-color:#444}}
.main{{max-width:1100px;margin:0 auto;padding:28px 20px}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:28px}}
.stat{{background:#161616;border:1px solid #222;border-radius:14px;padding:18px 20px}}
.stat-val{{font-size:30px;font-weight:700;color:#fff;margin-bottom:4px}}
.stat-label{{font-size:12px;color:#555;text-transform:uppercase;letter-spacing:.05em}}
.next-id{{font-size:14px;color:#22c55e;font-family:monospace;margin-top:4px}}
.card{{background:#161616;border:1px solid #222;border-radius:16px;padding:24px;margin-bottom:20px}}
.card-title{{font-size:13px;font-weight:600;color:#888;text-transform:uppercase;
             letter-spacing:.07em;margin-bottom:18px}}
.row2{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
label{{display:block;font-size:13px;color:#666;margin-bottom:6px}}
input[type=number]{{width:100%;padding:11px 14px;background:#111;border:1.5px solid #2a2a2a;
  border-radius:10px;font-size:20px;font-weight:600;color:#fff;outline:none;
  transition:border-color .15s}}
input[type=number]:focus{{border-color:#555}}
.theme-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}
.theme-card{{display:flex;align-items:center;gap:10px;padding:10px 12px;
             background:#111;border:1.5px solid #2a2a2a;border-radius:10px;
             cursor:pointer;transition:border-color .15s;font-size:13px;color:#aaa}}
.theme-card:hover{{border-color:#444}}
.theme-card.selected{{border-color:#fff;color:#fff}}
.theme-dot{{width:20px;height:20px;border-radius:50%;flex-shrink:0}}
.btn-gen{{width:100%;padding:14px;background:#fff;color:#111;border:none;
          border-radius:12px;font-size:16px;font-weight:700;cursor:pointer;
          margin-top:18px;transition:opacity .15s}}
.btn-gen:hover{{opacity:.88}}
.btn-gen:disabled{{opacity:.4;cursor:not-allowed}}
.result-box{{background:#111;border:1px solid #222;border-radius:10px;padding:14px;
             margin-top:16px;display:none}}
.result-title{{font-size:12px;color:#555;margin-bottom:10px;text-transform:uppercase;letter-spacing:.05em}}
.qr-result{{display:flex;align-items:center;justify-content:space-between;
            padding:9px 0;border-bottom:1px solid #1e1e1e;font-size:13px}}
.qr-result:last-child{{border-bottom:none}}
.mono{{font-family:monospace;font-size:13px;color:#22c55e}}
.dl-btn{{padding:5px 12px;background:#1a1a1a;border:1px solid #2a2a2a;
         border-radius:6px;color:#aaa;font-size:12px;cursor:pointer;
         text-decoration:none;transition:background .12s}}
.dl-btn:hover{{background:#222;color:#fff}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;padding:8px 12px;color:#555;font-size:11px;text-transform:uppercase;
    letter-spacing:.05em;border-bottom:1px solid #1e1e1e}}
td{{padding:10px 12px;border-bottom:1px solid #161616;color:#ccc;vertical-align:middle}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#1a1a1a}}
.badge-active{{background:#052e16;color:#22c55e;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600}}
.badge-unclaimed{{background:#1c1200;color:#eab308;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600}}
.tbl-btn{{padding:4px 10px;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:6px;
          color:#888;font-size:11px;text-decoration:none;margin-right:4px;display:inline-block;cursor:pointer;font-family:inherit}}
.tbl-btn:hover{{color:#fff;border-color:#444}}
.tbl-btn-warn{{border-color:#422006;color:#f97316}}
.tbl-btn-warn:hover{{background:#431407;color:#fb923c;border-color:#9a3412}}
.tbl-btn-danger{{border-color:#3f0a0a;color:#ef4444}}
.tbl-btn-danger:hover{{background:#2d0a0a;color:#f87171;border-color:#7f1d1d}}
.pg-bar{{display:flex;align-items:center;gap:8px;margin-top:14px;flex-wrap:wrap}}
.pg-btn{{padding:5px 12px;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:7px;
         color:#aaa;font-size:12px;cursor:pointer;transition:background .12s}}
.pg-btn:hover:not([disabled]){{background:#222;color:#fff;border-color:#444}}
.pg-btn[disabled]{{opacity:.35;cursor:not-allowed}}
.pg-info{{font-size:12px;color:#555;padding:0 6px}}
.pg-select{{padding:5px 10px;background:#111;border:1px solid #2a2a2a;border-radius:7px;
            color:#aaa;font-size:12px;cursor:pointer}}
.confirm-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);
                  z-index:999;align-items:center;justify-content:center}}
.confirm-overlay.open{{display:flex}}
.confirm-box{{background:#1a1a1a;border:1px solid #333;border-radius:16px;padding:28px 32px;
              max-width:380px;width:90%;text-align:center}}
.confirm-title{{font-size:16px;font-weight:700;color:#fff;margin-bottom:10px}}
.confirm-msg{{font-size:13px;color:#888;line-height:1.6;margin-bottom:22px}}
.confirm-actions{{display:flex;gap:10px;justify-content:center}}
.confirm-yes-del{{padding:10px 24px;background:#ef4444;color:#fff;border:none;border-radius:9px;
                  font-size:14px;font-weight:600;cursor:pointer}}
.confirm-yes-reset{{padding:10px 24px;background:#f97316;color:#fff;border:none;border-radius:9px;
                    font-size:14px;font-weight:600;cursor:pointer}}
.confirm-no{{padding:10px 24px;background:#222;color:#aaa;border:1px solid #333;
             border-radius:9px;font-size:14px;cursor:pointer}}
.msg{{padding:12px 16px;border-radius:10px;font-size:14px;margin-top:14px;display:none}}
.msg-ok{{background:#052e16;color:#22c55e;border:1px solid #14532d}}
.msg-err{{background:#2d0a0a;color:#ef4444;border:1px solid #7f1d1d}}
</style></head>
<body>
<div class="topbar">
  <div>
    <div class="logo-name">Pas<span class="logo-dot">baan</span> <span style="color:#555;font-weight:400">Admin</span></div>
    <div class="logo-sub">Pasbaan Pakistan Dashboard</div>
  </div>
  <a href="/admin/subscriptions" class="logout" style="margin-right:8px">💳 Subscriptions</a>
  <a href="/admin/logout" class="logout">Logout</a>
</div>

<div class="main">

  <!-- Stats -->
  <div class="stats">
    <div class="stat">
      <div class="stat-val">{stats['total']}</div>
      <div class="stat-label">Total QR Codes</div>
    </div>
    <div class="stat">
      <div class="stat-val" style="color:#22c55e">{stats['active']}</div>
      <div class="stat-label">Active (set up)</div>
    </div>
    <div class="stat">
      <div class="stat-val" style="color:#eab308">{stats['unclaimed']}</div>
      <div class="stat-label">Unclaimed</div>
    </div>
    <div class="stat">
      <div class="stat-val">{stats['total_scans']}</div>
      <div class="stat-label">Total Scans</div>
    </div>
    <div class="stat">
      <div class="stat-label" style="margin-bottom:6px">Next QR ID</div>
      <div class="next-id">{stats['next_id']}</div>
    </div>
  </div>

  <!-- Generate -->
  <div class="card">
    <div class="card-title">Generate New QR Codes</div>
    <div class="row2">
      <div>
        <label>Quantity</label>
        <input type="number" id="qty" value="1" min="1" max="500" placeholder="10">
      </div>
      <div>
        <label>Colour Theme</label>
        <div class="theme-grid" style="margin-top:2px" id="theme-grid">
          {theme_options}
        </div>
      </div>
    </div>
    <button class="btn-gen" id="gen-btn" onclick="generateCodes()">
      Generate QR Codes ↗
    </button>
    <div class="msg msg-ok" id="msg-ok"></div>
    <div class="msg msg-err" id="msg-err"></div>
    <div class="result-box" id="result-box">
      <div class="result-title">Generated Codes</div>
      <div id="result-list"></div>
    </div>
  </div>

  <!-- QR Table -->
  <div class="card">
    <div class="card-title">All QR Codes ({total} total)</div>
    <table>
      <thead>
        <tr>
          <th>QR ID</th><th>Status</th><th>Scans</th><th>Theme</th><th>Actions</th>
        </tr>
      </thead>
      <tbody id="qr-tbody">{rows_html}</tbody>
    </table>
    {pagination_html}
  </div>

</div>

<!-- Confirm Overlay -->
<div class="confirm-overlay" id="confirm-overlay">
  <div class="confirm-box">
    <div class="confirm-title" id="confirm-title">Are you sure?</div>
    <div class="confirm-msg" id="confirm-msg"></div>
    <div class="confirm-actions">
      <button class="confirm-no" onclick="closeConfirm()">Cancel</button>
      <button id="confirm-yes-btn" class="confirm-yes-del" onclick="confirmAction()">Confirm</button>
    </div>
  </div>
</div>

<script>
let selectedTheme = 'yellow';
let currentPage   = {page};
let currentPer    = {per_page};

// ── Theme picker ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {{ selectTheme('yellow'); }});

function selectTheme(name) {{
  selectedTheme = name;
  document.querySelectorAll('.theme-card').forEach(c => {{
    c.classList.toggle('selected', c.dataset.theme === name);
  }});
}}

// ── Generate ──────────────────────────────────────────────────────────────
async function generateCodes() {{
  const qty    = parseInt(document.getElementById('qty').value);
  const btn    = document.getElementById('gen-btn');
  const msgOk  = document.getElementById('msg-ok');
  const msgErr = document.getElementById('msg-err');
  const box    = document.getElementById('result-box');
  const list   = document.getElementById('result-list');

  msgOk.style.display  = 'none';
  msgErr.style.display = 'none';
  box.style.display    = 'none';

  if (!qty || qty < 1 || qty > 500) {{
    msgErr.textContent = 'Please enter a quantity between 1 and 500.';
    msgErr.style.display = 'block';
    return;
  }}

  btn.disabled    = true;
  btn.textContent = 'Generating...';

  try {{
    const r    = await fetch(`/admin/generate-qr?count=${{qty}}&theme=${{selectedTheme}}`);
    const data = await r.json();

    if (!r.ok) {{
      msgErr.textContent = 'Error: ' + (data.detail || 'Unknown error');
      msgErr.style.display = 'block';
      return;
    }}

    msgOk.textContent = `✓ ${{data.generated}} QR code(s) generated — downloading ZIP...`;
    msgOk.style.display = 'block';

    const ids = data.codes.map(c => c.qr_id).join(',');
    const a   = document.createElement('a');
    a.href    = `/admin/download-zip?qr_ids=${{encodeURIComponent(ids)}}`;
    a.download = 'pasbaan_stickers.zip';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);

    list.innerHTML = data.codes.map(c => `
      <div class="qr-result">
        <span class="mono">${{c.qr_id}}</span>
        <div>
          <a href="${{c.sticker_url}}" class="dl-btn" download="${{c.qr_id}}.png">⬇ PNG</a>
          <a href="${{c.scan_url}}" class="dl-btn" target="_blank">👁 Preview</a>
        </div>
      </div>
    `).join('');

    box.style.display = 'block';
    setTimeout(() => location.reload(), 4000);

  }} catch(e) {{
    msgErr.textContent = 'Network error: ' + e.message;
    msgErr.style.display = 'block';
  }} finally {{
    btn.disabled    = false;
    btn.textContent = 'Generate QR Codes ↗';
  }}
}}

// ── Pagination ────────────────────────────────────────────────────────────
function goPage(p) {{
  const url = new URL(window.location.href);
  url.searchParams.set('page', p);
  url.searchParams.set('per_page', currentPer);
  window.location.href = url.toString();
}}

function changePerPage(val) {{
  const url = new URL(window.location.href);
  url.searchParams.set('page', 1);
  url.searchParams.set('per_page', val);
  window.location.href = url.toString();
}}

// ── Confirm overlay ───────────────────────────────────────────────────────
let _pendingAction = null;

function closeConfirm() {{
  document.getElementById('confirm-overlay').classList.remove('open');
  _pendingAction = null;
}}

async function confirmAction() {{
  if (_pendingAction) await _pendingAction();
  closeConfirm();
}}

function deleteCode(qr_id) {{
  document.getElementById('confirm-title').textContent = 'Delete QR Code?';
  document.getElementById('confirm-msg').innerHTML =
    `This will <strong>permanently delete</strong> <code>${{qr_id}}</code> from the database.<br>
     If activated, all owner data will be lost. This cannot be undone.`;
  const btn = document.getElementById('confirm-yes-btn');
  btn.className = 'confirm-yes-del';
  btn.textContent = 'Yes, Delete';

  _pendingAction = async () => {{
    const r = await fetch(`/admin/qr/${{qr_id}}`, {{ method: 'DELETE' }});
    if (r.ok) {{
      const row = document.getElementById(`row-${{qr_id}}`);
      if (row) row.remove();
    }} else {{
      alert('Delete failed. Please try again.');
    }}
  }};

  document.getElementById('confirm-overlay').classList.add('open');
}}

function resetCode(qr_id) {{
  document.getElementById('confirm-title').textContent = 'Reset QR Code?';
  document.getElementById('confirm-msg').innerHTML =
    `This will <strong>clear all owner data</strong> from <code>${{qr_id}}</code> and reset scan count to 0.<br>
     The sticker ID is kept — the next scanner can set it up again.`;
  const btn = document.getElementById('confirm-yes-btn');
  btn.className = 'confirm-yes-reset';
  btn.textContent = 'Yes, Reset';

  _pendingAction = async () => {{
    const r = await fetch(`/admin/qr/${{qr_id}}/reset`, {{ method: 'POST' }});
    if (r.ok) {{
      setTimeout(() => location.reload(), 300);
    }} else {{
      alert('Reset failed. Please try again.');
    }}
  }};

  document.getElementById('confirm-overlay').classList.add('open');
}}
</script>
</body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC HTML PAGES
# ─────────────────────────────────────────────────────────────────────────────

def _css() -> str:
    return """
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0f0f0f">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f5f5f0;min-height:100vh;padding:20px 16px 48px;color:#111}
.wrap{max-width:460px;margin:0 auto}
.logo{text-align:center;padding:12px 0 24px}
.logo-name{font-size:22px;font-weight:700;letter-spacing:-.4px}
.logo-dot{color:#b90a0a}
.logo-sub{font-size:11px;color:#aaa;margin-top:3px;letter-spacing:.05em;text-transform:uppercase}
.card{background:#fff;border-radius:18px;padding:22px 20px;margin-bottom:14px;
      box-shadow:0 1px 4px rgba(0,0,0,.06)}
.sec-title{font-size:10px;font-weight:700;letter-spacing:.1em;
           text-transform:uppercase;color:#aaa;margin-bottom:14px}
label{display:block;font-size:13px;color:#777;margin:12px 0 5px}
label:first-of-type{margin-top:0}
input,select,textarea{width:100%;padding:11px 14px;border:1.5px solid #ebebeb;
  border-radius:11px;font-size:15px;color:#111;background:#fafafa;
  outline:none;transition:border-color .15s;font-family:inherit}
input:focus,select:focus,textarea:focus{border-color:#111;background:#fff}
textarea{height:78px;resize:vertical}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.btn{display:block;width:100%;padding:14px;border:none;border-radius:13px;
     font-size:16px;font-weight:600;cursor:pointer;text-align:center;
     text-decoration:none;transition:opacity .15s,transform .1s;margin-top:6px}
.btn:active{transform:scale(.98)}
.btn-dark{background:#111;color:#fff}.btn-dark:hover{opacity:.86}
.btn-ghost{background:transparent;border:1.5px solid #e0e0e0;color:#111;font-size:14px;padding:11px}
.badge{display:inline-flex;align-items:center;gap:5px;padding:4px 12px;
       border-radius:20px;font-size:12px;font-weight:600;margin-bottom:14px}
.badge-amber{background:#fef3c7;color:#92400e}
.badge-green{background:#dcfce7;color:#166534}
.call-btn{display:flex;align-items:center;gap:14px;padding:15px 16px;
          background:#f9f9f6;border:1.5px solid #ebebeb;border-radius:13px;
          text-decoration:none;color:#111;margin-bottom:10px;transition:background .12s}
.call-btn:active{background:#f0f0ec}
.c-icon{width:42px;height:42px;border-radius:50%;display:flex;align-items:center;
        justify-content:center;font-size:20px;flex-shrink:0}
.c-green{background:#dcfce7}.c-blue{background:#dbeafe}.c-amber{background:#fef3c7}
.c-rel{font-size:11px;color:#aaa;margin-bottom:2px}
.c-name{font-size:15px;font-weight:600}
.c-arrow{font-size:20px;color:#ccc;margin-left:auto}
.plate{background:#111;color:#fff;padding:6px 18px;border-radius:8px;
       font-family:monospace;font-size:21px;font-weight:700;
       letter-spacing:3px;display:inline-block;margin:8px 0 4px}
.msg-box{background:#fafaf7;border-left:3px solid #111;padding:12px 14px;
         border-radius:0 10px 10px 0;font-size:14px;color:#555;line-height:1.65}
.emer-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.emer-btn{display:flex;align-items:center;gap:8px;padding:10px 12px;
          background:#fff1f2;border:1.5px solid #fecdd3;border-radius:11px;
          text-decoration:none;color:#9f1239;font-size:13px;font-weight:600}
.note{font-size:11px;color:#ccc;text-align:center;margin-top:10px}
code{font-family:monospace;background:#f3f3f0;padding:2px 7px;border-radius:5px;font-size:13px}
#loc-btn:not([disabled]):hover{opacity:.88;transform:translateY(-1px)}
#loc-btn:not([disabled]):active{transform:scale(.98)}
</style>"""


def _logo() -> str:
    return """<div class="logo">
  <div class="logo-name">Pas<span class="logo-dot">baan</span></div>
  <div class="logo-sub">Vehicle Emergency Contact System</div>
</div>"""


def page_choose_plan(qr_id: str) -> str:
    """Plan selection page — clean professional redesign."""
    return f"""<!DOCTYPE html><html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Choose Plan — Pasbaan</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:#f0f0eb;min-height:100vh;padding:24px 16px 48px;color:#111;
}}
.wrap{{max-width:420px;margin:0 auto}}

/* Logo */
.logo{{text-align:center;padding:8px 0 22px}}
.logo-name{{font-size:22px;font-weight:700;letter-spacing:-.4px}}
.logo-dot{{color:#b90a0a}}
.logo-sub{{font-size:11px;color:#aaa;margin-top:3px;letter-spacing:.05em;text-transform:uppercase}}

/* Page heading */
.page-title{{text-align:center;margin-bottom:22px}}
.page-title h2{{font-size:21px;font-weight:800;color:#111;margin-bottom:5px}}
.page-title p{{font-size:13px;color:#888;line-height:1.55}}
.qr-chip{{display:inline-block;margin-top:8px;padding:3px 12px;
          background:#fff;border:1px solid #e0e0e0;border-radius:20px;
          font-size:11px;color:#aaa;font-family:monospace}}

/* Plan cards */
.plan{{background:#fff;border-radius:18px;overflow:hidden;
       margin-bottom:14px;border:2px solid transparent;
       box-shadow:0 1px 4px rgba(0,0,0,.06);position:relative}}
.plan.basic{{border-color:#d1fae5}}
.plan.premium{{border-color:#ddd6fe}}

.plan-top{{padding:20px 20px 16px}}
.plan-top.basic-top{{background:linear-gradient(135deg,#f0fdf4,#dcfce7)}}
.plan-top.premium-top{{background:linear-gradient(135deg,#faf5ff,#ede9fe)}}

.plan-label{{display:flex;align-items:center;gap:8px;margin-bottom:10px}}
.plan-pill{{padding:3px 11px;border-radius:20px;font-size:11px;font-weight:700;letter-spacing:.05em}}
.rec-tag{{margin-left:auto;font-size:10px;font-weight:700;padding:2px 10px;
          border-radius:20px;background:#7c3aed;color:#fff;letter-spacing:.04em}}

.plan-price-row{{display:flex;align-items:baseline;gap:5px}}
.plan-price{{font-size:30px;font-weight:800;color:#111;line-height:1}}
.plan-period{{font-size:13px;color:#888;font-weight:400}}
.plan-free-note{{font-size:12px;font-weight:600;margin-top:5px}}

/* Divider */
.plan-divider{{height:1px;background:#f0f0ee;margin:0 20px}}

/* Feature section */
.plan-body{{padding:16px 20px}}

/* WHO PAYS — the key differentiator */
.who-pays-box{{
  border-radius:11px;padding:12px 14px;margin-bottom:14px;
  display:flex;gap:11px;align-items:flex-start;
}}
.wp-icon{{font-size:20px;flex-shrink:0;margin-top:1px;line-height:1}}
.wp-label{{font-size:10px;font-weight:700;text-transform:uppercase;
           letter-spacing:.07em;margin-bottom:4px}}
.wp-desc{{font-size:12px;line-height:1.6}}

/* Feature list */
.feat-list{{list-style:none;margin-bottom:16px}}
.feat-list li{{display:flex;align-items:flex-start;gap:9px;
               font-size:13px;color:#444;padding:5px 0;
               border-bottom:1px solid #f5f5f3;line-height:1.4}}
.feat-list li:last-child{{border-bottom:none}}
.feat-list .fi{{font-size:14px;flex-shrink:0;margin-top:1px}}

/* Pack pricing */
.pack-grid{{
  display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-bottom:14px;
}}
.pack-item{{
  background:#f5f0ff;border:1.5px solid #ddd6fe;border-radius:10px;
  padding:9px 11px;
}}
.pack-calls{{font-size:13px;font-weight:700;color:#6d28d9}}
.pack-price{{font-size:12px;color:#888;margin-top:2px}}
.pack-no-exp{{font-size:11px;color:#7c3aed;margin-top:3px;font-weight:600}}

/* CTA button */
.plan-btn{{
  width:100%;padding:14px;border:none;border-radius:12px;
  font-size:15px;font-weight:700;cursor:pointer;font-family:inherit;
  transition:opacity .15s,transform .1s;letter-spacing:.01em;
}}
.plan-btn:active{{transform:scale(.97)}}

/* FAQ */
.faq{{background:#fff;border-radius:16px;padding:18px 18px;
      margin-top:6px;box-shadow:0 1px 4px rgba(0,0,0,.05)}}
.faq-title{{font-size:10px;font-weight:700;text-transform:uppercase;
            letter-spacing:.08em;color:#bbb;margin-bottom:12px}}
.faq-q{{font-size:13px;font-weight:600;color:#111;margin-bottom:3px}}
.faq-a{{font-size:12px;color:#777;line-height:1.6;margin-bottom:12px}}
.faq-a:last-child{{margin-bottom:0}}

.foot-note{{font-size:11px;color:#bbb;text-align:center;margin-top:16px;line-height:1.6}}
</style>
</head>
<body>
<div class="wrap">

  <!-- Logo -->
  <div class="logo">
    <div class="logo-name">Pas<span class="logo-dot">baan</span></div>
    <div class="logo-sub">Vehicle Emergency System</div>
  </div>

  <!-- Heading -->
  <div class="page-title">
    <h2>Choose Your Plan</h2>
    <p>Both plans are free for the first 6 months.<br>No payment needed right now.</p>
    <span class="qr-chip">{qr_id}</span>
  </div>

  <!-- ── BASIC PLAN ── -->
  <div class="plan basic">
    <div class="plan-top basic-top">
      <div class="plan-label">
        <span class="plan-pill" style="background:#d1fae5;color:#065f46">🔵 BASIC</span>
      </div>
      <div class="plan-price-row">
        <span class="plan-price">Rs. 399</span>
        <span class="plan-period">/ 6 months</span>
      </div>
      <p class="plan-free-note" style="color:#059669">Free for first 6 months</p>
    </div>
    <div class="plan-divider"></div>
    <div class="plan-body">

      <!-- Who pays callout -->
      <div class="who-pays-box" style="background:#fefce8;border:1.5px solid #fde68a;">
        <div class="wp-icon">📞</div>
        <div>
          <div class="wp-label" style="color:#92400e">Who pays for calls?</div>
          <div class="wp-desc" style="color:#78350f">
            The <strong>scanner pays</strong> — they call you directly from their own phone using their own airtime.
            Your numbers are <strong>visible</strong> on the contact page.
          </div>
        </div>
      </div>

      <ul class="feat-list">
        <li><span class="fi">✅</span><span>Emergency contact page when sticker is scanned</span></li>
        <li><span class="fi">✅</span><span>Up to 3 contacts with direct call buttons</span></li>
        <li><span class="fi">✅</span><span>Live GPS location sharing via WhatsApp</span></li>
        <li><span class="fi">✅</span><span>Pakistan emergency numbers (1122, Police, Edhi)</span></li>
        <li><span class="fi">✅</span><span>Deactivate / reactivate anytime</span></li>
        <li><span class="fi">⚠️</span><span style="color:#b45309"><strong>Phone numbers publicly visible</strong> to every scanner</span></li>
      </ul>

      <form method="POST" action="/scan/{qr_id}/select-plan">
        <input type="hidden" name="plan" value="basic">
        <button type="submit" class="plan-btn" style="background:#111;color:#fff;">
          Continue with Basic &rarr;
        </button>
      </form>
    </div>
  </div>

  <!-- ── PREMIUM PLAN ── -->
  <div class="plan premium">
    <div class="plan-top premium-top">
      <div class="plan-label">
        <span class="plan-pill" style="background:linear-gradient(135deg,#7c3aed,#6d28d9);color:#fff">👑 PREMIUM</span>
        <span class="rec-tag">RECOMMENDED</span>
      </div>
      <div class="plan-price-row">
        <span class="plan-price">Rs. 399</span>
        <span class="plan-period">/ 6 months</span>
      </div>
      <p class="plan-free-note" style="color:#7c3aed">Free for first 6 months &nbsp;·&nbsp; <span style="font-weight:400;color:#9ca3af">+ call packs</span></p>
    </div>
    <div class="plan-divider"></div>
    <div class="plan-body">

      <!-- Who pays callout -->
      <div class="who-pays-box" style="background:#f0fdf4;border:1.5px solid #86efac;">
        <div class="wp-icon">🔒</div>
        <div>
          <div class="wp-label" style="color:#166534">Who pays for calls?</div>
          <div class="wp-desc" style="color:#15803d">
            <strong>You pay</strong> — from your pre-bought call pack.
            The scanner calls through Pasbaan's masked line, <strong>completely free for them</strong>.
            Your real numbers are <strong>never revealed</strong> to anyone.
          </div>
        </div>
      </div>

      <ul class="feat-list">
        <li><span class="fi">✅</span><span>Everything in Basic</span></li>
        <li><span class="fi">🔒</span><span><strong>Numbers always private</strong> — hidden from every scanner</span></li>
        <li><span class="fi">📞</span><span>Masked calling — scanner calls free through Pasbaan</span></li>
        <li><span class="fi">📦</span><span>Buy call packs anytime — <strong>no expiry ever</strong></span></li>
        <li><span class="fi">🛡️</span><span>Full privacy for your family's numbers</span></li>
      </ul>

      <!-- Call Pack grid -->
      <p style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;
                color:#9ca3af;margin-bottom:8px">Call Pack Options</p>
      <div class="pack-grid">
        <div class="pack-item">
          <div class="pack-calls">20 calls</div>
          <div class="pack-price">Rs. 400</div>
          <div class="pack-no-exp">No expiry</div>
        </div>
        <div class="pack-item">
          <div class="pack-calls">50 calls</div>
          <div class="pack-price">Rs. 1,000</div>
          <div class="pack-no-exp">No expiry</div>
        </div>
        <div class="pack-item">
          <div class="pack-calls">100 calls</div>
          <div class="pack-price">Rs. 2,000</div>
          <div class="pack-no-exp">No expiry</div>
        </div>
        <div class="pack-item">
          <div class="pack-calls">200 calls</div>
          <div class="pack-price">Rs. 4,000</div>
          <div class="pack-no-exp">No expiry</div>
        </div>
      </div>
      <p style="font-size:11px;color:#9ca3af;margin-bottom:14px;line-height:1.5">
        Each call = 2 minutes. Buy once, use at your own pace.
      </p>

      <form method="POST" action="/scan/{qr_id}/select-plan">
        <input type="hidden" name="plan" value="premium">
        <button type="submit" class="plan-btn"
          style="background:linear-gradient(135deg,#7c3aed,#6d28d9);color:#fff;
                 box-shadow:0 4px 16px rgba(109,40,217,.3);">
          Continue with Premium &rarr;
        </button>
      </form>
    </div>
  </div>

  <!-- FAQ -->
  <div class="faq">
    <div class="faq-title">Common Questions</div>

    <div class="faq-q">Can I upgrade from Basic to Premium later?</div>
    <div class="faq-a">Yes, anytime from your contact page. Your existing contacts and details stay intact.</div>

    <div class="faq-q">What if my call pack balance runs out?</div>
    <div class="faq-a">The scanner sees a WhatsApp fallback option and you get an alert to top up. No calls are lost mid-conversation.</div>

    <div class="faq-q">Do call packs expire?</div>
    <div class="faq-a">Never. Buy 200 calls today and use the last one two years from now — completely fine.</div>
  </div>

  <p class="foot-note">
    Pasbaan Pakistan &nbsp;·&nbsp; Secure Vehicle Emergency System<br>
    Both plans free for first 6 months — no card needed now
  </p>

</div>
</body></html>"""


def page_setup(qr_id: str, plan: str = "basic") -> str:
    plan_banner = (
        '''<div class="card" style="background:linear-gradient(135deg,#faf5ff,#ede9fe);'''
        '''border:1.5px solid #a78bfa;padding:14px 16px;">'''
        '''<div style="display:flex;align-items:center;gap:10px;">'''
        '''<span style="font-size:22px">👑</span>'''
        '''<div><p style="font-size:13px;font-weight:700;color:#6d28d9;margin-bottom:2px">Premium Plan Selected</p>'''
        '''<p style="font-size:12px;color:#7c3aed;line-height:1.5">'''
        '''Your phone numbers will be <strong>private</strong>. Call packs can be purchased after activation.</p></div></div></div>'''
        if plan == "premium" else
        '''<div class="card" style="background:#f0fdf4;border:1.5px solid #86efac;padding:14px 16px;">'''
        '''<div style="display:flex;align-items:center;gap:10px;">'''
        '''<span style="font-size:22px">🔵</span>'''
        '''<div><p style="font-size:13px;font-weight:700;color:#166534;margin-bottom:2px">Basic Plan Selected</p>'''
        '''<p style="font-size:12px;color:#16a34a;line-height:1.5">'''
        '''Your phone numbers will be <strong>visible</strong> to anyone who scans your sticker.</p></div></div></div>'''
    )
    return f"""<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8">{_css()}<title>Activate Pasbaan — {qr_id}</title></head>
<body><div class="wrap">{_logo()}
{plan_banner}
<div class="card">
  <span class="badge badge-amber">⚡ First scan — Activate your Pasbaan sticker</span>
  <p style="font-size:15px;line-height:1.65;color:#555">
    Welcome! Fill in your emergency contacts below —
    they'll appear whenever someone scans your sticker.
  </p>
  <p style="font-size:11px;color:#ccc;margin-top:10px">Sticker ID: <code>{qr_id}</code></p>
</div>
<div class="card" style="background:#f0fdf4;border:1.5px solid #86efac;">
  <div class="sec-title" style="color:#166534">📍 WhatsApp Location Feature</div>
  <p style="font-size:13px;color:#166534;line-height:1.7;margin-bottom:8px">
    Pasbaan can send the <strong>live location</strong> of your vehicle to your contacts via WhatsApp
    whenever someone scans your sticker.
  </p>
  <p style="font-size:13px;color:#166534;line-height:1.7;">
    ✅ Please make sure the phone numbers you enter below are <strong>registered on WhatsApp</strong>,
    and tick the WhatsApp checkbox for each one. Only ticked numbers will receive the location message.
  </p>
  <p style="font-size:12px;color:#dc2626;margin-top:8px;line-height:1.6;
     background:#fef2f2;padding:8px 10px;border-radius:8px;border:1px solid #fecaca;">
    ⚠️ <strong>Important:</strong> If a number is ticked but not actually registered on WhatsApp,
    the location message will not be delivered to that person.
    An SMS fallback option will be available for the scanner to use manually.
  </p>
</div>
<form action="/scan/{qr_id}/setup" method="POST">
  <div class="card" style="background:linear-gradient(135deg,#f8faff 0%,#eef2ff 100%);border:1.5px solid #c7d2fe;">
    <div class="sec-title" style="color:#3730a3">🚗 Your Vehicle</div>
    <label>Your full name <span style="color:#94a3b8;font-size:11px;">(optional)</span></label>
    <input name="owner_name" type="text" placeholder="Ahmed Raza">
    <div class="row2">
      <div><label>Vehicle number <span style="color:#94a3b8;font-size:11px;">(optional)</span></label>
           <input name="vehicle_number" type="text" placeholder="LHR-1234"></div>
      <div><label>City <span style="color:#94a3b8;font-size:11px;">(optional)</span></label>
           <input name="city" type="text" placeholder="Lahore"></div>
    </div>
    <label style="margin-top:10px;">Your own phone number <span style="color:#94a3b8;font-size:11px;">(optional)</span></label>
    <input name="owner_phone" type="tel" placeholder="+92 300 1234567">
    <div style="display:flex;align-items:center;gap:8px;margin-top:10px;padding:10px 12px;
                background:#f0fdf4;border-radius:10px;border:1px solid #86efac;">
      <input type="checkbox" name="owner_whatsapp" value="yes" id="wa_owner"
             style="width:18px;height:18px;cursor:pointer;accent-color:#16a34a;flex-shrink:0;">
      <label for="wa_owner" style="font-size:13px;color:#166534;cursor:pointer;margin:0;font-weight:600;">
        💬 My number is on WhatsApp (for location sharing)
      </label>
    </div>
    <p style="font-size:11px;color:#6366f1;margin-top:6px;line-height:1.5;background:#eef2ff;padding:8px 10px;border-radius:8px;">
      📞 This number will appear as the <strong>"Call Vehicle Owner"</strong> button. If ticked above, it can also receive live location via WhatsApp.
    </p>
  </div>
  <div class="card" style="border:1.5px solid #ddd6fe;">
    <div class="sec-title" style="color:#5b21b6">📋 Emergency Contact 1 <span style="font-size:11px;color:#94a3b8;font-weight:400;">(optional)</span></div>
    <label>Relation</label>
    <select name="contact1_relation">
      <option value="">— Skip —</option>
      <option>Brother</option><option>Sister</option><option>Father</option>
      <option>Mother</option><option>Wife</option><option>Husband</option>
      <option>Son</option><option>Daughter</option><option>Friend</option><option>Other</option>
    </select>
    <label>Full name</label>
    <input name="contact1_name" type="text" placeholder="Full name">
    <label>Phone number</label>
    <input name="contact1_phone" type="tel" placeholder="+92 300 1234567">
    <div style="display:flex;align-items:center;gap:8px;margin-top:10px;padding:10px 12px;
                background:#f0fdf4;border-radius:10px;border:1px solid #86efac;">
      <input type="checkbox" name="contact1_whatsapp" value="yes" id="wa1"
             style="width:18px;height:18px;cursor:pointer;accent-color:#16a34a;flex-shrink:0;">
      <label for="wa1" style="font-size:13px;color:#166534;cursor:pointer;margin:0;font-weight:600;">
        💬 This number is on WhatsApp (for location sharing)
      </label>
    </div>
  </div>
  <div class="card" style="border:1.5px solid #e0e7ff;">
    <div class="sec-title" style="color:#4338ca">📋 Emergency Contact 2 <span style="font-size:11px;color:#94a3b8;font-weight:400;">(optional)</span></div>
    <label>Relation</label>
    <select name="contact2_relation">
      <option value="">— Skip —</option>
      <option>Brother</option><option>Sister</option><option>Father</option>
      <option>Mother</option><option>Wife</option><option>Husband</option>
      <option>Son</option><option>Daughter</option><option>Friend</option><option>Other</option>
    </select>
    <label>Full name</label><input name="contact2_name" type="text" placeholder="Full name">
    <label>Phone number</label><input name="contact2_phone" type="tel" placeholder="+92 300 1234567">
    <div style="display:flex;align-items:center;gap:8px;margin-top:10px;padding:10px 12px;
                background:#f0fdf4;border-radius:10px;border:1px solid #86efac;">
      <input type="checkbox" name="contact2_whatsapp" value="yes" id="wa2"
             style="width:18px;height:18px;cursor:pointer;accent-color:#16a34a;flex-shrink:0;">
      <label for="wa2" style="font-size:13px;color:#166534;cursor:pointer;margin:0;font-weight:600;">
        💬 This number is on WhatsApp (for location sharing)
      </label>
    </div>
  </div>
  <div class="card" style="border:1.5px solid #e0e7ff;">
    <div class="sec-title" style="color:#4338ca">📋 Emergency Contact 3 <span style="font-size:11px;color:#94a3b8;font-weight:400;">(optional)</span></div>
    <label>Relation</label>
    <select name="contact3_relation">
      <option value="">— Skip —</option>
      <option>Brother</option><option>Sister</option><option>Father</option>
      <option>Mother</option><option>Wife</option><option>Husband</option>
      <option>Son</option><option>Daughter</option><option>Friend</option><option>Other</option>
    </select>
    <label>Full name</label><input name="contact3_name" type="text" placeholder="Full name">
    <label>Phone number</label><input name="contact3_phone" type="tel" placeholder="+92 300 1234567">
    <div style="display:flex;align-items:center;gap:8px;margin-top:10px;padding:10px 12px;
                background:#f0fdf4;border-radius:10px;border:1px solid #86efac;">
      <input type="checkbox" name="contact3_whatsapp" value="yes" id="wa3"
             style="width:18px;height:18px;cursor:pointer;accent-color:#16a34a;flex-shrink:0;">
      <label for="wa3" style="font-size:13px;color:#166534;cursor:pointer;margin:0;font-weight:600;">
        💬 This number is on WhatsApp (for location sharing)
      </label>
    </div>
  </div>
  <div class="card" style="border:1.5px solid #e2e8f0;">
    <div class="sec-title" style="color:#475569">💬 Message for finder <span style="font-size:11px;color:#94a3b8;font-weight:400;">(optional)</span></div>
    <textarea name="message" placeholder="e.g. Please contact my family in case of emergency. JazakAllah Khair."></textarea>
  </div>
  <div class="card" style="background:#fafaf7;border:1.5px solid #e0e0e0;">
    <div class="sec-title">🔒 Set Your Owner PIN</div>
    <p style="font-size:13px;color:#555;line-height:1.65;margin-bottom:14px">
      Choose a <strong>4-digit PIN</strong>. You will need this PIN to edit your Pasbaan details later.
      Keep it safe — this is the only way to prove you are the owner.
    </p>
    <div style="background:#fef2f2;border:1.5px solid #fecaca;border-radius:10px;
                padding:12px 14px;margin-bottom:14px;">
      <p style="font-size:13px;color:#dc2626;font-weight:700;margin-bottom:6px;">
        ⚠️ IMPORTANT — Please read before setting your PIN:
      </p>
      <ul style="font-size:12px;color:#b91c1c;line-height:1.8;margin:0;padding-left:18px;">
        <li>Your PIN <strong>cannot be recovered</strong> if forgotten.</li>
        <li>Without your PIN you <strong>cannot edit</strong> your vehicle details in the future.</li>
        <li><strong>There is no way to reset it</strong> — not even by contacting support.</li>
        <li>Write your PIN down and store it in a <strong>safe place</strong>.</li>
      </ul>
    </div>
    <div class="row2">
      <div>
        <label>PIN (4 digits)</label>
        <input name="owner_pin" type="password" inputmode="numeric" maxlength="4"
               pattern="\\d{{4}}" placeholder="••••" required autocomplete="new-password"
               style="letter-spacing:8px;font-size:22px;text-align:center">
      </div>
      <div>
        <label>Confirm PIN</label>
        <input name="owner_pin_confirm" type="password" inputmode="numeric" maxlength="4"
               pattern="\\d{{4}}" placeholder="••••" required autocomplete="new-password"
               style="letter-spacing:8px;font-size:22px;text-align:center">
      </div>
    </div>
    <p style="font-size:11px;color:#aaa;margin-top:8px;">
      ⚠️ There is no way to recover a forgotten PIN. Write it down safely.
    </p>
  </div>
  <button type="submit" class="btn btn-dark">✅ Save &amp; Activate My Pasbaan</button>
  <p class="note" style="margin-top:12px">Phone numbers are never shown as text. They only open your dialer when tapped.</p>
</form></div></body></html>"""


def _to_intl(phone: str) -> str:
    """Convert Pakistani phone to international tel: format (+923XXXXXXXXX)."""
    digits = ''.join(filter(str.isdigit, phone.strip()))
    if digits.startswith("0"):
        digits = "92" + digits[1:]
    elif not digits.startswith("92"):
        digits = "92" + digits
    return "+" + digits


def page_contact(qr_id: str, data: dict, scan_count: int) -> str:
    icons  = ["c-green","c-blue","c-amber"]
    emojis = ["📞","📱","☎️"]
    buttons = ""
    for i, c in enumerate(data.get("contacts", [])):
        buttons += f"""<a href="tel:{_to_intl(c['phone'])}" class="call-btn">
          <div class="c-icon {icons[i%3]}">{emojis[i%3]}</div>
          <div><div class="c-rel">Call {c['relation']}</div>
               <div class="c-name">{c['name']}</div></div>
          <div class="c-arrow">›</div></a>"""

    # Owner direct call card — uses owner's own phone number
    owner_phone = data.get("owner_phone", "") or (data["contacts"][0]["phone"] if data.get("contacts") else "")
    owner_call_card = ""
    if owner_phone:
        owner_call_card = f"""
<div class="card" style="background:linear-gradient(135deg,#1e3a5f 0%,#1d4ed8 100%);border:none;padding:20px;">
  <div class="sec-title" style="color:#bfdbfe;margin-bottom:6px;">📞 Call Vehicle Owner</div>
  <p style="font-size:13px;color:#93c5fd;margin-bottom:14px;line-height:1.6">
    Wrong parking or need to reach the owner urgently? Call them directly.
  </p>
  <a href="tel:{_to_intl(owner_phone)}" style="display:flex;align-items:center;gap:14px;padding:16px 18px;
     background:rgba(255,255,255,.12);backdrop-filter:blur(8px);
     border:1.5px solid rgba(255,255,255,.25);border-radius:14px;
     text-decoration:none;color:#fff;transition:background .15s;cursor:pointer;">
    <div style="width:48px;height:48px;border-radius:50%;background:rgba(255,255,255,.2);
         display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0;">📲</div>
    <div style="flex:1;">
      <div style="font-size:11px;color:#93c5fd;margin-bottom:3px;text-transform:uppercase;letter-spacing:.06em;">Vehicle Owner</div>
      <div style="font-size:17px;font-weight:700;color:#fff;">{data.get('owner_name','')}</div>
    </div>
    <div style="font-size:28px;color:#60a5fa;font-weight:300;">›</div>
  </a>
</div>"""

    # Build WhatsApp number list — only contacts marked as WhatsApp
    wa_numbers = []
    for c in data.get("contacts", []):
        if not c.get("whatsapp"):
            continue
        raw    = c.get("phone", "").strip()
        digits = ''.join(filter(str.isdigit, raw))
        if not digits:
            continue
        # Normalise to international format without leading +
        # e.g. 03001234567 → 923001234567
        #      +923001234567 → 923001234567
        #      923001234567 → 923001234567 (already correct)
        if digits.startswith("0"):
            digits = "92" + digits[1:]
        elif digits.startswith("920"):
            pass  # already correct
        elif not digits.startswith("92"):
            digits = "92" + digits
        # Final sanity: must be 12 digits for Pakistani numbers (92 + 10)
        if len(digits) >= 11:
            wa_numbers.append({"num": digits, "name": c.get("name",""), "rel": c.get("relation","")})

    wa_numbers_js  = json.dumps(wa_numbers)
    vehicle_no     = data.get('vehicle_number', 'Unknown')
    owner_name_str = data.get('owner_name', 'Unknown')
    owner_name_js  = json.dumps(owner_name_str)
    city_str       = data.get('city', '')

    loc_btn_disabled = 'disabled' if not wa_numbers else ''
    loc_btn_color    = '#9ca3af' if not wa_numbers else '#16a34a'
    loc_btn_cursor   = 'not-allowed' if not wa_numbers else 'pointer'

    # Build the list of WhatsApp contact names to show inside the location card
    if wa_numbers:
        wa_names_html = "".join(
            f'<div style="display:flex;align-items:center;gap:6px;padding:5px 0;'
            f'border-bottom:1px solid #bbf7d0;font-size:13px;color:#166534;">'
            f'<span>💬</span><span><strong>{w["name"]}</strong> ({w["rel"]})</span></div>'
            for w in wa_numbers
        )
        wa_list_html = f'''<div style="margin:10px 0 12px;border-radius:8px;
            overflow:hidden;border:1px solid #bbf7d0;">{wa_names_html}</div>'''
    else:
        wa_list_html = '''<div style="font-size:13px;color:#dc2626;margin-bottom:12px;
            padding:8px 10px;background:#fef2f2;border-radius:8px;">
            ⚠️ No WhatsApp numbers registered. Location sharing is unavailable.</div>'''

    msg = ""
    if data.get("message"):
        msg = f'<div class="card"><div class="sec-title">Message from owner</div><div class="msg-box">{data["message"]}</div></div>'

    # Owner number for location sharing
    # owner_wa_num  — set only when owner marked their number as WhatsApp
    # owner_sms_num — always set if owner has a phone (for SMS fallback)
    owner_wa_num  = ""
    owner_sms_num = ""
    if owner_phone:
        raw_ow = ''.join(filter(str.isdigit, owner_phone.strip()))
        if raw_ow.startswith("0"):
            raw_ow = "92" + raw_ow[1:]
        elif raw_ow.startswith("92") and len(raw_ow) >= 12:
            pass
        elif not raw_ow.startswith("92"):
            raw_ow = "92" + raw_ow
        if len(raw_ow) >= 11:
            owner_sms_num = raw_ow
            if data.get("owner_whatsapp"):
                owner_wa_num = raw_ow

    return f"""<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">{_css()}<title>Pasbaan — {vehicle_no}</title></head>
<body><div class="wrap">{_logo()}

<!-- HERO VEHICLE CARD -->
<div class="card" style="text-align:center;padding:26px 20px;
     background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 100%);border:none;">
  <span class="badge" style="background:rgba(34,197,94,.15);color:#4ade80;border:1px solid rgba(34,197,94,.3);">✓ Verified Pasbaan Vehicle</span><br>
  <div class="plate" style="background:#fff;color:#0f172a;margin-top:12px">{vehicle_no}</div>
  <p style="font-size:18px;font-weight:700;margin-top:8px;color:#fff">{owner_name_str}</p>
  <p style="font-size:13px;color:#94a3b8;margin-top:3px">📍 {city_str}</p>
</div>

<!-- CALL OWNER CARD (prominent, at the top) -->
{owner_call_card}

<!-- LIVE LOCATION CARD (moved up — before emergency numbers) -->
<div class="card" style="background:linear-gradient(135deg,#f0fdf4 0%,#dcfce7 100%);
     border:1.5px solid #86efac;overflow:hidden;position:relative;">
  <div style="position:absolute;top:-18px;right:-18px;font-size:80px;opacity:.07;
              pointer-events:none;line-height:1">📍</div>
  <div class="sec-title" style="color:#166534">📍 Share Live Location</div>
  <p style="font-size:13px;color:#374151;margin-bottom:10px;line-height:1.65">
    Get your current GPS location and send it to the vehicle owner and/or registered WhatsApp contacts.
  </p>
  {wa_list_html}
  <button id="loc-btn" onclick="sendLocation()" {loc_btn_disabled}
    style="width:100%;padding:15px 20px;background:{loc_btn_color};
           color:#fff;border:none;border-radius:13px;font-size:15px;font-weight:700;
           cursor:{loc_btn_cursor};letter-spacing:.01em;
           box-shadow:{'0 4px 14px rgba(22,163,74,.35)' if not loc_btn_disabled else 'none'};
           transition:opacity .15s,transform .1s,box-shadow .15s;
           display:flex;align-items:center;justify-content:center;gap:10px;font-family:inherit;">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0">
      <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/>
      <circle cx="12" cy="10" r="3"/>
    </svg>
    <span id="loc-btn-text">Send Location via WhatsApp</span>
  </button>
  {'<button id="owner-loc-btn" onclick="sendLocationToOwner()" style="width:100%;margin-top:8px;padding:13px 20px;background:linear-gradient(135deg,#1d4ed8,#2563eb);color:#fff;border:none;border-radius:13px;font-size:14px;font-weight:700;cursor:pointer;letter-spacing:.01em;box-shadow:0 4px 14px rgba(37,99,235,.35);transition:opacity .15s,transform .1s;display:flex;align-items:center;justify-content:center;gap:10px;font-family:inherit;"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg><span id="owner-loc-btn-text">📲 Send Location to Vehicle Owner</span></button>' if (owner_wa_num or owner_sms_num) else ''}
  <div id="loc-status" style="font-size:13px;margin-top:12px;padding:10px 12px;
       border-radius:10px;text-align:center;display:none;line-height:1.55;
       background:rgba(255,255,255,.7);border:1px solid rgba(134,239,172,.5);">
  </div>
  <div id="contact-send-list" style="display:none;margin-top:12px;
       background:#fff;border-radius:12px;overflow:hidden;border:1.5px solid #bbf7d0;">
  </div>
</div>

<!-- EMERGENCY CONTACTS -->
<div class="card" style="border:1.5px solid #e0e7ff;">
  <div class="sec-title" style="color:#3730a3">🆘 Emergency Contacts</div>
  <p style="font-size:13px;color:#6b7280;margin-bottom:14px">Tap any button to call directly.</p>
  {buttons}
</div>
{msg}

<!-- PAKISTAN EMERGENCY NUMBERS -->
<div class="card" style="background:linear-gradient(135deg,#fff1f2 0%,#ffe4e6 100%);border:1.5px solid #fecdd3;">
  <div class="sec-title" style="color:#9f1239">🚨 Pakistan Emergency Numbers</div>
  <div class="emer-grid">
    <a href="tel:1122" class="emer-btn" style="background:#fff;border:1.5px solid #fda4af;">🚑 Rescue 1122</a>
    <a href="tel:115"  class="emer-btn" style="background:#fff;border:1.5px solid #fda4af;">🏥 Edhi 115</a>
    <a href="tel:15"   class="emer-btn" style="background:#fff;border:1.5px solid #fda4af;">👮 Police 15</a>
    <a href="tel:1021" class="emer-btn" style="background:#fff;border:1.5px solid #fda4af;">🔥 Fire 1021</a>
  </div>
</div>

<p class="note">{qr_id} · scan #{scan_count}</p>

<!-- Owner access button — triggers PIN modal -->
<button onclick="document.getElementById('owner-modal').style.display='flex'"
  style="width:100%;margin-top:10px;padding:11px;background:transparent;
         border:1.5px solid #e0e0e0;border-radius:13px;font-size:13px;
         color:#aaa;cursor:pointer;font-family:inherit">
  🔑 I am the owner — edit my details
</button>
<button onclick="document.getElementById('deactivate-modal').style.display='flex'"
  style="width:100%;margin-top:6px;padding:11px;background:transparent;
         border:1.5px solid #fca5a5;border-radius:13px;font-size:13px;
         color:#dc2626;cursor:pointer;font-family:inherit">
  🔴 Temporarily deactivate my QR code
</button>

<!-- PIN Modal overlay -->
<div id="owner-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);
     z-index:999;align-items:flex-end;justify-content:center;">
  <div style="background:#fff;border-radius:22px 22px 0 0;padding:28px 24px 36px;
              width:100%;max-width:460px;animation:slideUp .22s ease">
    <div style="text-align:center;margin-bottom:18px">
      <div style="font-size:36px;margin-bottom:8px">🔒</div>
      <h3 style="font-size:17px;font-weight:700;margin-bottom:4px">Owner Verification</h3>
      <p style="font-size:13px;color:#888">Enter your 4-digit PIN to edit your details</p>
    </div>
    <form method="POST" action="/scan/{qr_id}/verify-pin" id="pin-form">
      <input type="hidden" name="next_url" value="/scan/{qr_id}/update">
      <div style="display:flex;justify-content:center;gap:8px;margin-bottom:18px">
        <input type="text" name="pin" id="modal-pin"
               style="width:160px;text-align:center;font-size:28px;font-weight:700;
                      letter-spacing:12px;padding:12px 14px;border:2px solid #111;
                      border-radius:14px;background:#fafafa;color:#111"
               maxlength="4" inputmode="numeric" pattern="\\d{{4}}"
               autocomplete="off" placeholder="••••" required autofocus>
      </div>
      <!-- Numpad -->
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;
                  max-width:220px;margin:0 auto 18px">
        {chr(10).join('<button type="button" onclick="mp(' + chr(39) + d + chr(39) + ')" style="padding:14px;font-size:18px;font-weight:600;background:#f5f5f0;border:1.5px solid #e8e8e8;border-radius:11px;cursor:pointer">' + d + '</button>' for d in ['1','2','3','4','5','6','7','8','9','','0','⌫'])}
      </div>
      <button type="submit" class="btn btn-dark">Unlock & Edit →</button>
    </form>
    <button onclick="document.getElementById('owner-modal').style.display='none'"
      style="width:100%;margin-top:10px;padding:11px;background:transparent;
             border:1.5px solid #e8e8e8;border-radius:13px;font-size:13px;
             color:#aaa;cursor:pointer;font-family:inherit">Cancel</button>
    <p style="font-size:11px;color:#ccc;text-align:center;margin-top:12px;line-height:1.6">
      Forgotten PIN cannot be recovered. Details can no longer be edited.
    </p>
  </div>
</div>
<style>
@keyframes slideUp{{from{{transform:translateY(100%)}}to{{transform:translateY(0)}}}}
</style>
<script>
const mpin = document.getElementById('modal-pin');
function mp(v){{
  if(v==='⌫'){{mpin.value=mpin.value.slice(0,-1);return;}}
  if(mpin.value.length<4) mpin.value+=v;
}}
mpin.addEventListener('input',()=>{{mpin.value=mpin.value.replace(/\\D/g,'').slice(0,4);}});
// Auto-submit when 4 digits entered via keyboard
mpin.addEventListener('input',()=>{{if(mpin.value.length===4) document.getElementById('pin-form').requestSubmit();}});
</script>

<!-- Deactivate PIN modal -->
<div id="deactivate-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);
     z-index:999;align-items:flex-end;justify-content:center;">
  <div style="background:#fff;border-radius:22px 22px 0 0;padding:28px 24px 36px;
              width:100%;max-width:460px;animation:slideUp .22s ease">
    <div style="text-align:center;margin-bottom:18px">
      <div style="font-size:36px;margin-bottom:8px">🔴</div>
      <h3 style="font-size:17px;font-weight:700;margin-bottom:4px;color:#9f1239">Confirm Deactivation</h3>
      <p style="font-size:13px;color:#888">Enter your 4-digit PIN to temporarily deactivate this QR code</p>
    </div>
    <form method="POST" action="/scan/{qr_id}/deactivate" id="deact-form">
      <div style="display:flex;justify-content:center;gap:8px;margin-bottom:18px">
        <input type="text" name="pin" id="deact-pin"
               style="width:160px;text-align:center;font-size:28px;font-weight:700;
                      letter-spacing:12px;padding:12px 14px;border:2px solid #dc2626;
                      border-radius:14px;background:#fff5f5;color:#111"
               maxlength="4" inputmode="numeric" pattern="\\d{{4}}"
               autocomplete="off" placeholder="••••" required>
      </div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;
                  max-width:220px;margin:0 auto 18px">
        {chr(10).join('<button type="button" onclick="dp(' + chr(39) + d + chr(39) + ')" style="padding:14px;font-size:18px;font-weight:600;background:#f5f5f0;border:1.5px solid #e8e8e8;border-radius:11px;cursor:pointer">' + d + '</button>' for d in ['1','2','3','4','5','6','7','8','9','','0','⌫'])}
      </div>
      <button type="submit" style="width:100%;padding:14px;background:#dc2626;color:#fff;
              border:none;border-radius:13px;font-size:15px;font-weight:700;
              cursor:pointer;font-family:inherit">
        🔴 Confirm Deactivate
      </button>
    </form>
    <button onclick="document.getElementById('deactivate-modal').style.display='none'"
      style="width:100%;margin-top:10px;padding:11px;background:transparent;
             border:1.5px solid #e8e8e8;border-radius:13px;font-size:13px;
             color:#aaa;cursor:pointer;font-family:inherit">Cancel</button>
  </div>
</div>
<script>
const dpin = document.getElementById('deact-pin');
function dp(v){{
  if(v==='⌫'){{dpin.value=dpin.value.slice(0,-1);return;}}
  if(dpin.value.length<4) dpin.value+=v;
}}
dpin.addEventListener('input',()=>{{dpin.value=dpin.value.replace(/\\D/g,'').slice(0,4);}});
dpin.addEventListener('input',()=>{{if(dpin.value.length===4) document.getElementById('deact-form').requestSubmit();}});
</script>

<script>
const WA_CONTACTS  = {wa_numbers_js};
const OWNER_WA_NUM = "{owner_wa_num}";
const OWNER_SMS_NUM = "{owner_sms_num}";
const VEHICLE      = "{vehicle_no}";
const OWNER        = {owner_name_js};

function setStatus(msg, color, bgColor) {{
  const el = document.getElementById('loc-status');
  el.innerHTML        = msg;
  el.style.color      = color   || '#166534';
  el.style.background = bgColor || 'rgba(255,255,255,.7)';
  el.style.display    = 'block';
}}

function buildMessage(lat, lng) {{
  const mapsUrl = 'https://maps.google.com/?q=' + lat + ',' + lng;
  return [
    '🚨 *Pasbaan Emergency Alert*',
    '🚗 Vehicle: ' + VEHICLE,
    '👤 Owner: '   + OWNER,
    '',
    '📍 *Current Location:*',
    mapsUrl,
    '',
    '_Sent via Pasbaan Pakistan — Vehicle Emergency Contact System_'
  ].join('\\n');
}}

function showContactButtons(lat, lng) {{
  const waEncoded = encodeURIComponent(buildMessage(lat, lng));
  const container = document.getElementById('contact-send-list');
  container.innerHTML = '';

  WA_CONTACTS.forEach((c, i) => {{
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;' +
      'padding:11px 14px;gap:10px;' +
      (i < WA_CONTACTS.length - 1 ? 'border-bottom:1px solid #dcfce7;' : '');

    const nameDiv = document.createElement('div');
    nameDiv.style.cssText = 'flex:1;min-width:0;';
    nameDiv.innerHTML =
      '<div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;margin-bottom:2px">' + c.rel + '</div>' +
      '<div style="font-size:14px;font-weight:600;color:#111;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + c.name + '</div>';

    const btnGroup = document.createElement('div');
    btnGroup.style.cssText = 'display:flex;gap:6px;flex-shrink:0;';

    const waBtn  = document.createElement('a');
    waBtn.href   = 'https://wa.me/' + c.num.replace(/\\D/g,'') + '?text=' + waEncoded;
    waBtn.target = '_blank';
    waBtn.rel    = 'noopener noreferrer';
    waBtn.style.cssText =
      'padding:8px 13px;background:#16a34a;color:#fff;border-radius:10px;' +
      'font-size:12px;font-weight:700;text-decoration:none;' +
      'display:inline-flex;align-items:center;gap:5px;' +
      'box-shadow:0 2px 8px rgba(22,163,74,.28);';
    waBtn.innerHTML = '💬 WhatsApp';

    const smsBtn  = document.createElement('a');
    smsBtn.href   = 'sms:+' + c.num + '?body=' + waEncoded;
    smsBtn.style.cssText =
      'padding:8px 10px;background:#f3f4f6;color:#374151;border-radius:10px;' +
      'font-size:12px;font-weight:600;text-decoration:none;' +
      'display:inline-flex;align-items:center;gap:4px;border:1.5px solid #e5e7eb;';
    smsBtn.title   = 'SMS fallback if WhatsApp unavailable';
    smsBtn.innerHTML = '📱 SMS';

    btnGroup.appendChild(waBtn);
    btnGroup.appendChild(smsBtn);
    row.appendChild(nameDiv);
    row.appendChild(btnGroup);
    container.appendChild(row);
  }});

  container.style.display = 'block';
  setStatus(
    '✅ <strong>Location ready.</strong> Tap <strong>WhatsApp</strong> to send — or SMS as fallback.',
    '#166534', 'rgba(220,252,231,.8)'
  );
}}

function sendLocation() {{
  const btn     = document.getElementById('loc-btn');
  const btnText = document.getElementById('loc-btn-text');

  if (!navigator.geolocation) {{
    setStatus('❌ GPS is not supported on this browser or device.', '#dc2626', 'rgba(254,242,242,.9)');
    return;
  }}

  if (location.protocol !== 'https:' && location.hostname !== 'localhost') {{
    setStatus(
      '❌ Location requires a secure <strong>HTTPS</strong> connection. ' +
      'Please open this page over https:// and try again.',
      '#dc2626', 'rgba(254,242,242,.9)'
    );
    return;
  }}

  btn.disabled       = true;
  btn.style.background = '#6b7280';
  btn.style.boxShadow  = 'none';
  btn.style.cursor     = 'not-allowed';
  btnText.textContent  = 'Getting GPS location…';
  setStatus('📡 Requesting location — tap <strong>Allow</strong> if your browser asks.', '#374151', 'rgba(255,255,255,.85)');

  function restoreBtn() {{
    btn.disabled         = false;
    btn.style.background = '#16a34a';
    btn.style.boxShadow  = '0 4px 14px rgba(22,163,74,.35)';
    btn.style.cursor     = 'pointer';
    btnText.textContent  = 'Send Location via WhatsApp';
  }}

  function tryGet(highAccuracy) {{
    navigator.geolocation.getCurrentPosition(
      (pos) => {{
        restoreBtn();
        showContactButtons(
          pos.coords.latitude.toFixed(6),
          pos.coords.longitude.toFixed(6)
        );
      }},
      (err) => {{
        if (err.code === 3 && highAccuracy) {{
          setStatus('⏳ GPS timed out — trying network location…', '#6b7280', 'rgba(255,255,255,.85)');
          tryGet(false);
          return;
        }}
        restoreBtn();
        const msgs = {{
          1: '❌ <strong>Permission denied.</strong> Go to browser Settings → Site Permissions → Location, allow this site, then retry.',
          2: '❌ <strong>Location unavailable.</strong> Make sure GPS / Location Services are turned ON in your phone settings.',
          3: '❌ <strong>Timed out.</strong> Move to an open area with a better signal and try again.',
        }};
        setStatus(msgs[err.code] || '❌ Location error: ' + err.message, '#dc2626', 'rgba(254,242,242,.9)');
      }},
      {{ enableHighAccuracy: highAccuracy, timeout: highAccuracy ? 20000 : 10000, maximumAge: 0 }}
    );
  }}

  tryGet(true);
}}

function sendLocationToOwner() {{
  const btn     = document.getElementById('owner-loc-btn');
  const btnText = document.getElementById('owner-loc-btn-text');
  if (!btn || (!OWNER_WA_NUM && !OWNER_SMS_NUM)) return;

  if (!navigator.geolocation) {{
    setStatus('❌ GPS is not supported on this browser or device.', '#dc2626', 'rgba(254,242,242,.9)');
    return;
  }}
  if (location.protocol !== 'https:' && location.hostname !== 'localhost') {{
    setStatus('❌ Location requires a secure HTTPS connection.', '#dc2626', 'rgba(254,242,242,.9)');
    return;
  }}

  btn.disabled = true;
  btn.style.opacity = '0.6';
  btnText.textContent = 'Getting GPS location…';
  setStatus('📡 Requesting location — tap <strong>Allow</strong> if your browser asks.', '#374151', 'rgba(255,255,255,.85)');

  function restoreOwnerBtn() {{
    btn.disabled = false;
    btn.style.opacity = '1';
    btnText.textContent = '📲 Send Location to Vehicle Owner';
  }}

  function tryGetOwner(highAccuracy) {{
    navigator.geolocation.getCurrentPosition(
      (pos) => {{
        restoreOwnerBtn();
        const lat = pos.coords.latitude.toFixed(6);
        const lng = pos.coords.longitude.toFixed(6);
        const waEncoded = encodeURIComponent(buildMessage(lat, lng));

        const container = document.getElementById('contact-send-list');
        container.innerHTML = '';

        const row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;padding:11px 14px;gap:10px;';

        const nameDiv = document.createElement('div');
        nameDiv.style.cssText = 'flex:1;min-width:0;';
        nameDiv.innerHTML =
          '<div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;margin-bottom:2px">Vehicle Owner</div>' +
          '<div style="font-size:14px;font-weight:600;color:#111;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + OWNER + '</div>';

        const btnGroup = document.createElement('div');
        btnGroup.style.cssText = 'display:flex;gap:6px;flex-shrink:0;';

        if (OWNER_WA_NUM) {{
          const waBtn = document.createElement('a');
          waBtn.href = 'https://wa.me/' + OWNER_WA_NUM.replace(/\\D/g,'') + '?text=' + waEncoded;
          waBtn.target = '_blank';
          waBtn.rel = 'noopener noreferrer';
          waBtn.style.cssText =
            'padding:8px 13px;background:#16a34a;color:#fff;border-radius:10px;' +
            'font-size:12px;font-weight:700;text-decoration:none;' +
            'display:inline-flex;align-items:center;gap:5px;' +
            'box-shadow:0 2px 8px rgba(22,163,74,.28);';
          waBtn.innerHTML = '💬 WhatsApp';
          btnGroup.appendChild(waBtn);
        }}

        if (OWNER_SMS_NUM) {{
          const smsBtn = document.createElement('a');
          smsBtn.href = 'sms:+' + OWNER_SMS_NUM + '?body=' + waEncoded;
          smsBtn.style.cssText =
            'padding:8px 10px;background:#f3f4f6;color:#374151;border-radius:10px;' +
            'font-size:12px;font-weight:600;text-decoration:none;' +
            'display:inline-flex;align-items:center;gap:4px;border:1.5px solid #e5e7eb;';
          smsBtn.title = 'SMS fallback if WhatsApp unavailable';
          smsBtn.innerHTML = '📱 SMS';
          btnGroup.appendChild(smsBtn);
        }}

        row.appendChild(nameDiv);
        row.appendChild(btnGroup);
        container.appendChild(row);
        container.style.display = 'block';

        setStatus(
          OWNER_WA_NUM
            ? '✅ <strong>Location ready.</strong> Tap <strong>WhatsApp</strong> to send — or SMS as fallback.'
            : '✅ <strong>Location ready.</strong> Owner is not on WhatsApp — tap <strong>SMS</strong> to send.',
          '#166534', 'rgba(220,252,231,.8)'
        );
      }},
      (err) => {{
        if (err.code === 3 && highAccuracy) {{
          setStatus('⏳ GPS timed out — trying network location…', '#6b7280', 'rgba(255,255,255,.85)');
          tryGetOwner(false);
          return;
        }}
        restoreOwnerBtn();
        const msgs = {{
          1: '❌ <strong>Permission denied.</strong> Go to browser Settings → Site Permissions → Location, allow this site, then retry.',
          2: '❌ <strong>Location unavailable.</strong> Make sure GPS / Location Services are turned ON in your phone settings.',
          3: '❌ <strong>Timed out.</strong> Move to an open area with a better signal and try again.',
        }};
        setStatus(msgs[err.code] || '❌ Location error: ' + err.message, '#dc2626', 'rgba(254,242,242,.9)');
      }},
      {{ enableHighAccuracy: highAccuracy, timeout: highAccuracy ? 20000 : 10000, maximumAge: 0 }}
    );
  }}

  tryGetOwner(true);
}}
</script>
</div>
</body></html>"""


def page_payment_instructions(qr_id: str, name: str, plan: str = "basic") -> str:
    """
    Shown after setup — tells owner how to pay manually via JazzCash/Easypaisa
    and WhatsApp you to confirm, so you can activate their subscription.
    """
    plan_label = "👑 Premium" if plan == "premium" else "🔵 Basic"
    plan_color = "#7c3aed" if plan == "premium" else "#166534"
    plan_bg    = "#faf5ff" if plan == "premium" else "#f0fdf4"
    plan_border= "#ddd6fe" if plan == "premium" else "#86efac"

    whatsapp_msg = (
        f"Assalam o Alaikum! Mera naam {name} hai. "
        f"Main apna Pasbaan sticker {qr_id} ({plan_label.replace(chr(128081)+' ','').replace(chr(128309)+' ','')}) activate karna chahta hoon. "
        f"Payment screenshot attach kar raha hoon."
    )
    import urllib.parse
    wa_url = f"https://wa.me/{OWNER_WHATSAPP}?text={urllib.parse.quote(whatsapp_msg)}"

    return f"""<!DOCTYPE html><html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Complete Payment — Pasbaan</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f0f0eb;min-height:100vh;padding:24px 16px 48px;color:#111}}
.wrap{{max-width:420px;margin:0 auto}}
.logo{{text-align:center;padding:8px 0 22px}}
.logo-name{{font-size:22px;font-weight:700;letter-spacing:-.4px}}
.logo-dot{{color:#b90a0a}}
.logo-sub{{font-size:11px;color:#aaa;margin-top:3px;letter-spacing:.05em;text-transform:uppercase}}
.card{{background:#fff;border-radius:18px;padding:22px 20px;margin-bottom:14px;
       box-shadow:0 1px 4px rgba(0,0,0,.06)}}
.step{{display:flex;gap:14px;align-items:flex-start;padding:12px 0;
       border-bottom:1px solid #f5f5f3}}
.step:last-child{{border-bottom:none}}
.step-num{{width:28px;height:28px;border-radius:50%;background:#111;color:#fff;
           font-size:13px;font-weight:700;display:flex;align-items:center;
           justify-content:center;flex-shrink:0;margin-top:1px}}
.step-body{{flex:1}}
.step-title{{font-size:14px;font-weight:700;color:#111;margin-bottom:4px}}
.step-desc{{font-size:13px;color:#666;line-height:1.55}}
.pay-method{{border-radius:12px;padding:14px 16px;margin:10px 0;
             border:1.5px solid #e5e5e0;background:#fafaf7}}
.pay-label{{font-size:11px;font-weight:700;text-transform:uppercase;
            letter-spacing:.07em;color:#aaa;margin-bottom:6px}}
.pay-number{{font-size:22px;font-weight:800;color:#111;letter-spacing:.02em}}
.pay-name{{font-size:12px;color:#888;margin-top:3px}}
.divider-or{{text-align:center;font-size:12px;color:#ccc;
             margin:8px 0;position:relative}}
.divider-or::before,.divider-or::after{{content:'';position:absolute;
  top:50%;width:42%;height:1px;background:#eee}}
.divider-or::before{{left:0}}.divider-or::after{{right:0}}
.plan-chip{{display:inline-block;padding:4px 14px;border-radius:20px;
            font-size:12px;font-weight:700;
            background:{plan_bg};color:{plan_color};
            border:1.5px solid {plan_border};margin-bottom:12px}}
.wa-btn{{display:block;width:100%;padding:15px;border:none;border-radius:13px;
         background:#25d366;color:#fff;font-size:15px;font-weight:700;
         text-align:center;text-decoration:none;cursor:pointer;
         box-shadow:0 4px 14px rgba(37,211,102,.3);font-family:inherit}}
.wa-btn:active{{opacity:.9}}
.amount-box{{background:#f5f5f0;border-radius:12px;padding:14px 16px;
             display:flex;justify-content:space-between;align-items:center;margin:12px 0}}
.amount-label{{font-size:13px;color:#666}}
.amount-val{{font-size:24px;font-weight:800;color:#111}}
.note{{font-size:12px;color:#aaa;text-align:center;margin-top:6px;line-height:1.6}}
.id-chip{{display:inline-block;background:#f0f0eb;border-radius:8px;
          padding:3px 10px;font-family:monospace;font-size:13px;
          color:#555;font-weight:600}}
</style>
</head>
<body><div class="wrap">

  <div class="logo">
    <div class="logo-name">Pas<span class="logo-dot">baan</span></div>
    <div class="logo-sub">Vehicle Emergency System</div>
  </div>

  <!-- Success header -->
  <div class="card" style="text-align:center;padding:26px 20px 22px">
    <div style="font-size:52px;margin-bottom:12px">✅</div>
    <div class="plan-chip">{plan_label}</div>
    <h2 style="font-size:20px;font-weight:800;margin-bottom:6px">
      Sticker Activated, {name}!
    </h2>
    <p style="font-size:13px;color:#888;line-height:1.6">
      Your contact page is live. Complete payment below<br>
      to keep your subscription active after the free period.
    </p>
    <div style="margin-top:12px">
      <span style="font-size:11px;color:#bbb">Sticker ID &nbsp;</span>
      <span class="id-chip">{qr_id}</span>
    </div>
  </div>

  <!-- Payment steps -->
  <div class="card">
    <p style="font-size:13px;font-weight:700;color:#111;margin-bottom:14px">
      Complete your subscription in 2 steps:
    </p>

    <!-- Step 1: Pay -->
    <div class="step">
      <div class="step-num">1</div>
      <div class="step-body">
        <div class="step-title">Send Rs. {SUBSCRIPTION_PRICE} via JazzCash or Easypaisa</div>
        <div class="step-desc">Send the payment to either number below and <strong>take a screenshot</strong> of the confirmation.</div>

        <div class="amount-box">
          <span class="amount-label">Amount to send</span>
          <span class="amount-val">Rs. {SUBSCRIPTION_PRICE}</span>
        </div>

        <div class="pay-method">
          <div class="pay-label">💚 JazzCash</div>
          <div class="pay-number">{OWNER_JAZZCASH}</div>
          <div class="pay-name">{OWNER_NAME}</div>
        </div>
        <div class="divider-or">OR</div>
        <div class="pay-method">
          <div class="pay-label">🟣 Easypaisa</div>
          <div class="pay-number">{OWNER_EASYPAISA}</div>
          <div class="pay-name">{OWNER_NAME}</div>
        </div>
      </div>
    </div>

    <!-- Step 2: WhatsApp -->
    <div class="step">
      <div class="step-num">2</div>
      <div class="step-body">
        <div class="step-title">WhatsApp us the screenshot</div>
        <div class="step-desc" style="margin-bottom:14px">
          Send the payment screenshot to our WhatsApp. We activate your subscription within <strong>a few hours</strong> (usually faster).
        </div>
        <a href="{wa_url}" target="_blank" class="wa-btn">
          📲 &nbsp;WhatsApp Screenshot Now
        </a>
      </div>
    </div>
  </div>

  <!-- Free period note -->
  <div class="card" style="background:#fefce8;border:1.5px solid #fde68a;padding:16px 18px">
    <p style="font-size:13px;font-weight:700;color:#854d0e;margin-bottom:5px">
      ⏳ Your sticker works now — payment just keeps it alive
    </p>
    <p style="font-size:12px;color:#92400e;line-height:1.65">
      Your contact page is already live and anyone can scan it.
      You have a free period while we verify your payment.
      We'll WhatsApp you once your subscription is confirmed active.
    </p>
  </div>

  <!-- Preview link -->
  <a href="/scan/{qr_id}"
     style="display:block;text-align:center;padding:14px;background:#fff;
            border-radius:13px;font-size:14px;font-weight:600;color:#111;
            text-decoration:none;box-shadow:0 1px 4px rgba(0,0,0,.06);margin-top:2px">
    Preview my contact page →
  </a>

  <p class="note" style="margin-top:16px">
    Questions? WhatsApp us anytime at {OWNER_WHATSAPP}<br>
    Pasbaan Pakistan · Secure Vehicle Emergency System
  </p>

</div></body></html>"""


def page_success(qr_id: str, name: str, plan: str = "basic") -> str:
    plan_badge = (
        '<div style="display:inline-block;margin-bottom:14px;padding:5px 16px;'
        'background:linear-gradient(135deg,#7c3aed,#6d28d9);color:#fff;'
        'border-radius:20px;font-size:12px;font-weight:700;letter-spacing:.05em;">👑 PREMIUM PLAN</div>'
        if plan == "premium" else
        '<div style="display:inline-block;margin-bottom:14px;padding:5px 16px;'
        'background:#f0fdf4;color:#166534;border:1.5px solid #86efac;'
        'border-radius:20px;font-size:12px;font-weight:700;">🔵 BASIC PLAN</div>'
    )
    plan_note = (
        "<p style='font-size:12px;color:#7c3aed;margin-top:8px;line-height:1.6'>"
        "🔒 Premium: Your numbers are private. Call packs coming soon.</p>"
        if plan == "premium" else
        "<p style='font-size:12px;color:#166534;margin-top:8px;line-height:1.6'>"
        "📞 Basic: Your contact numbers are visible to anyone who scans your sticker.</p>"
    )
    return f"""<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8">{_css()}<title>Activated!</title></head>
<body><div class="wrap">{_logo()}
<div class="card" style="text-align:center;padding:40px 20px">
  <div style="font-size:60px;margin-bottom:16px">✅</div>
  {plan_badge}
  <h2 style="font-size:22px;font-weight:700;margin-bottom:10px">Pasbaan Activated!</h2>
  <p style="color:#666;font-size:15px;line-height:1.65">
    {name}, your emergency contacts are saved.<br>
    Anyone who scans your sticker will see your contact page instantly.
  </p>
  {plan_note}
  <div style="margin-top:22px;background:#f5f5f0;padding:14px;border-radius:12px">
    <p style="font-size:12px;color:#aaa;margin-bottom:4px">Your Sticker ID</p>
    <code style="font-size:17px;font-weight:700">{qr_id}</code>
  </div>
</div>
<a href="/scan/{qr_id}" class="btn btn-ghost" style="display:block;text-align:center;text-decoration:none">
  Preview my contact page →</a>
</div></body></html>"""


def page_pin_prompt(qr_id: str) -> str:
    """Full-page PIN entry — shown when owner tries to access /update without a valid cookie."""
    return f"""<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8">{_css()}<title>Owner Access — {qr_id}</title></head>
<body><div class="wrap">{_logo()}
<div class="card" style="text-align:center;padding:32px 20px;background:#fafaf7;border:1.5px solid #e5e5e0">
  <div style="font-size:48px;margin-bottom:14px">🔒</div>
  <h2 style="font-size:20px;font-weight:700;margin-bottom:8px">Owner Access</h2>
  <p style="font-size:14px;color:#666;line-height:1.65;margin-bottom:20px">
    Enter your 4-digit PIN to edit your Pasbaan details.
  </p>
  <form method="POST" action="/scan/{qr_id}/verify-pin">
    <input type="hidden" name="next_url" value="/scan/{qr_id}/update">
    <div style="display:flex;justify-content:center;gap:10px;margin-bottom:20px">
      <input type="text" id="pin-display"
             style="width:160px;text-align:center;font-size:28px;font-weight:700;
                    letter-spacing:12px;padding:12px 16px;border:2px solid #111;
                    border-radius:14px;background:#fff;color:#111"
             maxlength="4" inputmode="numeric" pattern="\\d{{4}}" autocomplete="off"
             name="pin" required placeholder="••••" autofocus>
    </div>
    <!-- Large numpad for mobile -->
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;
                max-width:240px;margin:0 auto 20px">
      {chr(10).join('<button type="button" onclick="padPress(' + chr(39) + d + chr(39) + ')" style="padding:16px;font-size:20px;font-weight:600;background:#f5f5f0;border:1.5px solid #e0e0e0;border-radius:12px;cursor:pointer">' + d + '</button>' for d in ['1','2','3','4','5','6','7','8','9','','0','⌫'])}
    </div>
    <button type="submit" class="btn btn-dark" style="max-width:240px;margin:0 auto">
      Unlock →
    </button>
  </form>
</div>
<div class="card" style="background:#fef2f2;border:1.5px solid #fecaca;margin-top:4px">
  <div class="sec-title" style="color:#dc2626">⚠️ Forgotten PIN</div>
  <p style="font-size:13px;color:#b91c1c;line-height:1.7">
    If you have forgotten your PIN, there is <strong>no way to reset or recover it</strong>.
    Your Pasbaan details can no longer be edited.
  </p>
</div>
<a href="/scan/{qr_id}" class="btn btn-ghost" style="display:block;text-align:center;text-decoration:none;margin-top:4px">
  ← Back to contact page
</a>
</div>
<script>
const inp = document.getElementById('pin-display');
function padPress(v) {{
  if (v === '⌫') {{ inp.value = inp.value.slice(0, -1); return; }}
  if (inp.value.length < 4) inp.value += v;
}}
inp.addEventListener('input', () => {{
  inp.value = inp.value.replace(/\\D/g,'').slice(0,4);
}});
</script>
</body></html>"""


def page_update(qr_id: str, data: dict) -> str:
    """Pre-filled edit form for owner to update their Pasbaan details."""
    def sel(name, val):
        opts = ["Brother","Sister","Father","Mother","Wife","Husband","Son","Daughter","Friend","Other"]
        return "".join(
            f'<option value="{o}" {"selected" if o == val else ""}>{o}</option>'
            for o in opts
        )

    c  = data.get("contacts", [])
    c1 = c[0] if len(c) > 0 else {}
    c2 = c[1] if len(c) > 1 else {}
    c3 = c[2] if len(c) > 2 else {}

    def wa_check(contact, iid):
        checked = "checked" if contact.get("whatsapp") else ""
        return f"""<div style="display:flex;align-items:center;gap:8px;margin-top:10px;padding:10px 12px;
                background:#f0fdf4;border-radius:10px;border:1px solid #86efac;">
      <input type="checkbox" name="contact{iid}_whatsapp" value="yes" id="wa{iid}"
             style="width:18px;height:18px;cursor:pointer;accent-color:#16a34a;flex-shrink:0;" {checked}>
      <label for="wa{iid}" style="font-size:13px;color:#166534;cursor:pointer;margin:0;font-weight:600;">
        💬 This number is on WhatsApp (for location sharing)
      </label>
    </div>"""

    return f"""<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8">{_css()}<title>Update Pasbaan — {qr_id}</title></head>
<body><div class="wrap">{_logo()}
<div class="card" style="background:#eff6ff;border:1.5px solid #93c5fd;">
  <span class="badge" style="background:#dbeafe;color:#1e40af">✏️ Update Your Pasbaan Details</span>
  <p style="font-size:14px;color:#1e40af;line-height:1.65;margin-top:8px">
    You can update your contacts, phone numbers, or message below. Changes take effect immediately.
  </p>
  <p style="font-size:11px;color:#93c5fd;margin-top:6px">Sticker ID: <code>{qr_id}</code></p>
</div>
<form action="/scan/{qr_id}/update" method="POST">
  <div class="card" style="background:linear-gradient(135deg,#f8faff 0%,#eef2ff 100%);border:1.5px solid #c7d2fe;">
    <div class="sec-title" style="color:#3730a3">🚗 Your Vehicle</div>
    <label>Your full name <span style="color:#94a3b8;font-size:11px;">(optional)</span></label>
    <input name="owner_name" type="text" value="{data.get('owner_name','')}">
    <div class="row2">
      <div><label>Vehicle number <span style="color:#94a3b8;font-size:11px;">(optional)</span></label>
           <input name="vehicle_number" type="text" value="{data.get('vehicle_number','')}"></div>
      <div><label>City <span style="color:#94a3b8;font-size:11px;">(optional)</span></label>
           <input name="city" type="text" value="{data.get('city','')}"></div>
    </div>
    <label style="margin-top:10px;">Your own phone number <span style="color:#94a3b8;font-size:11px;">(optional)</span></label>
    <input name="owner_phone" type="tel" value="{data.get('owner_phone','')}" placeholder="+92 300 1234567">
    <div style="display:flex;align-items:center;gap:8px;margin-top:10px;padding:10px 12px;
                background:#f0fdf4;border-radius:10px;border:1px solid #86efac;">
      <input type="checkbox" name="owner_whatsapp" value="yes" id="wa_owner"
             style="width:18px;height:18px;cursor:pointer;accent-color:#16a34a;flex-shrink:0;"
             {"checked" if data.get("owner_whatsapp") else ""}>
      <label for="wa_owner" style="font-size:13px;color:#166534;cursor:pointer;margin:0;font-weight:600;">
        💬 My number is on WhatsApp (for location sharing)
      </label>
    </div>
  </div>
  <div class="card" style="border:1.5px solid #ddd6fe;">
    <div class="sec-title" style="color:#5b21b6">📋 Emergency Contact 1 <span style="font-size:11px;color:#94a3b8;font-weight:400;">(optional)</span></div>
    <label>Relation</label>
    <select name="contact1_relation">{sel('contact1_relation', c1.get('relation','Brother'))}</select>
    <label>Full name</label>
    <input name="contact1_name" type="text" value="{c1.get('name','')}">
    <label>Phone number</label>
    <input name="contact1_phone" type="tel" value="{c1.get('phone','')}">
    {wa_check(c1, 1)}
  </div>
  <div class="card" style="border:1.5px solid #e0e7ff;">
    <div class="sec-title" style="color:#4338ca">📋 Emergency Contact 2 <span style="font-size:11px;color:#94a3b8;font-weight:400;">(optional)</span></div>
    <label>Relation</label>
    <select name="contact2_relation">
      <option value="">— Skip —</option>
      {sel('contact2_relation', c2.get('relation',''))}
    </select>
    <label>Full name</label>
    <input name="contact2_name" type="text" value="{c2.get('name','')}">
    <label>Phone number</label>
    <input name="contact2_phone" type="tel" value="{c2.get('phone','')}">
    {wa_check(c2, 2)}
  </div>
  <div class="card" style="border:1.5px solid #e0e7ff;">
    <div class="sec-title" style="color:#4338ca">📋 Emergency Contact 3 <span style="font-size:11px;color:#94a3b8;font-weight:400;">(optional)</span></div>
    <label>Relation</label>
    <select name="contact3_relation">
      <option value="">— Skip —</option>
      {sel('contact3_relation', c3.get('relation',''))}
    </select>
    <label>Full name</label>
    <input name="contact3_name" type="text" value="{c3.get('name','')}">
    <label>Phone number</label>
    <input name="contact3_phone" type="tel" value="{c3.get('phone','')}">
    {wa_check(c3, 3)}
  </div>
  <div class="card" style="border:1.5px solid #e2e8f0;">
    <div class="sec-title" style="color:#475569">💬 Message for finder <span style="font-size:11px;color:#94a3b8;font-weight:400;">(optional)</span></div>
    <textarea name="message" placeholder="e.g. Please contact my family in case of emergency.">{data.get('message','')}</textarea>
  </div>
  <div class="card" style="background:#fafaf7;border:1.5px solid #e0e0e0;">
    <div class="sec-title">🔒 Change PIN — Optional</div>
    <p style="font-size:13px;color:#666;line-height:1.6;margin-bottom:12px">
      Leave both fields blank to keep your current PIN. Fill them in only if you want to set a new one.
    </p>
    <div class="row2">
      <div>
        <label>New PIN (4 digits)</label>
        <input name="new_pin" type="password" inputmode="numeric" maxlength="4"
               pattern="\\d{{4}}" placeholder="••••" autocomplete="new-password"
               style="letter-spacing:8px;font-size:22px;text-align:center">
      </div>
      <div>
        <label>Confirm New PIN</label>
        <input name="new_pin_confirm" type="password" inputmode="numeric" maxlength="4"
               pattern="\\d{{4}}" placeholder="••••" autocomplete="new-password"
               style="letter-spacing:8px;font-size:22px;text-align:center">
      </div>
    </div>
  </div>
  <button type="submit" class="btn btn-dark">💾 Save Changes</button>
  <a href="/scan/{qr_id}" class="btn btn-ghost" style="display:block;text-align:center;text-decoration:none;margin-top:8px">
    ← Cancel, go back
  </a>
</form></div></body></html>"""


def page_update_success(qr_id: str, name: str) -> str:
    return f"""<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8">{_css()}<title>Updated!</title></head>
<body><div class="wrap">{_logo()}
<div class="card" style="text-align:center;padding:40px 20px">
  <div style="font-size:60px;margin-bottom:16px">✅</div>
  <h2 style="font-size:22px;font-weight:700;margin-bottom:10px">Details Updated!</h2>
  <p style="color:#666;font-size:15px;line-height:1.65">
    {name}, your Pasbaan has been updated successfully.<br>
    Anyone who scans your sticker will see the new information instantly.
  </p>
  <div style="margin-top:22px;background:#f5f5f0;padding:14px;border-radius:12px">
    <p style="font-size:12px;color:#aaa;margin-bottom:4px">Your Sticker ID</p>
    <code style="font-size:17px;font-weight:700">{qr_id}</code>
  </div>
</div>
<a href="/scan/{qr_id}" class="btn btn-ghost" style="display:block;text-align:center;text-decoration:none">
  Preview my contact page →</a>
</div></body></html>"""


def page_deactivated(qr_id: str) -> str:
    """Shown when a QR code has been temporarily deactivated by its owner."""
    return f"""<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{_css()}<title>QR Inactive — {qr_id}</title></head>
<body><div class="wrap">{_logo()}
<div class="card" style="text-align:center;padding:40px 20px;border:1.5px solid #fecaca;background:#fff5f5">
  <div style="font-size:64px;margin-bottom:16px">🔴</div>
  <h2 style="font-size:22px;font-weight:700;color:#9f1239;margin-bottom:10px">QR Code Temporarily Inactive</h2>
  <p style="color:#7f1d1d;font-size:14px;line-height:1.7;margin-bottom:6px">
    The vehicle owner has temporarily deactivated this Pasbaan.<br>
    If this is an emergency, try calling emergency services.
  </p>
  <p style="font-size:12px;color:#b91c1c;margin-top:4px">
    Sticker ID: <code style="font-weight:700">{qr_id}</code>
  </p>
</div>

<!-- Reactivate section for owner -->
<div class="card" style="margin-top:12px">
  <p style="font-size:13px;color:#555;text-align:center;margin-bottom:14px">
    Are you the owner? Reactivate your QR code below.
  </p>
  <button onclick="document.getElementById('reactivate-modal').style.display='flex'"
    style="width:100%;padding:13px;background:#16a34a;color:#fff;border:none;
           border-radius:13px;font-size:15px;font-weight:700;cursor:pointer;
           font-family:inherit;box-shadow:0 4px 14px rgba(22,163,74,.3)">
    🟢 Reactivate My QR Code
  </button>
</div>

<!-- Reactivate PIN modal -->
<div id="reactivate-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);
     z-index:999;align-items:flex-end;justify-content:center;">
  <div style="background:#fff;border-radius:22px 22px 0 0;padding:28px 24px 36px;
              width:100%;max-width:460px;animation:slideUp .22s ease">
    <div style="text-align:center;margin-bottom:18px">
      <div style="font-size:36px;margin-bottom:8px">🔒</div>
      <h3 style="font-size:17px;font-weight:700;margin-bottom:4px">Owner Verification</h3>
      <p style="font-size:13px;color:#888">Enter your 4-digit PIN to reactivate</p>
    </div>
    <form method="POST" action="/scan/{qr_id}/activate" id="react-form">
      <div style="display:flex;justify-content:center;gap:8px;margin-bottom:18px">
        <input type="text" name="pin" id="react-pin"
               style="width:160px;text-align:center;font-size:28px;font-weight:700;
                      letter-spacing:12px;padding:12px 14px;border:2px solid #111;
                      border-radius:14px;background:#fafafa;color:#111"
               maxlength="4" inputmode="numeric" pattern="\\d{{4}}"
               autocomplete="off" placeholder="••••" required autofocus>
      </div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;
                  max-width:220px;margin:0 auto 18px">
        {chr(10).join('<button type="button" onclick="rp(' + chr(39) + d + chr(39) + ')" style="padding:14px;font-size:18px;font-weight:600;background:#f5f5f0;border:1.5px solid #e8e8e8;border-radius:11px;cursor:pointer">' + d + '</button>' for d in ['1','2','3','4','5','6','7','8','9','','0','⌫'])}
      </div>
      <button type="submit" class="btn btn-dark" style="background:#16a34a">🟢 Reactivate</button>
    </form>
    <button onclick="document.getElementById('reactivate-modal').style.display='none'"
      style="width:100%;margin-top:10px;padding:11px;background:transparent;
             border:1.5px solid #e8e8e8;border-radius:13px;font-size:13px;
             color:#aaa;cursor:pointer;font-family:inherit">Cancel</button>
  </div>
</div>
<style>@keyframes slideUp{{from{{transform:translateY(100%)}}to{{transform:translateY(0)}}}}</style>
<script>
const rpin = document.getElementById('react-pin');
function rp(v){{
  if(v==='⌫'){{rpin.value=rpin.value.slice(0,-1);return;}}
  if(rpin.value.length<4) rpin.value+=v;
}}
rpin.addEventListener('input',()=>{{rpin.value=rpin.value.replace(/\\D/g,'').slice(0,4);}});
rpin.addEventListener('input',()=>{{if(rpin.value.length===4) document.getElementById('react-form').requestSubmit();}});
</script>
</div></body></html>"""


def page_admin_subscriptions(rows: list) -> str:
    """Admin subscriptions management page."""

    STATUS_STYLE = {
        "active":          ("✅ Active",          "#d1fae5", "#065f46"),
        "pending_payment": ("⏳ Pending Payment",  "#fef9c3", "#854d0e"),
        "suspended":       ("🚫 Suspended",        "#fee2e2", "#991b1b"),
        "trial":           ("🔵 Trial",            "#dbeafe", "#1e40af"),
    }

    rows_html = ""
    for r in rows:
        owner = r.get("owner_data") or {}
        name  = owner.get("owner_name", "—") if owner else "Unclaimed"
        phone = owner.get("contacts", [{}])[0].get("phone", "—") if owner else "—"
        plan  = r.get("plan", "basic")
        status= r.get("status", "pending_payment")
        label, bg, fg = STATUS_STYLE.get(status, (status, "#f5f5f5", "#333"))
        qr_id = r["qr_id"]
        exp   = r.get("expires_at")
        exp_str = exp.strftime("%d %b %Y") if exp else "—"
        plan_badge = (
            f'<span style="background:#ede9fe;color:#6d28d9;padding:2px 9px;'
            f'border-radius:12px;font-size:11px;font-weight:700;">👑 Premium</span>'
            if plan == "premium" else
            f'<span style="background:#d1fae5;color:#065f46;padding:2px 9px;'
            f'border-radius:12px;font-size:11px;font-weight:700;">🔵 Basic</span>'
        )
        status_badge = (
            f'<span style="background:{bg};color:{fg};padding:2px 9px;'
            f'border-radius:12px;font-size:11px;font-weight:600;">{label}</span>'
        )
        activate_btn = (
            f'<form method="POST" action="/admin/subscriptions/{qr_id}/activate" style="display:inline">'
            f'<button type="submit" style="padding:5px 12px;border:none;border-radius:8px;'
            f'background:#111;color:#fff;font-size:12px;font-weight:600;cursor:pointer;margin-right:5px">'
            f'✅ Activate</button></form>'
            if status != "active" else ""
        )
        suspend_btn = (
            f'<form method="POST" action="/admin/subscriptions/{qr_id}/suspend" style="display:inline">'
            f'<button type="submit" style="padding:5px 12px;border:none;border-radius:8px;'
            f'background:#fee2e2;color:#991b1b;font-size:12px;font-weight:600;cursor:pointer">'
            f'🚫 Suspend</button></form>'
            if status == "active" else ""
        )
        rows_html += f"""
        <tr>
          <td style="font-family:monospace;font-weight:600;font-size:13px">{qr_id}</td>
          <td style="font-size:13px">{name}<br><span style="font-size:11px;color:#aaa">{phone}</span></td>
          <td>{plan_badge}</td>
          <td>{status_badge}</td>
          <td style="font-size:12px;color:#666">{exp_str}</td>
          <td>{activate_btn}{suspend_btn}
            <a href="/scan/{qr_id}" target="_blank"
               style="padding:5px 10px;border:1px solid #ddd;border-radius:8px;
                      font-size:12px;text-decoration:none;color:#555">👁</a>
          </td>
        </tr>"""

    pending_count  = sum(1 for r in rows if r.get("status") == "pending_payment")
    active_count   = sum(1 for r in rows if r.get("status") == "active")
    suspended_count= sum(1 for r in rows if r.get("status") == "suspended")

    return f"""<!DOCTYPE html><html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Subscriptions — Pasbaan Admin</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#f0f0eb;min-height:100vh;padding:24px 16px 48px;color:#111}}
.wrap{{max-width:960px;margin:0 auto}}
.topbar{{display:flex;align-items:center;justify-content:space-between;
         background:#111;color:#fff;padding:12px 20px;border-radius:14px;margin-bottom:20px}}
.topbar-title{{font-size:16px;font-weight:700}}
.topbar a{{color:#aaa;font-size:13px;text-decoration:none;padding:5px 12px;
           border:1px solid #333;border-radius:8px}}
.topbar a:hover{{color:#fff;border-color:#666}}
.stat-row{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:20px}}
.stat-card{{background:#fff;border-radius:14px;padding:16px 18px;
            box-shadow:0 1px 4px rgba(0,0,0,.06)}}
.stat-label{{font-size:11px;font-weight:700;text-transform:uppercase;
             letter-spacing:.07em;color:#aaa;margin-bottom:6px}}
.stat-val{{font-size:28px;font-weight:800;color:#111}}
.card{{background:#fff;border-radius:16px;overflow:hidden;
       box-shadow:0 1px 4px rgba(0,0,0,.06);margin-bottom:16px}}
.card-header{{padding:16px 20px;border-bottom:1px solid #f0f0ee;
              display:flex;align-items:center;justify-content:space-between}}
.card-title{{font-size:15px;font-weight:700}}
table{{width:100%;border-collapse:collapse}}
th{{padding:10px 14px;font-size:11px;font-weight:700;text-transform:uppercase;
    letter-spacing:.06em;color:#999;border-bottom:2px solid #f0f0ee;text-align:left}}
td{{padding:12px 14px;border-bottom:1px solid #f7f7f5;vertical-align:middle}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#fafaf7}}
.alert{{background:#fef9c3;border:1.5px solid #fde68a;border-radius:12px;
        padding:12px 16px;margin-bottom:16px;font-size:13px;color:#854d0e}}
</style>
</head>
<body><div class="wrap">

  <div class="topbar">
    <div class="topbar-title">💳 Subscriptions</div>
    <div style="display:flex;gap:8px">
      <a href="/admin">← Dashboard</a>
      <a href="/admin/logout">Logout</a>
    </div>
  </div>

  {"" if not pending_count else
    f'<div class="alert">⚠️ <strong>{pending_count} subscription{"s" if pending_count>1 else ""} waiting for payment confirmation.</strong> '
    f'Check your JazzCash / Easypaisa and WhatsApp, then click Activate.</div>'}

  <!-- Stats -->
  <div class="stat-row">
    <div class="stat-card">
      <div class="stat-label">⏳ Pending</div>
      <div class="stat-val" style="color:#d97706">{pending_count}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">✅ Active</div>
      <div class="stat-val" style="color:#059669">{active_count}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">🚫 Suspended</div>
      <div class="stat-val" style="color:#dc2626">{suspended_count}</div>
    </div>
  </div>

  <!-- Table -->
  <div class="card">
    <div class="card-header">
      <span class="card-title">All Subscriptions ({len(rows)} total)</span>
    </div>
    <div style="overflow-x:auto">
      <table>
        <thead>
          <tr>
            <th>Sticker ID</th>
            <th>Owner</th>
            <th>Plan</th>
            <th>Status</th>
            <th>Expires</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>{rows_html if rows_html else
          "<tr><td colspan='6' style='text-align:center;color:#aaa;padding:30px'>No subscriptions yet</td></tr>"}
        </tbody>
      </table>
    </div>
  </div>

  <p style="font-size:12px;color:#bbb;text-align:center;margin-top:8px">
    Pasbaan Pakistan Admin · Subscription Management
  </p>
</div></body></html>"""


def page_not_found(qr_id: str) -> str:
    return f"""<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8">{_css()}<title>Not Found</title></head>
<body><div class="wrap">{_logo()}
<div class="card" style="text-align:center;padding:40px 20px">
  <div style="font-size:48px;margin-bottom:16px">❓</div>
  <h2 style="font-size:20px;font-weight:700;margin-bottom:10px">QR Code Not Recognised</h2>
  <p style="color:#777;font-size:14px;line-height:1.65">
    The code <code>{qr_id}</code> is not in our system.<br>
    This sticker may not be a genuine Pasbaan product.
  </p>
</div></div></body></html>"""