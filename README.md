# QRaft — QR Code Campaign Manager

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/ojusave/qraft)

QRaft lets you create QR code campaigns with custom logos and taglines, then track every scan in real time with a live-updating dashboard.

## Features

- Generate QR codes with optional logo overlays (PNG, JPG, WebP, GIF)
- Full-page QR display optimized for projecting to large audiences
- Live scan counter updates every 2 seconds
- Scan tracking via redirect URLs (QR → your server → destination)
- Daily scan report cron job

## Tech Stack

- **Backend**: Python / FastAPI
- **Database**: PostgreSQL (with connection pooling)
- **Frontend**: Vanilla JS, single-page `index.html` with DDS styling
- **Hosting**: Render (Blueprint deployment)

## Local Setup

1. **Prerequisites**: PostgreSQL running locally.

2. **Create the database**:
   ```bash
   createdb qraft
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Set environment variables** (copy `.env.example` to `.env` and source it, or export directly):
   ```bash
   export DATABASE_URL=postgresql://localhost:5432/qraft
   ```

5. **Run the app**:
   ```bash
   python main.py
   ```
   Open http://localhost:8000 in your browser.

## Deploy on Render

Click the **Deploy to Render** button above, or manually:

1. Push this repo to GitHub.
2. Go to [Render Dashboard](https://dashboard.render.com) → **New** → **Blueprint**.
3. Connect your repo. Render reads `render.yaml` and creates all services automatically.

No environment variables to configure — the app auto-detects its own URL from incoming requests.

## Services

| Service | Type | What it does |
|---------|------|-------------|
| **qraft** | Web | FastAPI app — serves the dashboard and API |
| **qraft-db** | PostgreSQL | Stores campaigns and scan events |
| **qraft-scan-report** | Cron | Runs daily at 8 AM, prints a scan report to logs |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serves the dashboard UI |
| `GET` | `/api/health` | Health check (Postgres connectivity) |
| `POST` | `/api/campaigns` | Create a new campaign (multipart form) |
| `GET` | `/api/campaigns` | List all campaigns |
| `GET` | `/api/campaigns/{id}/stats` | Get live scan count for a campaign |
| `GET` | `/r/{short_id}` | Redirect endpoint — tracks scan and redirects to destination URL |

## How Scan Tracking Works

1. QR codes encode a redirect URL through the app (`/r/{short_id}`)
2. When scanned, the request hits the server, which records the scan and redirects to the destination
3. Scans are buffered in memory and batch-flushed to Postgres every 5 seconds for performance
4. Individual scan events (timestamp, user agent) are stored in the `scan_events` table

## Architecture

- **Connection pooling** via psycopg2 ThreadedConnectionPool (2–20 connections)
- **In-memory scan counter** batches writes to Postgres every 5 seconds (handles 500+ concurrent scans)
- **In-memory campaign cache** for fast QR redirect lookups (no DB hit per scan)
