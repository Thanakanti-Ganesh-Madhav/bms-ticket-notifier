"""
BMS Ticket Checker — CI/Headless mode for GitHub Actions.
Runs once, checks all configured watches, emails on changes.
State is persisted via a JSON artifact.

Configure via environment variables or edit the CONFIG below.
"""

import os
import re
import sys
import json
import time
import threading
import webbrowser
from html import escape
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urlparse
import requests

# Load optional .env file
def load_dotenv():
    env_file = ".env"
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k not in os.environ:
                        os.environ[k] = v

load_dotenv()

# ──────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit these or set via env vars / .env
# ──────────────────────────────────────────────────────────────────────
CONFIG = {
    "url": (
        os.getenv("BMS_URL", "").strip()
        or (
            "https://in.bookmyshow.com/movies/hyderabad/dune-part-three/buytickets/ET00526390,"
            "https://in.bookmyshow.com/movies/hyderabad/avengers-doomsday/buytickets/ET00439706"
        )
    ),
    "dates": (
        os.getenv("BMS_DATES", "").strip()
        or "20261216,20261217,20261218,20261219,20261220,20261221,20261222,20261223"
    ),
    "format": os.getenv("BMS_FORMAT", ""),        # comma-separated e.g. "IMAX,Dolby,2D", empty = all formats
    "theatre": os.getenv("BMS_THEATRE", ""),       # substring filter, empty = all
    "time_period": os.getenv("BMS_TIME", ""),      # e.g. "evening,night", empty = all
}

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev")

STATE_FILE = "bms_state.json"

# ──────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────
AVAIL_STATUS_MAP = {
    "0": ("SOLD OUT",    "🔴"),
    "1": ("ALMOST FULL", "🟡"),
    "2": ("FILLING FAST","🟠"),
    "3": ("AVAILABLE",   "🟢"),
}

DATE_STYLE_MAP = {
    "date-selected": "BOOKABLE",
    "date-disabled": "NOT_OPEN",
    "date-default":  "AVAILABLE",
}

TIME_PERIODS = {
    "morning":   (600, 1200),
    "afternoon": (1200, 1600),
    "evening":   (1600, 1900),
    "night":     (1900, 2400),
}

REGION_MAP = {
    "chennai":    ("CHEN",   "chennai",    "13.056", "80.206", "tf3"),
    "mumbai":     ("MUMBAI", "mumbai",     "19.076", "72.878", "te7"),
    "delhi-ncr":  ("NCR",    "delhi-ncr",  "28.613", "77.209", "ttn"),
    "delhi":      ("NCR",    "delhi-ncr",  "28.613", "77.209", "ttn"),
    "bengaluru":  ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "bangalore":  ("BANG",   "bengaluru",  "12.972", "77.594", "tdr"),
    "hyderabad":  ("HYD",    "hyderabad",  "17.385", "78.487", "tep"),
    "kolkata":    ("KOLK",   "kolkata",    "22.573", "88.364", "tun"),
    "pune":       ("PUNE",   "pune",       "18.520", "73.856", "te2"),
    "kochi":      ("KOCH",   "kochi",      "9.932",  "76.267", "t9z"),
}


# ─────────────────────────────────────���────────────────────────────────
# DATA
# ──────────────────────────────────────────────────────────────────────
@dataclass
class CatInfo:
    name: str
    price: str
    status: str

@dataclass
class ShowInfo:
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    screen_attr: str
    format: str = ""
    movie: str = ""
    categories: list[CatInfo] = field(default_factory=list)

@dataclass
class DateInfo:
    date_code: str
    status: str


# ──────────────────────────────────────────────────────────────────────
# URL PARSER + REGION RESOLVER
# ──────────────────────────────────────────────────────────────────────
def parse_bms_url(url):
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    result = {"event_code": None, "date_code": None, "region_slug": None}
    for p in parts:
        if re.match(r"^ET\d{8,}$", p):
            result["event_code"] = p
        elif re.match(r"^\d{8}$", p):
            result["date_code"] = p
    if "movies" in parts:
        idx = parts.index("movies")
        if idx + 1 < len(parts):
            result["region_slug"] = parts[idx + 1]
    return result


def resolve_region(slug):
    key = (slug or "").lower().strip()
    if key in REGION_MAP:
        return REGION_MAP[key]
    return (key.upper()[:6], key, "0", "0", "")


