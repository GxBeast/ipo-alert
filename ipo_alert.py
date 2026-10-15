#!/usr/bin/env python3
"""
IPO Alert Bot for Indian Stock Markets.
Scrapes live GMP and Subscription data from investorgain.com and sends Telegram alerts.

Alert Rules:
  - Scans daily at 2:00 PM IST.
  - Filters strictly for open IPOs with GMP >= 20%.
  - Sends Telegram alerts ONLY if at least one IPO meets the threshold.
  - Formats:
      IPO Name (Mainboard / SME)
      GMP
      QIB Sub
      HNI Sub
      Retail Sub

Environment Variables:
  - TG_TOKEN: Telegram bot token (required)
  - TG_CHAT_ID: Telegram chat ID (required)
  - GMP_MIN_PCT: Minimum GMP percentage required to alert (default: 20)
"""

import os
import sys
import re
import time
import traceback
import argparse
from datetime import datetime, date, timezone, timedelta
from typing import List, Dict, Optional, Tuple

# Ensure stdout and stderr support UTF-8 (emojis, rupee signs) across all platforms
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import requests
from playwright.sync_api import sync_playwright

# Indian Standard Time (UTC + 5:30)
IST = timezone(timedelta(hours=5, minutes=30))

# Target URLs
URL_GMP = "https://www.investorgain.com/report/live-ipo-gmp/331/"
URL_SUB_ALL = "https://www.investorgain.com/report/ipo-subscription-live/333/"


# ==========================================
# 1. HELPERS & NORMALIZATION
# ==========================================

def get_ist_now() -> datetime:
    """Returns the current datetime in Indian Standard Time."""
    return datetime.now(IST)


def normalize_name(name: str) -> str:
    """
    Normalizes company names across different pages for accurate matching.
    Removes common suffixes (IPO, SME, BSE, NSE, Ltd, Limited) and special characters.
    """
    n = name.lower()
    n = re.sub(r'\b(ipo|bse|nse|sme|ltd|limited|corp|corporation|india|technologies|technology|services)\b', ' ', n)
    n = re.sub(r'[^a-z0-9]', '', n)
    return n.strip()


def parse_price(price_text: str) -> float:
    """Extracts numeric price from text like '700', '1,119', '650 - 700'."""
    if not price_text or price_text.strip() in ['-', '--', 'N/A']:
        return 0.0
    parts = price_text.split('-')
    target = parts[-1].strip()
    match = re.search(r'([0-9,]+(?:\.[0-9]+)?)', target)
    if match:
        try:
            return float(match.group(1).replace(',', ''))
        except ValueError:
            pass
    return 0.0


def parse_gmp(gmp_text: str, price_val: float) -> Tuple[float, float]:
    """
    Extracts GMP in Rupees and GMP percentage.
    Examples: '₹500 (71.43%)', '₹-- (0.00%)', '₹88 (32.47%)'
    """
    gmp_rs = 0.0
    gmp_pct = 0.0

    # 1. Percentage in parentheses
    pct_match = re.search(r'\(([0-9.]+)%\)', gmp_text)
    if pct_match:
        try:
            gmp_pct = float(pct_match.group(1))
        except ValueError:
            pass

    # 2. GMP in Rupees
    rs_match = re.search(r'₹\s*([0-9,]+(?:\.[0-9]+)?)', gmp_text)
    if rs_match:
        try:
            gmp_rs = float(rs_match.group(1).replace(',', ''))
        except ValueError:
            pass

    # 3. If GMP percentage is missing or 0 but GMP Rs exists, compute it
    if gmp_pct == 0.0 and price_val > 0 and gmp_rs > 0:
        gmp_pct = round((gmp_rs / price_val) * 100, 2)

    return gmp_rs, gmp_pct


