# Incident Auto-Assignment Bot

Assigns new incidents to whichever roster agent is currently on-shift,
available, and supports the incident's business service — round-robin
between ties.

## Files
- `main.py` — the FastAPI server (matching logic + ServiceNow callback)
- `roster.xlsx` — your shift roster (same format as Template_Rooster.xlsx)
- `requirements.txt` — Python deps
- `.env.example` — required environment variables
- `render.yaml` — one-file config for Render's free tier
- `servicenow_business_rule.js` — paste-in Business Rule for ServiceNow

## How it works
1. Someone creates an incident in ServiceNow.
2. An **async** Business Rule fires a POST to your Python server with the
   incident's sys_id, business service, and open time.
3. The server re-reads `roster.xlsx`, filters to agents who are
   `Available` + on-shift right now + support that business service,
   round-robins between matches, and looks up that agent's ServiceNow
   sys_id by email.
4. It PATCHes the incident's `assigned_to` field back via the Table API.

## 1. Run it locally first
```bash
pip install -r requirements.txt
cp .env.example .env      # fill in real values
uvicorn main:app --reload --port 8000
```
Test it:
```bash
curl -X POST http://localhost:8000/assign \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <APP_API_KEY from .env>" \
  -d '{"sys_id":"test","number":"INC0000001","business_service":"BS_GDI_SAP_Sabre","opened_at":"2026-08-20 10:00:00"}'
```

## 2. Deploy for free — Render
Render's free web service tier needs no credit card, gives you free
HTTPS + a public URL, and auto-detects Python. Trade-off: it spins down
after ~15 minutes idle and takes 30-50s to wake on the next request — fine
for a pilot, worth upgrading to a paid instance (or a cheap always-on
Railway/Fly.io box) once this is live for real incident volume.

1. Push this folder to a GitHub repo (include `roster.xlsx`, or better,
   swap `load_roster()` to pull from wherever your roster actually lives
   long-term — see note below).
2. Go to https://render.com → New → **Web Service** → connect the repo.
3. Render reads `render.yaml` automatically. If asked manually instead:
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Under **Environment**, set `APP_API_KEY`, `SN_INSTANCE`, `SN_USER`,
   `SN_PASSWORD` (use a dedicated ServiceNow integration account scoped
   to read `sys_user` and write `incident.assigned_to`, not a personal
   login).
5. Deploy. You'll get a URL like `https://incident-assignment-bot.onrender.com`.
6. Check `https://<your-url>/health` returns `{"status":"ok"}`.

**Keeping it warm (optional):** to avoid the 30-50s cold-start delay on
the first incident after idle time, ping `/health` every 10 minutes with
a free cron service like cron-job.org.

## 3. Wire up ServiceNow
Open `servicenow_business_rule.js` — it has the exact steps at the top
(table, when-to-run settings, where to put your URL and API key). Short
version:
1. Create a System Property `x_custom.assignment_bot_api_key` with the
   same value as `APP_API_KEY`.
2. New Business Rule on `incident`, When = `async`, Insert = true,
   filter: assigned_to is empty.
3. Paste the script, swap in your Render URL.

## Known data gotcha in the roster template
`Shift_End_Time` must be true 24-hour time. A day shift listed as
`09:00:00` → `06:00:00` reads as an overnight shift (9am today until
6am tomorrow) instead of a 9-hour day shift — it should be `18:00:00`.
The server logs a `[roster warning]` for any shift that computes to
over 16 hours so you can catch typos like this before they cause a
wrong assignment. Double check every row before going live.

## Scaling notes
- Round-robin state and the email→sys_id cache are in-memory, so they
  reset on restart and won't stay in sync if you ever run more than one
  server instance. Fine for a single free-tier instance; move to Redis
  (or any shared KV store) if you scale out.
- Reading the whole `.xlsx` on every request is fine at low ticket
  volume. If incident volume grows, swap the roster source for a small
  database table or a ServiceNow table you query via API instead of a
  spreadsheet — same matching logic, different `load_roster()`.
- For real production use, also add: retry/backoff on the ServiceNow
  callback, a fallback assignment group if no one matches, and audit
  logging of every assignment decision.