# ──────────────────────────────────────────────────────────────────────
# BMS API
# ──────────────────────────────────────────────────────────────────────
API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)

HTTP_SESSION = requests.Session()


def fetch_bms(event_code, date_code, region_code, region_slug,
              lat, lon, geohash):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": (
            f"https://in.bookmyshow.com/movies/"
            f"{region_slug}/buytickets/{event_code}/"
        ),
        "x-app-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
    }
    params = {
        "eventCode": event_code,
        "dateCode": date_code or "",
        "isDesktop": "true",
        "regionCode": region_code,
        "lat": lat,
        "lon": lon,
    }
    try:
        resp = HTTP_SESSION.get(API_URL, headers=headers,
                                params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        print(f"  HTTP {resp.status_code}")
    except requests.RequestException as e:
        print(f"  Request failed: {e}")
    return None


# ──────────────────────────────────────────────────────────────────────
# PARSERS
# ──────────────────────────────────────────────────────────────────────
def extract_formats(data):
    """Extract all available format names and their respective event codes."""
    formats = {}
    bs = data.get("data", {}).get("bottomSheetData", {}).get("format-selector", {})
    for w in bs.get("widgets", []):
        if w.get("type") == "chip-list":
            for item in w.get("data", []):
                title = item.get("title", "").strip()
                ev = item.get("cta", {}).get("additionalData", {}).get("eventCode", "")
                if title and ev:
                    formats[title] = ev
    return formats


def parse_movie_info(data):
    info = {"name": "Unknown Movie", "language": ""}
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") == "horizontal-text-list":
            for item in w.get("data", []):
                for row in item.get("leftText", {}).get("data", []):
                    for c in row.get("components", []):
                        if "•" in c.get("text", ""):
                            info["language"] = c["text"].strip()
    bs = data.get("data", {}).get("bottomSheetData", {})
    for w in bs.get("format-selector", {}).get("widgets", []):
        if w.get("type") == "vertical-text-list":
            for d in w.get("data", []):
                if d.get("styleId") == "bottomsheet-subtitle":
                    info["name"] = d.get("text", info["name"])
    return info


def parse_dates(data):
    dates = []
    for w in data.get("data", {}).get("topStickyWidgets", []):
        if w.get("type") != "horizontal-block-list":
            continue
        for item in w.get("data", []):
            texts = item.get("data", [])
            if len(texts) >= 3:
                style = item.get("styleId", "")
                dates.append(DateInfo(
                    date_code=item.get("id", ""),
                    status=DATE_STYLE_MAP.get(style, "UNKNOWN"),
                ))
    return dates


def parse_shows(data, format_name="", movie_name=""):
    shows = []
    for w in data.get("data", {}).get("showtimeWidgets", []):
        if w.get("type") != "groupList":
            continue
        for g in w.get("data", []):
            if g.get("type") != "venueGroup":
                continue
            for card in g.get("data", []):
                if card.get("type") != "venue-card":
                    continue
                addl = card.get("additionalData", {})
                vname = addl.get("venueName", "Unknown")
                vcode = addl.get("venueCode", "")

                for st in card.get("showtimes", []):
                    sa = st.get("additionalData", {})
                    date_code = str(
                        sa.get("showDateCode", "")
                        or sa.get("dateCode", "")
                    ).strip()
                    if not date_code and re.match(
                            r"^\d{8}", sa.get("cutOffDateTime", "")):
                        date_code = sa["cutOffDateTime"][:8]

                    show = ShowInfo(
                        venue_code=vcode,
                        venue_name=vname,
                        session_id=sa.get("sessionId", ""),
                        date_code=date_code,
                        time=st.get("title", ""),
                        time_code=sa.get("showTimeCode", ""),
                        screen_attr=(st.get("screenAttr", "")
                                     or sa.get("attributes", "")),
                        format=format_name,
                        movie=movie_name,
                    )
                    for cat in sa.get("categories", []):
                        ca = str(cat.get("availStatus", ""))
                        lbl, _ = AVAIL_STATUS_MAP.get(ca, ("UNKNOWN", ""))
                        show.categories.append(CatInfo(
                            name=cat.get("priceDesc", ""),
                            price=cat.get("curPrice", "0"),
                            status=ca,
                        ))
                    shows.append(show)
    return shows


# ──────────────────────────────────────────────────────────────────────
# FILTERING
# ──────────────────────────────────────────────────────────────────────
def filter_shows(shows, theatre_filter, time_periods, date_codes):
    result = []
    kws = [k.strip().lower() for k in theatre_filter.split(",")
           if k.strip()] if theatre_filter else []
    periods = [p.strip().lower() for p in time_periods.split(",")
               if p.strip()] if time_periods else []
    dates_set = set(d.strip() for d in date_codes.split(",")
                    if d.strip()) if date_codes else set()

    for s in shows:
        # Theatre filter
        if kws:
            name_lower = s.venue_name.lower()
            if not any(k in name_lower for k in kws):
                continue

        # Date filter
        if dates_set and s.date_code and s.date_code not in dates_set:
            continue

        # Time period filter
        if periods:
            try:
                tc = int(s.time_code)
            except ValueError:
                tc = 0
            matched = False
            for p in periods:
                if p in TIME_PERIODS:
                    lo, hi = TIME_PERIODS[p]
                    if lo <= tc < hi:
                        matched = True
                        break
            if not matched:
                continue

        result.append(s)
    return result


# ──────────────────────────────────────────────────────────────────────
# STATE (for change detection between runs)
# ──────────────────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def build_state(shows, dates):
    """Build a comparable state dict."""
    show_state = {}
    for s in shows:
        for c in s.categories:
            key = f"{s.movie}|{s.venue_code}|{s.session_id}|{s.date_code}|{s.format}|{c.name}"
            show_state[key] = {
                "movie": s.movie,
                "venue": s.venue_name,
                "time": s.time,
                "date": s.date_code,
                "format": s.format,
                "cat": c.name,
                "price": c.price,
                "status": c.status,
            }

    date_state = {
        d.date_code: d.status for d in dates
    }

    return {"shows": show_state, "dates": date_state}


def detect_changes(old_state, new_state):
    changes = []

    # New dates opening
    old_dates = old_state.get("dates", {})
    new_dates = new_state.get("dates", {})
    for dc, status in new_dates.items():
        old_status = old_dates.get(dc)
        if (old_status == "NOT_OPEN"
                and status in ("BOOKABLE", "AVAILABLE")):
            changes.append(f"📅 NEW DATE OPENED: {dc}")

    old_shows = old_state.get("shows", {})
    new_shows = new_state.get("shows", {})

    # New showtimes
    for key in set(new_shows) - set(old_shows):
        s = new_shows[key]
        fmt_str = f" [{s.get('format')}]" if s.get("format") else ""
        movie_tag = f"[{s.get('movie')}] " if s.get("movie") else ""
        changes.append(
            f"🆕 NEW: {movie_tag}{s['venue']} {s['time']}{fmt_str} [{s['date']}] "
            f"— {s['cat']} ₹{s['price']}"
        )

    # Sold out → available
    for key, new_s in new_shows.items():
        old_s = old_shows.get(key)
        if old_s and old_s["status"] == "0" and new_s["status"] != "0":
            lbl, ico = AVAIL_STATUS_MAP.get(
                new_s["status"], ("UNKNOWN", "⚪")
            )
            fmt_str = f" [{new_s.get('format')}]" if new_s.get("format") else ""
            movie_tag = f"[{new_s.get('movie')}] " if new_s.get("movie") else ""
            changes.append(
                f"{ico} BACK: {movie_tag}{new_s['venue']} {new_s['time']}{fmt_str} "
                f"[{new_s['date']}] — {new_s['cat']} → {lbl}"
            )

    return changes


# ──────────────────────────────────────────────────────────────────────
# EMAIL NOTIFICATION (Resend)
# ──────────────────────────────────────────────────────────────────────
def _cat_status_label(status):
    return AVAIL_STATUS_MAP.get(status, ("UNKNOWN", ""))[0]


def is_priority_alert(text):
    t = text.lower()
    return "imax" in t or "dolby" in t


def trigger_priority_alert(booking_urls, priority_items):
    """Emergency high-priority alert when IMAX or Dolby Cinema tickets drop."""
    if isinstance(booking_urls, str):
        booking_urls = [booking_urls]

    # 1. Instantly pop up the browser directly to BookMyShow
    for url in booking_urls:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    # 2. Sound loud repeating siren in background thread
    def siren():
        try:
            import winsound
            for _ in range(12):
                winsound.Beep(1800, 200)
                winsound.Beep(2400, 200)
        except Exception:
            for _ in range(12):
                print("\a", end="", flush=True)
                time.sleep(0.2)
    threading.Thread(target=siren, daemon=True).start()

    # 3. Popup Windows system-modal dialog on top of all windows
    def popup():
        try:
            import ctypes
            summary = "\n".join(priority_items[:6])
            ctypes.windll.user32.MessageBoxW(
                0,
                f"🔥 IMAX / DOLBY CINEMA TICKETS JUST OPENED IN HYDERABAD! 🔥\n\n{summary}\n\nYour browser has opened directly to the booking page.\nBook now before seats sell out!",
                "🚨 BMS EMERGENCY ALERT: IMAX / DOLBY LIVE",
                0x1000 | 0x30  # MB_SYSTEMMODAL | MB_ICONEXCLAMATION
            )
        except Exception:
            pass
    threading.Thread(target=popup, daemon=True).start()


def send_email(subject, changes, shows, movie_urls):
    api_key = RESEND_API_KEY.strip()
    to = RESEND_TO_EMAIL.strip()
    frm = RESEND_FROM_EMAIL.strip() or "onboarding@resend.dev"

    if not api_key or not to:
        print("  ⚠️  Skipping email — RESEND_API_KEY or RESEND_TO_EMAIL not set.")
        return

    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p")

    # Build changes HTML
    changes_html = ""
    if changes:
        rows = "".join(
            f'<li style="padding:4px 0;font-size:14px;">{escape(c)}</li>'
            for c in changes
        )
        changes_html = f"""
        <h3 style="margin:0 0 8px 0;font-size:16px;font-weight:bold;color:#111;">
            🚨 Changes Detected:
        </h3>
        <ul style="margin:0 0 20px 0;padding-left:20px;line-height:1.6;color:#333;">
            {rows}
        </ul>"""

    # Build booking buttons
    buttons_html = ""
    if movie_urls:
        btn_items = ""
        for name, url in movie_urls.items():
            btn_items += (
                f'<a href="{escape(url)}" '
                f'style="background-color:#e50914;color:#ffffff;padding:12px 20px;'
                f'text-decoration:none;font-weight:bold;font-size:14px;border-radius:6px;'
                f'display:inline-block;margin:4px 8px 4px 0;">'
                f'👉 BOOK {escape(name.upper())}</a> '
            )
        buttons_html = f'<div style="margin:16px 0;">{btn_items}</div>'

    # Build shows section grouped by movie then venue
    movie_groups = {}
    for s in shows:
        movie_groups.setdefault(s.movie or "Showtimes", {}).setdefault(s.venue_name, []).append(s)

    shows_html = ""
    for mname, venue_groups in movie_groups.items():
        shows_html += f'<h3 style="margin:22px 0 8px 0;font-size:17px;color:#111;border-bottom:2px solid #e50914;padding-bottom:4px;">🎬 {escape(mname)}</h3>'
        for vname, vshows in venue_groups.items():
            show_rows = ""
            for s in vshows:
                cats = " | ".join(
                    f"{escape(c.name)} Rs.{escape(c.price)} ({_cat_status_label(c.status)})"
                    for c in s.categories
                )
                fmt_info = f" [{escape(s.format)}]" if s.format else ""
                scr_info = f" [{escape(s.screen_attr)}]" if s.screen_attr else ""
                show_rows += (
                    f'<tr>'
                    f'<td style="padding:6px 8px;border-bottom:1px solid #ddd;font-size:13px;vertical-align:top;">'
                    f'{escape(s.time)}{fmt_info}{scr_info}</td>'
                    f'<td style="padding:6px 8px;border-bottom:1px solid #ddd;font-size:13px;vertical-align:top;">'
                    f'{cats}</td>'
                    f'</tr>'
                )

            shows_html += f"""
            <p style="margin:14px 0 4px 0;font-size:14px;font-weight:bold;color:#333;">
                {escape(vname)}
            </p>
            <table style="width:100%;border-collapse:collapse;font-size:13px;">
                <tr style="background:#f5f5f5;">
                    <th style="padding:6px 8px;text-align:left;border-bottom:1px solid #ddd;font-weight:bold;">Time &amp; Screen</th>
                    <th style="padding:6px 8px;text-align:left;border-bottom:1px solid #ddd;font-weight:bold;">Categories &amp; Price</th>
                </tr>
                {show_rows}
            </table>"""

    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;font-family:Arial,Helvetica,sans-serif;
             font-size:14px;color:#333;background:#fff;">
    <h2 style="margin:0 0 4px 0;font-size:20px;color:#111;">
        {escape(subject)}
    </h2>
    <p style="margin:0 0 20px 0;font-size:13px;color:#666;">
        {escape(now_str)}
    </p>
    {buttons_html}
    <hr style="border:none;border-top:1px solid #ddd;margin:20px 0;">
    {changes_html}
    <h3 style="margin:0 0 8px 0;font-size:16px;font-weight:bold;color:#333;">
        Current Showtimes
    </h3>
    {shows_html}
    <p style="margin:24px 0 0 0;font-size:12px;color:#999;">
        This is an automated alert from BMS Ticket Notifier.
    </p>
</body>
</html>"""

    # Build plain-text version with full show details
    plain_lines = [subject, "", f"Checked at: {now_str}", ""]
    if changes:
        plain_lines.append("Changes Detected:")
        plain_lines.extend(f"  - {c}" for c in changes)
        plain_lines.append("")
    plain_lines.append("Current Showtimes:")
    for mname, venue_groups in movie_groups.items():
        plain_lines.append(f"\n🎬 {mname}")
        for vname, vshows in venue_groups.items():
            plain_lines.append(f"  {vname}:")
            for s in vshows:
                cats = " | ".join(
                    f"{c.name} Rs.{c.price} ({_cat_status_label(c.status)})"
                    for c in s.categories
                )
                fmt_info = f" [{s.format}]" if s.format else ""
                scr_info = f" [{s.screen_attr}]" if s.screen_attr else ""
                plain_lines.append(f"    {s.time}{fmt_info}{scr_info} - {cats}")
    plain_lines.extend(["", "This is an automated alert from BMS Ticket Notifier."])
    plain = "\n".join(plain_lines)

    try:
        resp = HTTP_SESSION.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": frm, "to": [to],
                "subject": subject,
                "text": plain, "html": html,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f"  ✅ Email sent to {to}")
        else:
            print(f"  ❌ Resend {resp.status_code}: {resp.text}")
    except requests.RequestException as e:
        print(f"  ❌ Email failed: {e}")


# ──────────────────────────────────────────────────────────────────────
# MAIN & WATCHER
# ──────────────────────────────────────────────────────────────────────
def sound_alert():
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1200, 250)
            time.sleep(0.1)
    except Exception:
        print("\a", end="", flush=True)


def run_check():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] BMS Ticket Checker")

    raw_urls = CONFIG["url"].strip()
    if raw_urls:
        url_list = [u.strip() for u in raw_urls.split(",") if u.strip()]
    else:
        url_list = [
            "https://in.bookmyshow.com/movies/hyderabad/dune-part-three/buytickets/ET00526390",
            "https://in.bookmyshow.com/movies/hyderabad/avengers-doomsday/buytickets/ET00439706",
        ]

    raw_dates = CONFIG["dates"].strip()
    config_dates = [d.strip() for d in raw_dates.split(",") if d.strip()] if raw_dates else []

    all_shows = []
    all_dates = []
    monitored_movies = {}

    for url in url_list:
        parsed = parse_bms_url(url)
        event_code = parsed["event_code"]
        region_slug = parsed["region_slug"]
        url_date = parsed.get("date_code", "")

        if not event_code or not region_slug:
            print(f"  ❌ Invalid BMS_URL: {url}")
            continue

        region_code, region_slug_r, lat, lon, geohash = resolve_region(region_slug)

        # Initial call to discover movie info and available formats
        initial_data = fetch_bms(event_code, url_date or (config_dates[0] if config_dates else ""),
                                 region_code, region_slug_r, lat, lon, geohash)
        if not initial_data:
            print(f"  ⚠️  Could not fetch initial data for {event_code}")
            continue

        movie_info = parse_movie_info(initial_data)
        movie_name = movie_info["name"]
        monitored_movies[movie_name] = url

        discovered_formats = extract_formats(initial_data)

        # Determine which formats to query
        format_filter = [f.strip().lower() for f in CONFIG.get("format", "").split(",") if f.strip()]
        formats_to_query = {}
        if discovered_formats:
            for fmt_name, ev_code in discovered_formats.items():
                if not format_filter or any(ff in fmt_name.lower() for ff in format_filter):
                    formats_to_query[fmt_name] = ev_code
        else:
            formats_to_query["Default"] = event_code

        # Determine dates for this movie
        parsed_dates = parse_dates(initial_data)
        all_dates.extend(parsed_dates)
        if config_dates:
            movie_dates = config_dates
        elif parsed_dates:
            movie_dates = [d.date_code for d in parsed_dates if d.status != "NOT_OPEN"]
        else:
            movie_dates = [url_date or ""]

        print(f"\n  🎬 Movie: {movie_name} ({movie_info['language']})")
        print(f"     Formats: {', '.join(formats_to_query.keys())}")
        print(f"     Dates: {movie_dates}")

        # Fetch shows across each format and date for this movie
        for fmt_name, ev_code in formats_to_query.items():
            for dc in movie_dates:
                data = fetch_bms(ev_code, dc, region_code, region_slug_r, lat, lon, geohash)
                if not data:
                    continue
                all_shows.extend(parse_shows(data, format_name=fmt_name, movie_name=movie_name))

    if not all_shows:
        print("  ❌ No showtimes found.")
        return

    # Apply filters
    filtered = filter_shows(
        all_shows,
        CONFIG["theatre"],
        CONFIG["time_period"],
        CONFIG["dates"],
    )
    print(f"\n  📊 Total: {len(filtered)} showtime(s) matching filters across {len(monitored_movies)} movie(s)")

    # Build state & detect changes
    new_state = build_state(filtered, all_dates)
    old_state = load_state()

    changes = []
    if old_state:
        changes = detect_changes(old_state, new_state)

    save_state(new_state)

    if changes:
        priority_changes = [c for c in changes if is_priority_alert(c)]

        if priority_changes:
            print("\n" + "█" * 68)
            print("🔥🚨🔥 EMERGENCY ALERT: NEW IMAX / DOLBY TICKETS DETECTED! 🔥🚨🔥")
            print("   Launching browser & sounding high-priority alarm...")
            print("█" * 68)
            for c in priority_changes:
                print(f"     👉 {c}")
            target_urls = [
                murl for mname, murl in monitored_movies.items()
                if any(mname.lower() in c.lower() for c in priority_changes)
            ]
            if not target_urls:
                target_urls = list(monitored_movies.values())
            trigger_priority_alert(target_urls, priority_changes)
            email_subj = f"🔥🚨 [IMAX / DOLBY LIVE] {', '.join(monitored_movies.keys())} Hyderabad - BOOK NOW!"
        else:
            print(f"\n  🚨 {len(changes)} CHANGE(S) DETECTED:")
            for c in changes:
                print(f"     {c}")
            sound_alert()
            email_subj = f"BMS Alert: {', '.join(monitored_movies.keys())} - {len(changes)} change(s)"

        send_email(email_subj, changes, filtered, monitored_movies)
    else:
        print("  ✅ No changes since last check.")

    # Group and print current status
    print(f"\n  Current show status ({len(filtered)} shows):")
    grouped_by_movie_date = {}
    for s in filtered:
        grouped_by_movie_date.setdefault(s.movie, {}).setdefault(s.date_code, []).append(s)

    for mname, date_groups in grouped_by_movie_date.items():
        print(f"\n  🎬 MOVIE: {mname}")
        for dc in sorted(date_groups.keys()):
            print(f"    📅 Date: {dc}")
            for s in date_groups[dc]:
                cats = ", ".join(
                    f"{c.name}=₹{c.price}({AVAIL_STATUS_MAP.get(c.status, ('?',''))[0]})"
                    for c in s.categories
                )
                fmt_tag = f"[{s.format}] " if s.format else ""
                scr_attr = f"|{s.screen_attr}" if s.screen_attr else ""
                highlight = ""
                full_text = (s.format + s.screen_attr + s.venue_name).lower()
                if "imax" in full_text and "dolby" in full_text:
                    highlight = " ⭐✨ [IMAX+DOLBY]"
                elif "imax" in full_text:
                    highlight = " ⭐ [IMAX]"
                elif "dolby" in full_text:
                    highlight = " ✨ [DOLBY]"
                print(f"      {fmt_tag}{s.venue_name} — {s.time}{scr_attr}{highlight} — {cats}")


def run_server(port, interval=60):
    import http.server
    import socketserver

    server_status = {
        "status": "initializing",
        "last_check": None,
        "total_checks": 0,
        "last_error": None,
    }

    def background_worker():
        time.sleep(1)
        while True:
            try:
                print(f"\n[Worker] Running scheduled check at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}...")
                run_check()
                server_status["status"] = "healthy"
                server_status["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                server_status["total_checks"] += 1
                server_status["last_error"] = None
            except Exception as e:
                print(f"[Worker] Error during check: {e}")
                server_status["last_error"] = str(e)
            time.sleep(interval)

    worker_thread = threading.Thread(target=background_worker, daemon=True)
    worker_thread.start()

    class HealthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/health", "/status"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                resp = {
                    "service": "bms-ticket-notifier",
                    "status": server_status["status"],
                    "last_check": server_status["last_check"],
                    "total_checks": server_status["total_checks"],
                    "last_error": server_status["last_error"],
                    "interval_seconds": interval,
                    "movies": ["Dune: Part Three", "Avengers: Doomsday"],
                    "city": "Hyderabad",
                    "priority_formats": ["IMAX", "Dolby Cinema", "Dolby Atmos"],
                }
                self.wfile.write(json.dumps(resp, indent=2).encode("utf-8"))
            elif self.path == "/check":
                try:
                    run_check()
                    server_status["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    server_status["total_checks"] += 1
                    msg = "Check triggered and completed successfully."
                    code = 200
                except Exception as ex:
                    msg = f"Check failed: {ex}"
                    code = 500
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"message": msg}).encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            sys.stdout.write(f"[HTTP] {self.address_string()} - {format % args}\n")

    print("\n" + "=" * 68)
    print(f"🚀 BMS TICKET NOTIFIER WEB SERVICE RUNNING ON PORT {port}")
    print(f"   Health check: http://0.0.0.0:{port}/health")
    print(f"   Background check interval: {interval} seconds")
    print("=" * 68 + "\n")

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", port), HealthHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server.")


def main():
    port_env = os.getenv("PORT")
    if port_env or "--server" in sys.argv:
        port = int(port_env) if port_env else 10000
        server_interval = int(os.getenv("CHECK_INTERVAL", "60"))
        run_server(port, server_interval)
        return

    watch_mode = "--watch" in sys.argv or "-w" in sys.argv or os.getenv("BMS_WATCH") == "1"
    fast_mode = "--fast" in sys.argv or "-f" in sys.argv

    interval = 30 if fast_mode else 60  # default 60s, or 30s in fast mode
    for arg in sys.argv:
        if arg.startswith("--interval="):
            val = arg.split("=")[1].strip().lower()
            try:
                if val.endswith("s"):
                    interval = int(val[:-1])
                elif val.endswith("m"):
                    interval = int(val[:-1]) * 60
                else:
                    interval = int(val)
            except ValueError:
                pass

    if watch_mode:
        print("=" * 68)
        print("👀 BMS TICKET WATCHER — LIVE MONITORING MODE")
        print(f"   Polling frequency: Every {interval} second(s).")
        print("   🎯 Monitoring movies: Dune: Part Three & Avengers: Doomsday")
        print("   🔥 IMAX & DOLBY Auto-Action: Siren + opens booking browser!")
        print("   Press Ctrl+C at any time to stop.")
        print("=" * 68 + "\n")
        while True:
            try:
                run_check()
                print(f"\n⏳ Next check in {interval}s... (Press Ctrl+C to stop)\n")
                time.sleep(interval)
            except KeyboardInterrupt:
                print("\n👋 BMS Ticket Watcher stopped.")
                break
    else:
        run_check()
        print("\n" + "=" * 68)
        print("💡 TIP: For fastest IMAX & Dolby Cinema booking, run live watch mode:")
        print("   uv run main.py --watch --fast")
        print("   (Checks every 30 seconds & auto-launches your browser on drop)")
        print("=" * 68)


if __name__ == "__main__":
    main()
