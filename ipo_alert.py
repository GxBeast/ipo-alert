"""IPO alert: GMP >= 15% and QIB >= 10x (judged on the last day). Sends a Telegram message.
Usage: python ipo_alert.py morning|final
"""
import os, re, sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from dateutil import parser as dp
from playwright.sync_api import sync_playwright

GMP_MIN_PCT = float(os.getenv("GMP_MIN_PCT", 15))
QIB_MIN_X = float(os.getenv("QIB_MIN_X", 10))
GMP_URL = "https://www.investorgain.com/report/live-ipo-gmp/331/"
SUB_BASE = "https://www.investorgain.com/report/ipo-subscription-live/333/"
SUB_URLS = [SUB_BASE, SUB_BASE + "closing-today/"]
TODAY = datetime.now(ZoneInfo("Asia/Kolkata")).date()
MODE = sys.argv[1] if len(sys.argv) > 1 else "morning"


def num(text, signed=False):
    m = re.search(r"-?\d[\d,]*\.?\d*" if signed else r"\d[\d,]*\.?\d*", text or "")
    return float(m.group().replace(",", "")) if m else None


def price_num(text):  # upper end of a price band
    nums = re.findall(r"\d[\d,]*\.?\d*", text or "")
    return float(nums[-1].replace(",", "")) if nums else None


def to_date(text):
    try:
        return dp.parse((text or "").split("\n")[0], dayfirst=True,
                        default=datetime(TODAY.year, 1, 1)).date()
    except Exception:
        return None


def key(name):
    n = re.sub(r"\b(ipo|sme|limited|ltd)\b", "", (name or "").split("\n")[0].lower())
    return re.sub(r"[^a-z0-9]", "", n)


def col(heads, k, avoid=""):
    for i, h in enumerate(heads):
        if k in h and not (avoid and avoid in h):
            return i
    return None


def scrape(page, url):
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_function("() => [...document.querySelectorAll('table tbody tr')].some(r => r.cells.length > 4)", timeout=60000)
        data = page.evaluate("""() => {
        const t = [...document.querySelectorAll('table')]
            .sort((a, b) => b.rows.length - a.rows.length)[0];
        return {
            heads: [...t.querySelectorAll('thead th')].map(h => h.innerText),
            rows: [...t.querySelectorAll('tbody tr')].map(r => [...r.cells].map(c => c.innerText))
        };
    }""")
    heads = [re.sub(r"[▲▼]", "", h).strip().lower() for h in data["heads"]]
    return heads, data["rows"]


def get_gmp(page):
    heads, rows = scrape(page, GMP_URL)
    i_gmp, i_price = col(heads, "gmp", "updat"), col(heads, "price")
    i_open, i_close = col(heads, "open"), col(heads, "close")
    if None in (i_gmp, i_price, i_close):
        raise RuntimeError(f"Unexpected GMP table columns: {heads}")
    out = []
    for r in rows:
        try:
            gmp, price = num(r[i_gmp], True), price_num(r[i_price])
            if gmp is None or not price:
                continue
            out.append({
                "name": r[0].split("\n")[0].strip(), "gmp": gmp, "price": price,
                "pct": gmp / price * 100,
                "open": to_date(r[i_open]) if i_open is not None else None,
                "close": to_date(r[i_close]),
            })
        except IndexError:
            continue
    return out


def get_qib(page):
    qib = {}
    for url in SUB_URLS:
        try:
            heads, rows = scrape(page, url)
            i_q = col(heads, "qib")
            if i_q is None:
                continue
            for r in rows:
                if len(r) > i_q:
                    qib[key(r[0])] = num(r[i_q])
        except Exception as e:
            print("subscription page failed:", url, e)
    return qib


def find_qib(qib, name):
    k = key(name)
    for kk, v in qib.items():
        if kk and (kk == k or kk in k or k in kk):
            return v
    return None


def when(d):
    n = (d - TODAY).days
    return "closes TODAY" if n == 0 else "closes tomorrow" if n == 1 else f"closes in {n} days"


def verdict(i):
    gmp_ok = i["pct"] >= GMP_MIN_PCT
    q = i["qib"]
    qs = "n/a" if q is None else f"{q:.1f}x"
    if not gmp_ok:
        return f"⚪ SKIP: GMP {i['pct']:.0f}% < {GMP_MIN_PCT:.0f}%"
    if q is not None and q >= QIB_MIN_X:
        return f"🟢 APPLY: GMP {i['pct']:.0f}%, QIB {qs}"
    if MODE == "final":
        return f"🔴 SKIP: GMP ok ({i['pct']:.0f}%) but QIB {qs} < {QIB_MIN_X:.0f}x"
    return f"🟡 WATCH: GMP ok ({i['pct']:.0f}%), QIB {qs} so far; QIB often jumps late. Rechecked at 3 PM"


def build(ipos):
    live = [i for i in ipos if i["close"] and i["close"] >= TODAY
            and (i["open"] is None or i["open"] <= TODAY)]
    live.sort(key=lambda i: -i["pct"])
    today = [i for i in live if i["close"] == TODAY]
    lines = []
    if MODE == "morning" and live:
        lines.append(f"📊 Open IPOs, {TODAY:%d %b}")
        for i in live:
            q = "n/a" if i["qib"] is None else f"{i['qib']:.1f}x"
            mark = "✅" if i["pct"] >= GMP_MIN_PCT else "▫️"
            lines.append(f"{mark} {i['name']}: GMP ₹{i['gmp']:g} ({i['pct']:.0f}%), QIB {q}, {when(i['close'])}")
    if today:
        lines.append("")
        lines.append("⏰ CLOSING TODAY: apply before 5 PM" if MODE == "final" else "⏰ CLOSING TODAY")
        lines += [f"{i['name']}\n   {verdict(i)}" for i in today]
    return "\n".join(lines).strip()


def send(text):
    token, chat = os.environ["TG_TOKEN"], os.environ["TG_CHAT_ID"]
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                  data={"chat_id": chat, "text": text[:4000]}, timeout=30).raise_for_status()


def main():
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
            ipos = get_gmp(page)
            qib = get_qib(page)
            browser.close()
        for i in ipos:
            i["qib"] = find_qib(qib, i["name"])
        msg = build(ipos)
        print(msg or "Nothing to report")
        if msg:
            send(msg)
    except Exception as e:
        send(f"⚠️ IPO alert script failed: {e}")
        raise


if __name__ == "__main__":
    main()
