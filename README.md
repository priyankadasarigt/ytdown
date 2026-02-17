# YT Downloader – Koyeb Deployment Guide

## What's changed from the Railway version

| Area | Change |
|---|---|
| **Bot detection bypass** | yt-dlp now uses the Android player client, realistic browser headers, and exponential-backoff retries |
| **Cookie support** | Three ways to supply cookies (see below) |
| **Download folder** | Changed to `/tmp/downloads` (writable on Koyeb) |
| **Port** | Defaults to `8000` (Koyeb standard) |
| **Dockerfile** | Added for one-click Koyeb deploy |

---

## Deploying on Koyeb

### Option A – Docker (recommended)
1. Push this repo to GitHub / GitLab.
2. In Koyeb → **Create Service** → select your repo.
3. Koyeb auto-detects the `Dockerfile`.
4. Set the **port** to `8000`.
5. Add your environment variables (see below).

### Option B – Buildpack
Koyeb can also auto-detect Python. The `Procfile` is included for this case.

---

## Environment Variables (set in Koyeb dashboard)

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | ✅ | Flask secret key |
| `FRONTEND_URL` | ✅ | Your frontend URL e.g. `https://theyt.pages.dev` |
| `R2_ACCOUNT_ID` | ✅ | Cloudflare account ID |
| `R2_ACCESS_KEY_ID` | ✅ | R2 access key |
| `R2_SECRET_ACCESS_KEY` | ✅ | R2 secret key |
| `R2_BUCKET_NAME` | ✅ | R2 bucket name |
| `R2_PUBLIC_URL` | ✅ | Public URL for R2 |
| `COOKIE_BASE64` | Recommended | Base64-encoded cookies.txt (easiest for Koyeb) |
| `COOKIE_FILE` | Optional | Path to cookies.txt on disk (Docker image only) |
| `ADMIN_SECRET` | Optional | Protects the `/api/upload_cookies` endpoint |

---

## How to set up cookies (fixes the YouTube bot error)

YouTube sometimes blocks bot-like traffic. Providing cookies from a **logged-in YouTube session** bypasses this.

### Step 1 – Export cookies from your browser

Install the browser extension **"Get cookies.txt LOCALLY"** (Chrome/Firefox).

1. Log into YouTube in your browser.
2. Click the extension → **Export** → save as `cookies.txt`.

### Step 2 – Supply the cookie file to Koyeb

**Easiest: COOKIE_BASE64 env var**

```bash
# On your local machine, base64-encode the file:
base64 -w 0 cookies.txt
# (on macOS use: base64 -i cookies.txt)
```

Copy the output string and paste it as the `COOKIE_BASE64` environment variable in Koyeb. The server decodes it automatically at startup.

**Alternative: Upload via API at runtime**

```bash
curl -X POST https://your-koyeb-app.koyeb.app/api/upload_cookies \
  -H "X-Admin-Secret: your_admin_secret" \
  -F "cookies=@cookies.txt"
```

**Check cookie status:**

```
GET https://your-koyeb-app.koyeb.app/api/cookie_status
```

---

## How the bot-bypass works

Even without cookies, the app now:

- Uses the **Android YouTube client** (`player_client: android`) – YouTube applies lighter bot checks to mobile clients.
- Sends realistic Chrome browser headers.
- Retries with **exponential back-off** (2, 4, 8 … seconds) on temporary errors.
- Sleeps 1-3 seconds between requests to mimic human behaviour.

Cookies on top of this make it very unlikely to get the bot error.

---

## Health check endpoint

```
GET /health
→ { "status": "online", "idle_minutes": 0.5, "cookies_active": true }
```

Koyeb health check path: `/health`
