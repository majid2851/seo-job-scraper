import re
import requests
import os
import html
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

# ─── Optional: Google Sheets ──────────────────────────────────────────────────
try:
    import gspread
    from google.oauth2.service_account import Credentials
    SHEETS_AVAILABLE = True
except ImportError:
    SHEETS_AVAILABLE = False

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
RAPIDAPI_KEY       = os.environ["RAPIDAPI_KEY"]
TELEGRAM_TOKEN     = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
GSHEET_CREDENTIALS = os.environ.get("GSHEET_CREDENTIALS", "")
GSHEET_ID          = os.environ.get("GSHEET_ID", "")
GSHEET_SHEET_NAME  = "Jobs"

SEEN_JOBS_FILE    = Path("seen_jobs.txt")
MAX_SEEN_JOBS     = 2000
MAX_JOBS_PER_RUN  = 15

# True  = only jobs that explicitly mention worldwide / work from anywhere
# False = reject geo-restricted jobs, but allow vague "remote" postings
STRICT_WORLDWIDE_ONLY = False

# ─── Search queries ───────────────────────────────────────────────────────────
SEARCH_QUERIES = [
    "Android Developer remote worldwide",
    "Android Engineer remote worldwide",
    "Senior Android Developer remote worldwide",
    "Senior Android Engineer remote worldwide",
    "Kotlin Developer remote worldwide",
]

# ─── Role / type blacklist ────────────────────────────────────────────────────
BLACKLIST_KEYWORDS = [
    "director",
    "agency",
]

# ─── Geographic restriction blacklist ─────────────────────────────────────────
GEO_BLACKLIST = [
    # US
    "us only", "usa only", "u.s. only", "united states only",
    "us residents only", "must reside in us", "must be located in the us",
    "must be based in the us", "based in the united states",
    "authorized to work in the us", "authorized to work in the united states",
    "legally authorized to work in the u.s", "work authorization in the us",
    "us citizen", "us citizenship", "green card",
    "must live in the us", "must be in the us",
    "remote - us", "remote (us)", "remote us only", "us remote only",

    # UK / EU / other regions
    "uk only", "united kingdom only", "must be based in the uk",
    "eu only", "europe only", "eea only", "schengen only",
    "canada only", "must be located in canada",
    "australia only", "must be in australia",
    "india only", "must be based in india",

    # Generic location locks
    "must be based in", "must be located in", "must reside in",
    "must live in", "candidates must be located",
    "geographic restriction", "location restriction",
    "within commuting distance", "on-site required", "hybrid only",
    "relocation required",

    # Timezone / region hints
    "pst only", "est only", "cet only",
    "us business hours required",
    "within 3 hours of", "same timezone as",
]

# ─── Worldwide whitelist (used when STRICT_WORLDWIDE_ONLY = True) ─────────────
WORLDWIDE_KEYWORDS = [
    "work from anywhere",
    "work anywhere",
    "worldwide",
    "global remote",
    "fully remote worldwide",
    "any country",
    "no location restriction",
    "location independent",
    "open to candidates globally",
    "international candidates welcome",
    "remote worldwide",
]

# Title patterns like "Remote - US" or "(US Remote)"
RESTRICTIVE_TITLE_PATTERNS = [
    r"\bremote\s*[-–]\s*(us|usa|uk|eu|canada|australia|india)\b",
    r"\b(us|usa|uk|eu|canada|australia|india)\s+remote\b",
    r"\(remote\s*[-–]?\s*(us|usa|uk|eu|canada|australia|india)\)",
]


# ══════════════════════════════════════════════════════════════════════════════
# Persistent cache — seen_jobs.txt
# ══════════════════════════════════════════════════════════════════════════════

def load_seen_jobs() -> set:
    if SEEN_JOBS_FILE.exists():
        ids = set(line.strip() for line in SEEN_JOBS_FILE.read_text().splitlines() if line.strip())
        log.info(f"Loaded {len(ids)} seen job IDs from cache")
        return ids
    log.info("No cache file found — starting fresh")
    return set()


def save_seen_jobs(seen: set) -> None:
    ids_list = list(seen)
    if len(ids_list) > MAX_SEEN_JOBS:
        ids_list = ids_list[-MAX_SEEN_JOBS:]
    SEEN_JOBS_FILE.write_text("\n".join(ids_list))
    log.info(f"Saved {len(ids_list)} job IDs to cache")


# ══════════════════════════════════════════════════════════════════════════════
# JSearch API
# ══════════════════════════════════════════════════════════════════════════════

