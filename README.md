# QRaft — QR Code Campaign Manager

QRaft lets you create QR code campaigns with custom logos and taglines, then track every scan in real time with a live-updating dashboard.

## Local Setup

1. **Prerequisites**: PostgreSQL and Redis running locally.

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
   export REDIS_URL=redis://localhost:6379
   export BASE_URL=http://localhost:8000
   export PORT=8000
   ```

5. **Run the app**:
   ```bash
   python main.py
   ```
   Open http://localhost:8000 in your browser.

## Deploy on Render

1. Push this repo to GitHub.
2. Go to [Render Dashboard](https://dashboard.render.com) → **New** → **Blueprint**.
3. Connect your repo. Render reads `render.yaml` and creates all 4 services automatically.
4. Update `BASE_URL` in the Render environment to match your web service URL (e.g. `https://qraft.onrender.com`).

## Services

| Service | Type | What it does |
|---------|------|-------------|
| **qraft** | Web | FastAPI app — serves the dashboard and API |
| **qraft-db** | PostgreSQL | Stores campaigns and scan events |
| **qraft-cache** | Redis | Caches campaign data and tracks live scan counts |
| **qraft-scan-report** | Cron | Runs daily at 8 AM, prints a scan report to logs |
