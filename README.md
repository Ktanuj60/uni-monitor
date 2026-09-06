# University Program & Fee Monitor

Watches all 30 universities in `universities.json` twice a day (7 AM & 7 PM IST),
auto-discovers each site's Programs/Specialization pages and Fees pages, and
emails you a line-by-line diff whenever something is added, removed, or changed.
Runs entirely inside GitHub Actions — once set up, it needs no manual trigger.

## How it works
- `scraper.py` — opens each site with a real browser (Playwright), closes any
  popup, finds pages that look like Programs or Fees pages, and grabs their text.
- `sheets_store.py` — saves a snapshot of each page's content to a Google Sheet,
  so tomorrow's run has something to compare against.
- `diff_and_alert.py` — line-level diff between old and new content, formatted
  into an email.
- `main.py` — runs the whole pipeline end to end.
- `.github/workflows/monitor.yml` — the scheduler. This is what removes the
  need for you to run anything by hand.

## One-time setup (about 15 minutes)

### 1. Google Sheet + Service Account (for storing snapshots)
1. Create a new Google Sheet (any name). Copy its ID from the URL:
   `https://docs.google.com/spreadsheets/d/THIS_PART_IS_THE_ID/edit`
2. Go to [Google Cloud Console](https://console.cloud.google.com/) → create a
   project (or use an existing one) → enable the **Google Sheets API**.
3. Create a **Service Account** (IAM & Admin → Service Accounts) → create a
   **JSON key** for it and download it.
4. Open the downloaded JSON, copy the `"client_email"` value, and **share your
   Google Sheet with that email address as Editor** (this step is the one
   people usually miss — without it, the script can't write to the sheet).

### 2. Gmail App Password (for sending alerts)
1. Turn on 2-Step Verification on the Gmail account you want alerts sent from.
2. Go to Google Account → Security → App Passwords → generate one for "Mail".
3. Save the 16-character password — you'll paste it into a GitHub secret below.

### 3. Push this project to GitHub
Create a new (private, recommended) repository and push this whole folder to it.

### 4. Add GitHub Secrets
In your repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add these five:

| Secret name | Value |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Paste the **entire contents** of the service account JSON file |
| `GOOGLE_SHEET_ID` | The Sheet ID from step 1 |
| `GMAIL_USER` | The Gmail address you'll send alerts from |
| `GMAIL_APP_PASSWORD` | The 16-character app password from step 2 |
| `ALERT_EMAIL_TO` | Where alerts should go (comma-separate multiple addresses) |

### 5. Test it
Go to the **Actions** tab → "University Program & Fee Monitor" → **Run workflow**
(this is the manual trigger — `workflow_dispatch`). Watch the log. The first
run just builds the baseline snapshot (no email, nothing to compare against
yet). Run it a second time to confirm the diff/email logic works.

After that, it runs automatically at 7 AM and 7 PM IST every day — no further
action needed from you.

## Important caveats (read before relying on this)
- **Auto-discovery isn't perfect.** The scraper guesses which links are
  "Programs" or "Fees" pages by keyword. For a handful of the 30 sites it may
  pick the wrong page or miss one. Check `last_run_debug.json` (only produced
  when you run `scraper.py` directly, not in Actions) or just watch the first
  few email reports — if a university's alerts look off, add explicit
  overrides to that entry in `universities.json`:
  ```json
  { "name": "...", "url": "...", "program_urls": ["https://exact/page"], "fee_urls": ["https://exact/page"] }
  ```
- **Popup closing is best-effort.** It handles common modal patterns but a
  site with an unusual popup may need a specific selector added to
  `POPUP_CLOSE_SELECTORS` in `scraper.py`.
- **Sites can change their structure** at any time, which may require
  revisiting the keyword lists or per-site overrides occasionally — this
  isn't a "set up once, never look at it again forever" system, but it does
  run and alert you completely unattended day to day.
- **Adding more universities later**: just add entries to `universities.json`
  — no other code changes needed.
- **Phase 2 (offers/scholarships/announcements)**: intentionally not built
  yet, as agreed — this first version is programs + fees only. Say the word
  and I'll add a second, separate scraper + email digest for that, reusing
  this same scheduling setup.