def search_jobs(query: str, retries: int = 3) -> list:
    url = "https://jsearch.p.rapidapi.com/search"
    headers = {
        "x-rapidapi-key":  RAPIDAPI_KEY,
        "x-rapidapi-host": "jsearch.p.rapidapi.com",
    }
    params = {
        "query":          query,
        "num_pages":      "1",
        "date_posted":    "3days",
        "work_from_home": "true",
    }

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=20)

            if resp.status_code == 429:
                log.warning("Rate limit hit — waiting 60s before retry...")
                time.sleep(60)
                continue

            if resp.status_code == 403:
                log.error("API key invalid or not subscribed (403)")
                return []

            resp.raise_for_status()
            data = resp.json()

            if data.get("status") != "OK":
                log.warning(f"API non-OK for '{query}': {data.get('error')}")
                return []

            return data.get("data", [])

        except requests.exceptions.Timeout:
            log.warning(f"Timeout on attempt {attempt}/{retries} for '{query}'")
        except requests.exceptions.JSONDecodeError:
            log.error(f"Invalid JSON response for '{query}'")
            return []
        except requests.exceptions.RequestException as e:
            log.error(f"Request error (attempt {attempt}/{retries}): {e}")

        if attempt < retries:
            wait = 5 * attempt
            log.info(f"Waiting {wait}s before retry...")
            time.sleep(wait)

    log.error(f"All {retries} attempts failed for '{query}'")
    return []


# ══════════════════════════════════════════════════════════════════════════════
# Filters
# ══════════════════════════════════════════════════════════════════════════════

def get_searchable_text(job: dict) -> str:
    parts = [
        job.get("job_title") or "",
        job.get("job_description") or "",
        job.get("job_location") or "",
        job.get("job_city") or "",
        job.get("job_state") or "",
        job.get("job_country") or "",
    ]

    highlights = job.get("job_highlights") or {}
    if isinstance(highlights, dict):
        for section in highlights.values():
            if isinstance(section, list):
                parts.extend(section)

    return " ".join(parts).lower()


def is_blacklisted(job: dict) -> bool:
    text = get_searchable_text(job)

    for keyword in BLACKLIST_KEYWORDS:
        if keyword.lower() in text:
            log.info(f"  ⛔ Blacklisted '{job.get('job_title')}' — matched: '{keyword}'")
            return True
    return False


def is_geo_restricted(job: dict) -> bool:
    text = get_searchable_text(job)

    for keyword in GEO_BLACKLIST:
        if keyword.lower() in text:
            log.info(f"  🌍 Geo-restricted '{job.get('job_title')}' — matched: '{keyword}'")
            return True

    title = (job.get("job_title") or "").lower()
    for pattern in RESTRICTIVE_TITLE_PATTERNS:
        if re.search(pattern, title):
            log.info(f"  🌍 Geo-restricted title pattern: '{job.get('job_title')}'")
            return True

    return False


def is_worldwide_eligible(job: dict) -> bool:
    text = get_searchable_text(job)
    return any(kw in text for kw in WORLDWIDE_KEYWORDS)


# ══════════════════════════════════════════════════════════════════════════════
# Telegram
# ══════════════════════════════════════════════════════════════════════════════

def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            log.error(f"Telegram error {resp.status_code}: {resp.text[:300]}")
            return False
        return True
    except Exception as e:
        log.error(f"Telegram send exception: {e}")
        return False


def extract_salary(job: dict) -> str:
    if job.get("job_salary_string"):
        return job["job_salary_string"]

    min_s  = job.get("job_min_salary")
    max_s  = job.get("job_max_salary")
    period = (job.get("job_salary_period") or "").lower()

    period_map = {"year": "/yr", "month": "/mo", "hour": "/hr", "week": "/wk"}
    period_label = period_map.get(period, f"/{period}" if period else "")

    if min_s and max_s:
        return f"${int(min_s):,} – ${int(max_s):,}{period_label}"
    if min_s:
        return f"${int(min_s):,}+{period_label}"
    return ""


def format_job(job: dict) -> str:
    title    = html.escape(job.get("job_title")    or "بدون عنوان")
    company  = html.escape(job.get("employer_name") or "نامشخص")
    city     = html.escape(job.get("job_city")     or "")
    country  = html.escape(job.get("job_country")  or "")
    location = f"{city}, {country}".strip(", ") or "Remote"
    source   = html.escape(job.get("job_publisher") or "")
    link     = job.get("job_apply_link") or job.get("job_google_link") or ""
    salary   = extract_salary(job)

    lines = [
        f"💼 <b>{title}</b>",
        f"🏢 {company}",
        f"📍 {location}",
    ]

    if salary:
        lines.append(f"💰 <b>{html.escape(salary)}</b>")

    if source:
        lines.append(f"🌐 {source}")

    if link:
        lines.append(f'🔗 <a href="{link}">Apply Now</a>')

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Google Sheets (optional)
# ══════════════════════════════════════════════════════════════════════════════

def get_sheets_client():
    if not SHEETS_AVAILABLE:
        log.info("gspread not installed — skipping Google Sheets")
        return None
    if not GSHEET_CREDENTIALS or not GSHEET_ID:
        log.info("GSHEET_CREDENTIALS or GSHEET_ID not set — skipping Google Sheets")
        return None
    try:
        creds_dict = json.loads(GSHEET_CREDENTIALS)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds  = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        log.info("Google Sheets connected ✅")
        return client
    except json.JSONDecodeError:
        log.error("GSHEET_CREDENTIALS is not valid JSON")
    except Exception as e:
        log.error(f"Google Sheets auth error: {e}")
    return None