def parse_date_str(dt_str: str, base_year: int) -> Optional[date]:
    """Parses date strings in formats '15-Oct', '15-10-2026', '2026-10-15'."""
    if not dt_str or dt_str.strip() in ['-', '--', 'N/A']:
        return None

    clean_str = dt_str.split('\n')[0].strip()

    try:
        return datetime.strptime(clean_str, "%Y-%m-%d").date()
    except ValueError:
        pass

    try:
        return datetime.strptime(clean_str, "%d-%m-%Y").date()
    except ValueError:
        pass

    match = re.search(r'(\d{1,2})-([A-Za-z]{3})', clean_str)
    if match:
        day = int(match.group(1))
        month_str = match.group(2)
        try:
            return datetime.strptime(f"{day}-{month_str}-{base_year}", "%d-%b-%Y").date()
        except ValueError:
            pass

    return None


def clean_header_name(header: str) -> str:
    """Strips sort arrows (▲, ▼) and punctuation for flexible column lookup."""
    h = re.sub(r'[▲▼\(\)₹]', ' ', header)
    return re.sub(r'\s+', ' ', h).strip().upper()


# ==========================================
# 2. TELEGRAM MESSAGING
# ==========================================

def send_telegram_message(text: str) -> bool:
    """
    Sends an HTML-formatted message to Telegram using env vars TG_TOKEN and TG_CHAT_ID.
    Never logs or prints the token or chat ID.
    """
    token = os.environ.get("TG_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("[ERROR] Telegram credentials (TG_TOKEN, TG_CHAT_ID) are missing from environment.", file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]
    success = True

    for chunk in chunks:
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code != 200:
                print(f"[ERROR] Telegram API error {resp.status_code}: {resp.text}", file=sys.stderr)
                success = False
        except Exception as ex:
            print(f"[ERROR] Failed to send Telegram message: {ex}", file=sys.stderr)
            success = False

    return success


def send_telegram_error(err_msg: str):
    """Sends an automated failure alert to Telegram so failures are never silent."""
    now_ist = get_ist_now().strftime("%d-%b-%Y %I:%M %p IST")
    body = (
        f"🚨 <b>IPO Alert Bot Failed</b>\n"
        f"<i>Time: {now_ist}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Error Details:</b>\n"
        f"<code>{err_msg[:2500]}</code>\n\n"
        f"⚠️ <i>Please check GitHub Actions run logs.</i>"
    )
    send_telegram_message(body)


# ==========================================
# 3. SCRAPING WITH PLAYWRIGHT (PRIMARY)
# ==========================================

def scrape_page_playwright(url: str, max_retries: int = 3) -> List[Dict[str, str]]:
    """
    Loads page using Playwright with domcontentloaded and wait_for_function.
    Blocks media and tracker requests to prevent background timeouts.
    Retries up to max_retries times.
    """
    last_exception = None

    for attempt in range(1, max_retries + 1):
        try:
            print(f"[Playwright] Loading {url} (Attempt {attempt}/{max_retries})...")
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
                page = context.new_page()

                # Block trackers, ads, images, and fonts for high speed and reliability
                def route_filter(route):
                    req = route.request
                    res_type = req.resource_type
                    req_url = req.url.lower()
                    if res_type in ["image", "media", "font"]:
                        route.abort()
                    elif any(ad in req_url for ad in ["google-analytics", "doubleclick", "googletagmanager", "adservice", "amazon-adsystem"]):
                        route.abort()
                    else:
                        route.continue_()

                page.route("**/*", route_filter)

                # 1. Wait for DOMContentLoaded
                page.goto(url, wait_until="domcontentloaded", timeout=45000)

                # 2. Wait until table has rows with cells > 4
                page.wait_for_function(
                    """() => {
                        const rows = document.querySelectorAll('#reportTable tbody tr');
                        if (rows.length === 0) return false;
                        const firstRow = rows[0];
                        return firstRow.querySelectorAll('td').length > 4;
                    }""",
                    timeout=25000
                )

                # 3. Extract Headers
                headers = []
                th_elements = page.query_selector_all("#reportTable thead th")
                if not th_elements:
                    th_elements = page.query_selector_all("#reportTable tr th")
                for th in th_elements:
                    headers.append(clean_header_name(th.inner_text()))

                # 4. Extract Rows
                rows = page.query_selector_all("#reportTable tbody tr")
                parsed_rows = []

                for r in rows:
                    cells = r.query_selector_all("td")
                    if not cells:
                        continue

                    # Company name from <a> tag
                    a_tag = r.query_selector("td:nth-child(1) a")
                    name = a_tag.inner_text().strip() if a_tag else cells[0].inner_text().strip()

                    # Badges
                    badges = [b.inner_text().strip() for b in r.query_selector_all("td:nth-child(1) span.badge")]
                    status_badge = badges[-1] if badges else ""
                    cat_badge = badges[0] if badges else "Mainboard"

                    row_data = {
                        "_name": name,
                        "_status_badge": status_badge,
                        "_cat_badge": cat_badge,
                    }

                    for idx, cell in enumerate(cells):
                        col_key = headers[idx] if idx < len(headers) else f"COL_{idx}"
                        row_data[col_key] = cell.inner_text().strip()

                    parsed_rows.append(row_data)

                browser.close()
                print(f"[Playwright] Successfully extracted {len(parsed_rows)} rows from {url}")
                return parsed_rows

        except Exception as e:
            last_exception = e
            print(f"[Playwright] Attempt {attempt} failed for {url}: {e}", file=sys.stderr)
            if attempt < max_retries:
                time.sleep(attempt * 3)

    raise RuntimeError(f"Playwright failed to scrape {url} after {max_retries} attempts. Last error: {last_exception}")


# ==========================================
# 4. BACKEND JSON API FALLBACK
# ==========================================

def fetch_json_api_fallback(report_id: int, filter_type: str = "all") -> List[Dict[str, str]]:
    """Fallback method querying InvestorGain's internal JSON API directly."""
    now = get_ist_now()
    year = now.year
    fiscal_year = f"{year}-{str(year + 1)[-2:]}"

    api_url = f"https://webnodejs.investorgain.com/cloud/v2/report/data-read/{report_id}/1/10/{year}/{fiscal_year}/0/{filter_type}?search=&v=11-18"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.investorgain.com/",
        "Origin": "https://www.investorgain.com"
    }

    print(f"[Fallback API] Querying {api_url}...")
    resp = requests.get(api_url, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    table_data = data.get("reportTableData", [])
    parsed_rows = []

    for item in table_data:
        name = item.get("~ipo_name")
        if not name and "Name" in item:
            name = re.sub(r'<[^>]+>', ' ', item["Name"])
            name = re.sub(r'\s+', ' ', name).strip()

        row = {
            "_name": name or "Unknown IPO",
            "_status_badge": item.get("~ipo_status1", ""),
            "_cat_badge": item.get("~IPO_Category", "Mainboard"),
            "GMP": item.get("GMP", ""),
            "PRICE": item.get("Price (₹)", item.get("IPO Price", "0")),
            "OPEN": item.get("~Srt_Open", item.get("Open", "")),
            "CLOSE": item.get("~Srt_Close", item.get("Close", item.get("Closing Date", ""))),
            "QIB": item.get("QIB", "0"),
            "NII": item.get("NII", "0"),
            "RII": item.get("RII", "0"),
            "TOTAL": item.get("Total", item.get("Sub", "-")),
            "LOT": item.get("Lot", "-")
        }
        parsed_rows.append(row)

    print(f"[Fallback API] Successfully extracted {len(parsed_rows)} rows via JSON API.")
    return parsed_rows


def fetch_report(url: str, report_id: int, filter_type: str = "all") -> List[Dict[str, str]]:
    """Tries Playwright first; falls back to direct JSON API if Playwright fails."""
    try:
        return scrape_page_playwright(url)
    except Exception as e:
        print(f"[WARN] Playwright scraping failed ({e}). Falling back to JSON API...", file=sys.stderr)
        return fetch_json_api_fallback(report_id, filter_type)


# ==========================================
# 5. DATA INGESTION & SUBSCRIPTION MATCHING
# ==========================================

def get_subscription_map() -> Dict[str, Dict]:
    """
    Fetches the live subscription table and returns a lookup dictionary
    containing QIB, HNI (NII), and Retail (RII) subscription multiples.
    """
    sub_rows = fetch_report(URL_SUB_ALL, report_id=333, filter_type="all")

    sub_map = {}
    for r in sub_rows:
        raw_name = r.get("_name", "")
        clean_name = re.sub(r'(IPO|BSE|NSE|SME|[OUC]|CT)+$', '', raw_name).strip()
        norm = normalize_name(clean_name)

        qib_val = 0.0
        hni_val = 0.0
        retail_val = 0.0

        for k, v in r.items():
            k_clean = k.strip()
            # QIB
            if "QIB" in k_clean:
                try:
                    qib_val = float(str(v).replace(',', ''))
                except ValueError:
                    pass
            # HNI is reported under NII column
            elif "NII" in k_clean:
                try:
                    hni_val = float(str(v).replace(',', ''))
                except ValueError:
                    pass
            # Retail is reported under RII column
            elif "RII" in k_clean:
                try:
                    retail_val = float(str(v).replace(',', ''))
                except ValueError:
                    pass

        sub_map[norm] = {
            "name": clean_name,
            "qib": qib_val,
            "hni": hni_val,
            "retail": retail_val
        }

    return sub_map


def get_all_ipos() -> List[Dict]:
    """
    Fetches live GMP data and correlates with QIB, HNI, and Retail subscription.
    """
    today_ist = get_ist_now().date()
    current_year = today_ist.year

    gmp_rows = fetch_report(URL_GMP, report_id=331, filter_type="all")
    sub_map = get_subscription_map()

    ipos = []
    for r in gmp_rows:
        raw_name = r.get("_name", "")
        clean_name = re.sub(r'(IPO|BSE|NSE|SME|[OUC]|CT)+$', '', raw_name).strip()
        norm = normalize_name(clean_name)

        # Category: Mainboard or SME
        cat_badge = r.get("_cat_badge", "Mainboard")
        if "SME" in r.get("NAME", "") or "SME" in raw_name or "SME" in cat_badge:
            category = "SME"
        else:
            category = "Mainboard"

        # Price
        price_text = ""
        for k, v in r.items():
            if "PRICE" in k:
                price_text = v
                break
        price_val = parse_price(price_text)

        # GMP
        gmp_text = ""
        for k, v in r.items():
            if "GMP" in k:
                gmp_text = v
                break
        gmp_rs, gmp_pct = parse_gmp(gmp_text, price_val)

        # Open / Close Dates
        open_text = ""
        close_text = ""
        for k, v in r.items():
            if k == "OPEN":
                open_text = v
            elif k == "CLOSE":
                close_text = v

        open_dt = parse_date_str(open_text, current_year)
        close_dt = parse_date_str(close_text, current_year)

        status_badge = r.get("_status_badge", "")
        is_closing_today = (status_badge == "CT") or (close_dt == today_ist)
        is_open = (status_badge in ["O", "CT"]) or (open_dt and close_dt and open_dt <= today_ist <= close_dt)

        if close_dt:
            days_left = (close_dt - today_ist).days
        else:
            days_left = 0 if is_closing_today else -1

        # Match Subscription Data
        sub_info = sub_map.get(norm)
        qib_val = sub_info["qib"] if sub_info else 0.0
        hni_val = sub_info["hni"] if sub_info else 0.0
        retail_val = sub_info["retail"] if sub_info else 0.0

        ipos.append({
            "name": clean_name,
            "category": category,
            "price": price_val,
            "gmp_rs": gmp_rs,
            "gmp_pct": gmp_pct,
            "open_date": open_dt,
            "close_date": close_dt,
            "close_str": close_text.split('\n')[0].strip(),
            "status_badge": status_badge,
            "is_open": is_open,
            "is_closing_today": is_closing_today,
            "days_left": days_left,
            "qib_x": qib_val,
            "hni_x": hni_val,
            "retail_x": retail_val
        })

    return ipos


# ==========================================
# 6. MESSAGE FORMATTING (CUSTOM LAYOUT)
# ==========================================

def build_gmp_alert_message(qualifying_ipos: List[Dict], gmp_min: float = 20.0) -> str:
    """
    Formats the message strictly according to requested format:
      ipo name(mainbord/sme)
      gmp
      qib sub
      hni sub
      retail sub
    """
    now_ist = get_ist_now().strftime("%d-%b-%Y, %I:%M %p IST")
    lines = [
        f"🚨 <b>IPO LAST DAY ALERT (GMP ≥ {gmp_min:.0f}%)</b>",
        f"<i>Closing Today: {now_ist}</i>",
        "━━━━━━━━━━━━━━━━━━━━━━"
    ]

    for ipo in qualifying_ipos:
        cat_label = "mainboard" if ipo["category"] == "Mainboard" else "sme"

        lines.append(f"<b>{ipo['name']} ({cat_label})</b>")
        lines.append(f"• <b>GMP:</b> ₹{ipo['gmp_rs']:.0f} (<b>{ipo['gmp_pct']:+.1f}%</b>)")
        lines.append(f"• <b>QIB Sub:</b> {ipo['qib_x']:.2f}x")
        lines.append(f"• <b>HNI Sub:</b> {ipo['hni_x']:.2f}x")
        lines.append(f"• <b>Retail Sub:</b> {ipo['retail_x']:.2f}x")
        lines.append("──────────────────────")

    return "\n".join(lines)


# ==========================================
# 7. MAIN RUNNER
# ==========================================

def main():
    parser = argparse.ArgumentParser(description="IPO Alert Bot")
    parser.add_argument("--dry-run", action="store_true", help="Print message to console instead of sending to Telegram")
    args = parser.parse_args()

    gmp_min = float(os.environ.get("GMP_MIN_PCT", "20"))

    print("==================================================")
    print("IPO Alert Bot Started")
    print(f"Time: {get_ist_now().strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"Threshold: Only GMP >= {gmp_min}%")
    print("==================================================")

    try:
        all_ipos = get_all_ipos()
        print(f"Total IPO records fetched: {len(all_ipos)}")

        # Filter strictly for IPOs CLOSING TODAY (Last Date) with GMP >= 20%
        qualifying_ipos = [
            ipo for ipo in all_ipos
            if ipo["is_closing_today"] and ipo["gmp_pct"] >= gmp_min
        ]

        print(f"IPOs closing today with GMP >= {gmp_min}%: {len(qualifying_ipos)}")

        # If no IPO closes today with GMP >= 20%, do not send any message
        if not qualifying_ipos:
            print(f"[INFO] No IPO closing today meets the {gmp_min}% GMP threshold. Skipping Telegram notification.")
            return

        message = build_gmp_alert_message(qualifying_ipos, gmp_min=gmp_min)

        if args.dry_run:
            print("\n--- GENERATED TELEGRAM MESSAGE (DRY RUN) ---")
            print(message)
            print("-------------------------------------------\n")
        else:
            print("[Telegram] Sending alert for qualifying IPOs...")
            success = send_telegram_message(message)
            if success:
                print("[Telegram] Alert sent successfully!")
            else:
                print("[Telegram] Failed to send alert.", file=sys.stderr)
                sys.exit(1)

    except Exception as e:
        error_details = traceback.format_exc()
        print(f"[FATAL ERROR] {error_details}", file=sys.stderr)
        if not args.dry_run:
            send_telegram_error(f"{e}\n\n{error_details}")
        sys.exit(1)


if __name__ == "__main__":
    main()
