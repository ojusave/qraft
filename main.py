import os
import json
import base64
import io
import string
import random
import logging
import traceback
import threading
import time
from collections import defaultdict

import psycopg2
import psycopg2.extras
import psycopg2.pool
import qrcode
import qrcode.constants
import requests as http_requests
from PIL import Image
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/qraft")
PORT = int(os.environ.get("PORT", 8000))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("qraft")

app = FastAPI(title="QRaft")

# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------
db_pool = None


def get_db():
    return db_pool.getconn()


def put_db(conn):
    db_pool.putconn(conn)


# ---------------------------------------------------------------------------
# In-memory scan counter (flushed to Postgres periodically)
# ---------------------------------------------------------------------------
scan_lock = threading.Lock()
scan_counts = defaultdict(int)       # campaign_id -> pending scan count
scan_events = []                     # list of (campaign_id, user_agent) tuples
campaign_cache = {}                  # short_id -> {"id": ..., "url": ...}
campaign_cache_lock = threading.Lock()


def flush_scans():
    """Flush accumulated scan counts and events to Postgres every 5 seconds."""
    while True:
        time.sleep(5)
        with scan_lock:
            if not scan_counts and not scan_events:
                continue
            batch_counts = dict(scan_counts)
            batch_events = list(scan_events)
            scan_counts.clear()
            scan_events.clear()

        try:
            conn = get_db()
            cur = conn.cursor()
            for campaign_id, count in batch_counts.items():
                cur.execute(
                    "UPDATE campaigns SET total_scans = total_scans + %s WHERE id = %s",
                    (count, campaign_id),
                )
            if batch_events:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO scan_events (campaign_id, user_agent) VALUES %s",
                    batch_events,
                )
            conn.commit()
            cur.close()
            put_db(conn)
        except Exception:
            logger.error(f"Error flushing scans: {traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Database migrations
# ---------------------------------------------------------------------------

def run_migrations():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS campaigns (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            url           TEXT NOT NULL,
            logo_url      TEXT,
            logo_base64   TEXT,
            tagline       TEXT NOT NULL,
            qr_base64     TEXT NOT NULL,
            short_id      TEXT UNIQUE NOT NULL,
            created_at    TIMESTAMPTZ DEFAULT now(),
            total_scans   INTEGER DEFAULT 0
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS scan_events (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            campaign_id   UUID REFERENCES campaigns(id),
            scanned_at    TIMESTAMPTZ DEFAULT now(),
            user_agent    TEXT
        );
    """)
    conn.commit()
    cur.close()
    put_db(conn)
    logger.info("Migrations complete")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
def startup():
    global db_pool
    db_pool = psycopg2.pool.ThreadedConnectionPool(2, 20, DATABASE_URL)
    run_migrations()
    # Start background flush thread
    t = threading.Thread(target=flush_scans, daemon=True)
    t.start()
    logger.info("Scan flush thread started")


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

SUPPORTED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}


def generate_short_id(length=8):
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choices(alphabet, k=length))


def _request_origin(request: Request) -> tuple[str, str]:
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    return scheme, host


def campaign_public_url(request: Request, short_id: str) -> str:
    """Full URL that QR codes encode; scans hit /r/ then redirect to the destination."""
    scheme, host = _request_origin(request)
    return f"{scheme}://{host}/r/{short_id}"


def campaign_page_url(request: Request, short_id: str) -> str:
    """In-app URL that opens the full-screen QR view (/c/) in the SPA."""
    scheme, host = _request_origin(request)
    return f"{scheme}://{host}/c/{short_id}"


DEFAULT_LOGO_PATH = os.path.join(BASE_DIR, "default-logo.png")


def get_default_logo():
    """Load the default Render logomark."""
    if os.path.exists(DEFAULT_LOGO_PATH):
        return Image.open(DEFAULT_LOGO_PATH)
    return None


def generate_qr(data: str, logo_image=None) -> str:
    """Generate a QR PNG as a base64 string, optionally with a centered logo."""
    if logo_image is None:
        logo_image = get_default_logo()
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    img = img.resize((400, 400), Image.LANCZOS)

    if logo_image:
        logo = logo_image.convert("RGBA")
        logo_size = int(400 * 0.3)
        logo = logo.resize((logo_size, logo_size), Image.LANCZOS)
        offset = ((400 - logo_size) // 2, (400 - logo_size) // 2)
        img.paste(logo, offset, logo)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"), media_type="text/html")


@app.get("/c/{short_id}")
def campaign_spa(short_id: str):
    """Same SPA as `/`; client reads `/c/{short_id}` and opens that campaign."""
    return FileResponse(os.path.join(BASE_DIR, "index.html"), media_type="text/html")


# ---- Health ---------------------------------------------------------------

@app.get("/api/health")
def health():
    result = {"status": "ok", "postgres": "ok"}
    try:
        conn = get_db()
        conn.cursor().execute("SELECT 1")
        put_db(conn)
    except Exception:
        result["postgres"] = "error"
        result["status"] = "error"
    return result


# ---- Create campaign ------------------------------------------------------

@app.post("/api/campaigns")
async def create_campaign(request: Request):
    try:
        form = await request.form()
        url = form.get("url")
        tagline = form.get("tagline")

        if not url or not tagline:
            return JSONResponse({"error": "url and tagline are required"}, status_code=400)

        logo_url = form.get("logo_url")
        logo_file = form.get("logo_file")

        logo_image = None
        logo_base64_str = None
        logo_url_str = None

        # Check file upload first
        if logo_file and hasattr(logo_file, "read"):
            filename = getattr(logo_file, "filename", None) or ""
            content_type = getattr(logo_file, "content_type", None) or ""
            if filename.strip() and content_type in SUPPORTED_IMAGE_TYPES:
                data = await logo_file.read()
                if len(data) > 10:
                    logo_image = Image.open(io.BytesIO(data))
                    logo_base64_str = base64.b64encode(data).decode()

        # Fallback to URL
        if not logo_image and logo_url and str(logo_url).strip():
            logo_url = str(logo_url).strip()
            resp = http_requests.get(logo_url, timeout=10)
            resp.raise_for_status()
            if len(resp.content) > 0:
                logo_image = Image.open(io.BytesIO(resp.content))
                logo_base64_str = base64.b64encode(resp.content).decode()
                logo_url_str = logo_url

        short_id = generate_short_id()
        campaign_url = campaign_public_url(request, short_id)
        qr_display_url = campaign_page_url(request, short_id)
        qr_b64 = generate_qr(campaign_url, logo_image)

        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            INSERT INTO campaigns (url, logo_url, logo_base64, tagline, qr_base64, short_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, short_id, qr_base64, url, tagline, created_at
            """,
            (str(url), logo_url_str, logo_base64_str, str(tagline), qr_b64, short_id),
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        put_db(conn)

        return {
            "id": str(row["id"]),
            "short_id": row["short_id"],
            # Scan / track: encoded in the QR image; hits /r/ then redirects to "url"
            "campaign_url": campaign_url,
            # Open in a browser to see the big QR + stats (same as clicking a campaign card)
            "qr_display_url": qr_display_url,
            "qr_base64": row["qr_base64"],
            "url": row["url"],
            "tagline": row["tagline"],
            "created_at": row["created_at"].isoformat(),
        }

    except Exception as e:
        logger.error(f"Error creating campaign: {traceback.format_exc()}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- List campaigns -------------------------------------------------------

@app.get("/api/campaigns")
def list_campaigns(request: Request):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM campaigns ORDER BY created_at DESC")
    rows = cur.fetchall()
    cur.close()
    put_db(conn)

    campaigns = []
    for row in rows:
        pending = 0
        with scan_lock:
            pending = scan_counts.get(str(row["id"]), 0)
        campaigns.append(_campaign_to_json(request, row, pending))

    return campaigns


def _campaign_to_json(request: Request, row, pending_scans: int):
    short_id = row["short_id"]
    display = campaign_page_url(request, short_id)
    return {
        "id": str(row["id"]),
        "short_id": short_id,
        "campaign_url": campaign_public_url(request, short_id),
        "qr_display_url": display,
        "qr_base64": row["qr_base64"],
        "url": row["url"],
        "tagline": row["tagline"],
        "total_scans": row["total_scans"] + pending_scans,
        "created_at": row["created_at"].isoformat(),
    }


@app.get("/api/campaigns/by-short/{short_id}")
def get_campaign_by_short(request: Request, short_id: str):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM campaigns WHERE short_id = %s", (short_id,))
    row = cur.fetchone()
    cur.close()
    put_db(conn)
    if not row:
        return JSONResponse({"error": "Campaign not found"}, status_code=404)
    pending = 0
    with scan_lock:
        pending = scan_counts.get(str(row["id"]), 0)
    return _campaign_to_json(request, row, pending)


# ---- Campaign stats -------------------------------------------------------

@app.get("/api/campaigns/{campaign_id}/stats")
def campaign_stats(campaign_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT total_scans FROM campaigns WHERE id = %s", (campaign_id,))
    row = cur.fetchone()
    cur.close()
    put_db(conn)
    db_scans = row[0] if row else 0
    with scan_lock:
        pending = scan_counts.get(campaign_id, 0)
    return {"total_scans": db_scans + pending}


# ---- Delete campaign ------------------------------------------------------

@app.delete("/api/campaigns/{campaign_id}")
def delete_campaign(campaign_id: str):
    try:
        conn = get_db()
        cur = conn.cursor()
        # Get short_id to clear cache
        cur.execute("SELECT short_id FROM campaigns WHERE id = %s", (campaign_id,))
        row = cur.fetchone()
        if not row:
            put_db(conn)
            return JSONResponse({"error": "Campaign not found"}, status_code=404)
        short_id = row[0]
        # Delete scan events first (FK constraint)
        cur.execute("DELETE FROM scan_events WHERE campaign_id = %s", (campaign_id,))
        cur.execute("DELETE FROM campaigns WHERE id = %s", (campaign_id,))
        conn.commit()
        cur.close()
        put_db(conn)
        # Clear from in-memory caches
        with campaign_cache_lock:
            campaign_cache.pop(short_id, None)
        with scan_lock:
            scan_counts.pop(campaign_id, None)
        return {"status": "deleted"}
    except Exception as e:
        logger.error(f"Error deleting campaign: {traceback.format_exc()}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- Favicon --------------------------------------------------------------

@app.get("/favicon.png")
def favicon():
    return FileResponse(os.path.join(BASE_DIR, "favicon.png"), media_type="image/png")


# ---- Redirect (scan) ------------------------------------------------------

@app.get("/r/{short_id}")
def redirect_scan(short_id: str, request: Request):
    # Check in-memory cache first
    with campaign_cache_lock:
        campaign = campaign_cache.get(short_id)

    if not campaign:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, url FROM campaigns WHERE short_id = %s", (short_id,))
        row = cur.fetchone()
        cur.close()
        put_db(conn)
        if not row:
            return JSONResponse({"error": "Campaign not found"}, status_code=404)
        campaign = {"id": str(row["id"]), "url": row["url"]}
        with campaign_cache_lock:
            campaign_cache[short_id] = campaign

    # Buffer scan count + event (flushed to Postgres by background thread)
    user_agent = request.headers.get("user-agent", "")
    with scan_lock:
        scan_counts[campaign["id"]] += 1
        scan_events.append((campaign["id"], user_agent))

    return RedirectResponse(url=campaign["url"], status_code=302)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