def ensure_sheet_headers(client) -> None:
    if client is None:
        return
    try:
        sheet = client.open_by_key(GSHEET_ID).worksheet(GSHEET_SHEET_NAME)
        first_row = sheet.row_values(1)
        if not first_row:
            headers = [
                "Job Title", "Company", "Apply Link", "Posted Date",
                "City", "Country", "Salary", "Saved At (UTC)",
            ]
            sheet.insert_row(headers, 1)
            log.info("Sheet headers created")
    except Exception as e:
        log.error(f"Sheet header check error: {e}")


def append_to_sheet(client, job: dict) -> None:
    if client is None:
        return
    try:
        sheet = client.open_by_key(GSHEET_ID).worksheet(GSHEET_SHEET_NAME)
        posted = (job.get("job_posted_at_datetime_utc") or "")[:10]
        row = [
            job.get("job_title", ""),
            job.get("employer_name", ""),
            job.get("job_apply_link") or job.get("job_google_link") or "",
            posted,
            job.get("job_city", ""),
            job.get("job_country", ""),
            extract_salary(job),
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        ]
        sheet.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        log.error(f"Sheet append error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log.info(f"═══ Bot started at {now} ═══")
    log.info(f"Filter mode: {'STRICT worldwide only' if STRICT_WORLDWIDE_ONLY else 'Geo blacklist (loose)'}")

    seen_jobs     = load_seen_jobs()
    sheets_client = get_sheets_client()
    ensure_sheet_headers(sheets_client)

    new_jobs      = []
    blacklisted   = 0
    geo_filtered  = 0
    not_worldwide = 0
    already_seen  = 0
    errors        = 0

    for query in SEARCH_QUERIES:
        log.info(f"Searching: '{query}'")
        try:
            jobs = search_jobs(query)
            log.info(f"  → {len(jobs)} raw results")

            for job in jobs:
                try:
                    job_id = job.get("job_id") or job.get("job_apply_link") or ""
                    if not job_id:
                        continue

                    if job_id in seen_jobs:
                        already_seen += 1
                        continue

                    seen_jobs.add(job_id)

                    if is_blacklisted(job):
                        blacklisted += 1
                        continue

                    if is_geo_restricted(job):
                        geo_filtered += 1
                        continue

                    if STRICT_WORLDWIDE_ONLY and not is_worldwide_eligible(job):
                        not_worldwide += 1
                        log.info(f"  🌐 Not worldwide '{job.get('job_title')}'")
                        continue

                    new_jobs.append(job)

                except Exception as e:
                    log.error(f"  Error processing job item: {e}")
                    errors += 1
                    continue

        except Exception as e:
            log.error(f"Error in query '{query}': {e}")
            errors += 1
            continue

        time.sleep(1.5)

    dedup_seen = set()
    unique_jobs = []
    for job in new_jobs:
        jid = job.get("job_id", "")
        if jid and jid not in dedup_seen:
            dedup_seen.add(jid)
            unique_jobs.append(job)

    log.info(
        f"Summary → new: {len(unique_jobs)} | role-filtered: {blacklisted} | "
        f"geo-filtered: {geo_filtered} | not-worldwide: {not_worldwide} | "
        f"already seen: {already_seen} | errors: {errors}"
    )

    if not unique_jobs:
        send_telegram(
            f"🔍 <b>گزارش روزانه</b>\n"
            f"📅 {now}\n\n"
            f"✅ آگهی جدیدی امروز پیدا نشد.\n"
            f"⛔ فیلتر نقش: {blacklisted} | 🌍 فیلتر مکان: {geo_filtered} | "
            f"🌐 غیرجهانی: {not_worldwide} | 🔁 تکراری: {already_seen}"
        )
        save_seen_jobs(seen_jobs)
        return

    send_telegram(
        f"🔍 <b>آگهی‌های شغلی جدید</b>\n"
        f"📅 {now}\n"
        f"📊 {len(unique_jobs)} آگهی جدید | 🌍 {geo_filtered} فیلتر مکان\n"
        f"➖➖➖➖➖➖➖➖"
    )
    time.sleep(1)

    sent = 0
    for job in unique_jobs[:MAX_JOBS_PER_RUN]:
        try:
            msg = format_job(job)
            if send_telegram(msg):
                sent += 1
                append_to_sheet(sheets_client, job)
            time.sleep(0.8)
        except Exception as e:
            log.error(f"Error sending job to Telegram: {e}")
            continue

    save_seen_jobs(seen_jobs)
    log.info(f"═══ Done. Sent {sent}/{len(unique_jobs)} jobs ═══")


if __name__ == "__main__":
    main()
