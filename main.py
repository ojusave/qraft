import os
import json
import base64
import io
import string
import random
import logging
import traceback

import psycopg2
import psycopg2.extras
import redis
import qrcode
import qrcode.constants
import requests as http_requests
from PIL import Image
from fastapi import FastAPI, File, Form, UploadFile, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/qraft")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
PORT = int(os.environ.get("PORT", 8000))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("qraft")

app = FastAPI(title="QRaft")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


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
    cur.close()
    conn.close()
    logger.info("Migrations complete")


# ---------------------------------------------------------------------------
# Redis helper
# ---------------------------------------------------------------------------

def get_redis():
    return redis.from_url(REDIS_URL, decode_responses=True)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
def startup():
    run_migrations()


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

SUPPORTED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}


def generate_short_id(length=8):
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choices(alphabet, k=length))


def generate_qr(data: str, logo_image=None) -> str:
    """Generate a QR PNG as a base64 string, optionally with a centered logo."""
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


# ---- Health ---------------------------------------------------------------

@app.get("/api/health")
def health():
    result = {"status": "ok", "postgres": "ok", "redis": "ok"}
    try:
        conn = get_db()
        conn.cursor().execute("SELECT 1")
        conn.close()
    except Exception:
        result["postgres"] = "error"
        result["status"] = "error"
    try:
        r = get_redis()
        r.ping()
    except Exception:
        result["redis"] = "error"
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

        # Check file upload first — skip entirely if no real file
        has_file = False
        if logo_file and hasattr(logo_file, "read"):
            filename = getattr(logo_file, "filename", None) or ""
            content_type = getattr(logo_file, "content_type", None) or ""
            logger.info(f"logo_file received: filename='{filename}', content_type='{content_type}', type={type(logo_file)}")
            if filename.strip() and content_type in SUPPORTED_IMAGE_TYPES:
                data = await logo_file.read()
                if len(data) > 10:
                    logo_image = Image.open(io.BytesIO(data))
                    logo_base64_str = base64.b64encode(data).decode()
                    has_file = True
                    logger.info(f"Logo loaded from file upload: {filename} ({len(data)} bytes)")

        # Fallback to URL
        if not logo_image and logo_url and str(logo_url).strip():
            logo_url = str(logo_url).strip()
            resp = http_requests.get(logo_url, timeout=10)
            resp.raise_for_status()
            if len(resp.content) > 0:
                logo_image = Image.open(io.BytesIO(resp.content))
                logo_base64_str = base64.b64encode(resp.content).decode()
                logo_url_str = logo_url
                logger.info(f"Logo loaded from URL: {logo_url}")

        short_id = generate_short_id()
        qr_b64 = generate_qr(str(url), logo_image)

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
        cur.close()
        conn.close()

        # Invalidate cache
        try:
            r = get_redis()
            r.delete("campaigns:recent")
        except Exception:
            pass

        return {
            "id": str(row["id"]),
            "short_id": row["short_id"],
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
def list_campaigns():
    r = None
    try:
        r = get_redis()
        cached = r.get("campaigns:recent")
        if cached:
            campaigns = json.loads(cached)
            # Overlay live scan counts from Redis
            for c in campaigns:
                val = r.get(f"campaign:{c['id']}:scans")
                c["total_scans"] = int(val) if val else c.get("total_scans", 0)
            return campaigns
    except Exception:
        pass

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM campaigns ORDER BY created_at DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    campaigns = []
    for row in rows:
        live_scans = row["total_scans"]
        if r:
            try:
                val = r.get(f"campaign:{row['id']}:scans")
                if val:
                    live_scans = int(val)
            except Exception:
                pass
        campaigns.append({
            "id": str(row["id"]),
            "short_id": row["short_id"],
            "qr_base64": row["qr_base64"],
            "url": row["url"],
            "tagline": row["tagline"],
            "total_scans": live_scans,
            "created_at": row["created_at"].isoformat(),
        })

    if r:
        try:
            r.setex("campaigns:recent", 60, json.dumps(campaigns))
        except Exception:
            pass

    return campaigns


# ---- Campaign stats -------------------------------------------------------

@app.get("/api/campaigns/{campaign_id}/stats")
def campaign_stats(campaign_id: str):
    redis_scans = 0
    try:
        r = get_redis()
        val = r.get(f"campaign:{campaign_id}:scans")
        redis_scans = int(val) if val else 0
    except Exception:
        pass

    postgres_scans = 0
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT total_scans FROM campaigns WHERE id = %s", (campaign_id,))
        row = cur.fetchone()
        if row:
            postgres_scans = row[0]
        cur.close()
        conn.close()
    except Exception:
        pass

    return {"redis_scans": redis_scans, "postgres_scans": postgres_scans}


# ---- Redirect (scan) ------------------------------------------------------

@app.get("/r/{short_id}")
def redirect_scan(short_id: str, request: Request):
    r = None
    campaign = None

    # Try Redis cache first
    try:
        r = get_redis()
        cached = r.get(f"campaign:short:{short_id}")
        if cached:
            campaign = json.loads(cached)
    except Exception:
        pass

    # Fallback to Postgres
    if not campaign:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, url FROM campaigns WHERE short_id = %s", (short_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return JSONResponse({"error": "Campaign not found"}, status_code=404)
        campaign = {"id": str(row["id"]), "url": row["url"]}
        if r:
            try:
                r.setex(f"campaign:short:{short_id}", 300, json.dumps(campaign))
            except Exception:
                pass

    campaign_id = campaign["id"]
    dest_url = campaign["url"]

    # Increment Redis counter atomically
    try:
        if r:
            r.incr(f"campaign:{campaign_id}:scans")
    except Exception:
        pass

    # Write scan event + update total in Postgres
    user_agent = request.headers.get("user-agent", "")
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO scan_events (campaign_id, user_agent) VALUES (%s, %s)",
            (campaign_id, user_agent),
        )
        cur.execute(
            "UPDATE campaigns SET total_scans = total_scans + 1 WHERE id = %s",
            (campaign_id,),
        )
        cur.close()
        conn.close()
    except Exception:
        pass

    return RedirectResponse(url=dest_url, status_code=302)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
