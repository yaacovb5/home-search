"""
Home Search Bot — Cedarhurst, Woodmere, Lawrence NY
Runs via GitHub Actions 3x/day. Fetches Zillow, detects changes,
generates index.html, and sends push notifications via ntfy.sh.
"""

import requests
import json
import re
import time
import os
import sys
from datetime import datetime, timezone
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
MAX_PRICE    = 1_310_000
DOWN_CAP     = 220_000
INSURANCE    = 320          # $/mo
ARM_RATE     = 0.055        # 5.5% ARM
FIXED_RATE   = 0.06125      # 6.125% 30-yr fixed
LOAN_MONTHS  = 360

SEARCH_URLS = {
    "Cedarhurst": "https://www.zillow.com/cedarhurst-ny-11516/",
    "Woodmere":   "https://www.zillow.com/woodmere-ny-11598/",
    "Lawrence":   "https://www.zillow.com/lawrence-ny-11559/",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")   # Set in GitHub Secrets
STATE_FILE = "known_listings.json"


# ──────────────────────────────────────────────
# PAYMENT MATH
# ──────────────────────────────────────────────
def calc_pi(loan, annual_rate):
    r = annual_rate / 12
    return loan * r * (1 + r) ** LOAN_MONTHS / ((1 + r) ** LOAN_MONTHS - 1)

def calc_monthly(price, annual_tax, annual_rate):
    down = min(price * 0.20, DOWN_CAP)
    loan = price - down
    pi   = calc_pi(loan, annual_rate)
    tax  = annual_tax / 12
    return round(pi + tax + INSURANCE), round(down), round(loan), round(pi)


# ──────────────────────────────────────────────
# WEB FETCH
# ──────────────────────────────────────────────
def fetch(url, delay=3, retries=3):
    for attempt in range(retries):
        try:
            time.sleep(delay + attempt * 3)
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                return r.text
            print(f"  HTTP {r.status_code} for {url}")
        except Exception as e:
            print(f"  Error ({attempt+1}/{retries}): {e}")
    return None


# ──────────────────────────────────────────────
# ZILLOW PARSING
# ──────────────────────────────────────────────
def extract_json_blob(html):
    """Zillow embeds listing data as JSON in a <script> tag."""
    # Try the main search results JSON
    patterns = [
        r'"listResults"\s*:\s*(\[.*?\])\s*,\s*"[a-z]',
        r'"searchResults"\s*:\s*\{.*?"listResults"\s*:\s*(\[.*?\])',
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    return None

def parse_search_page(html, city):
    """Return list of listing dicts from a Zillow search-results page."""
    listings = []
    soup = BeautifulSoup(html, "lxml")

    # Try JSON blob first (faster, more reliable)
    blob = extract_json_blob(html)
    if blob and isinstance(blob, list):
        for item in blob:
            try:
                price = int(str(item.get("price", "0")).replace("$", "").replace(",", "").replace("+", ""))
                if price > MAX_PRICE or price < 100_000:
                    continue
                status = item.get("statusType", "FOR_SALE").upper()
                zpid = str(item.get("zpid", ""))
                address = item.get("address", item.get("addressStreet", ""))
                beds  = item.get("beds", 0)
                baths = item.get("baths", 0)
                sqft  = item.get("area", 0)
                listed_date = item.get("listingDateTimeOnZillow", "") or item.get("variableData", {}).get("text", "")
                detail_url = item.get("detailUrl", f"/homedetails/{zpid}_zpid/")
                if not detail_url.startswith("http"):
                    detail_url = "https://www.zillow.com" + detail_url
                listings.append({
                    "zpid":        zpid,
                    "address":     address,
                    "city":        city,
                    "price":       price,
                    "beds":        beds,
                    "baths":       baths,
                    "sqft":        sqft,
                    "status":      status,
                    "url":         detail_url,
                    "listed_date": listed_date,
                    "annual_tax":  0,   # filled in later
                })
            except Exception as e:
                print(f"  parse error on item: {e}")
        if listings:
            return listings

    # HTML fallback — look for article cards
    for card in soup.select("article[data-zpid]"):
        try:
            zpid  = card.get("data-zpid", "")
            price_el = card.select_one("[data-test='property-card-price']")
            price = 0
            if price_el:
                price = int(re.sub(r"[^0-9]", "", price_el.text))
            if price > MAX_PRICE or price < 100_000:
                continue
            addr_el = card.select_one("address")
            address = addr_el.text.strip() if addr_el else ""
            detail_url = "https://www.zillow.com/homedetails/" + zpid + "_zpid/"
            beds_el = card.select_one("[data-test='bed-bath-sqft-info'] abbr[title='bd']")
            beds  = int(beds_el.text.strip()) if beds_el else 0
            baths = 0
            sqft  = 0
            listings.append({
                "zpid":        zpid,
                "address":     address,
                "city":        city,
                "price":       price,
                "beds":        beds,
                "baths":       baths,
                "sqft":        sqft,
                "status":      "FOR_SALE",
                "url":         detail_url,
                "listed_date": "",
                "annual_tax":  0,
            })
        except Exception as e:
            print(f"  HTML fallback parse error: {e}")

    return listings

def fetch_property_details(listing):
    """Fetch annual tax and confirm status from the listing detail page."""
    html = fetch(listing["url"], delay=2)
    if not html:
        return listing

    # Check pending status
    if "is pending" in html.lower() or '"statusType":"PENDING"' in html:
        listing["status"] = "PENDING"

    # Extract annual tax
    tax_match = re.search(r'"annualPropertyTax"\s*:\s*(\d+)', html)
    if tax_match:
        listing["annual_tax"] = int(tax_match.group(1))
    else:
        # Try text patterns like "$8,613/year" or "Annual tax: $8,613"
        m = re.search(r'(?:annual\s+tax|property\s+tax)[^$]*\$\s*([\d,]+)', html, re.IGNORECASE)
        if m:
            listing["annual_tax"] = int(m.group(1).replace(",", ""))

    # Extract sqft if not already set
    if not listing.get("sqft"):
        sqft_m = re.search(r'"livingArea"\s*:\s*(\d+)', html)
        if sqft_m:
            listing["sqft"] = int(sqft_m.group(1))

    return listing


# ──────────────────────────────────────────────
# STATE MANAGEMENT
# ──────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"listings": {}, "pending": {}, "last_run": ""}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ──────────────────────────────────────────────
# CHANGE DETECTION
# ──────────────────────────────────────────────
def detect_changes(current_listings, prev_state):
    prev_by_zpid = {z: d for z, d in prev_state.get("listings", {}).items()}
    prev_pending  = set(prev_state.get("pending", {}).keys())

    new_listings    = []
    newly_pending   = []
    price_changes   = []

    for lst in current_listings:
        zpid = lst["zpid"]
        if lst["status"] == "PENDING":
            if zpid not in prev_pending:
                newly_pending.append(lst)
        else:
            if zpid not in prev_by_zpid:
                new_listings.append(lst)
            elif prev_by_zpid[zpid]["price"] != lst["price"]:
                old_p = prev_by_zpid[zpid]["price"]
                delta = lst["price"] - old_p
                price_changes.append({**lst, "old_price": old_p, "delta": delta})

    return new_listings, newly_pending, price_changes


# ──────────────────────────────────────────────
# NOTIFICATION
# ──────────────────────────────────────────────
def send_notification(title, body, priority="default"):
    if not NTFY_TOPIC:
        print("No NTFY_TOPIC set — skipping notification")
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title":    title,
                "Priority": priority,
                "Tags":     "house",
            },
            timeout=10,
        )
        print(f"  Notification sent: {title}")
    except Exception as e:
        print(f"  Notification error: {e}")


# ──────────────────────────────────────────────
# HTML GENERATION
# ──────────────────────────────────────────────
def fmt_price(n):
    return f"${n:,.0f}"

def fmt_money(n):
    return f"${n:,.0f}"

def map_iframe(address, city_state):
    q = requests.utils.quote(f"{address}, {city_state}")
    src = f"https://maps.google.com/maps?q={q}&z=16&output=embed"
    return (
        f'<div class="card-map">'
        f'<iframe src="{src}" width="100%" height="130" '
        f'style="border:0;border-radius:0" loading="lazy" '
        f'referrerpolicy="no-referrer-when-downgrade"></iframe>'
        f'</div>'
    )

def build_card(lst, today_str):
    price = lst["price"]
    tax   = lst["annual_tax"] or 8_000   # fallback estimate
    city  = lst["city"]
    addr  = lst["address"]
    beds  = lst["beds"]
    baths = lst["baths"]
    sqft  = lst["sqft"]
    url   = lst["url"]
    listed = lst.get("listed_date", "")
    is_new = lst.get("is_new", False)

    city_state_map = {"Cedarhurst": "Cedarhurst, NY 11516",
                      "Woodmere":   "Woodmere, NY 11598",
                      "Lawrence":   "Lawrence, NY 11559"}

    # Payments
    total_55, down_55, loan_55, pi_55 = calc_monthly(price, tax, ARM_RATE)
    total_6125, down_6125, loan_6125, pi_6125 = calc_monthly(price, tax, FIXED_RATE)

    new_ribbon = '<span class="new-ribbon">NEW</span>' if is_new else ""
    new_cls    = " new-card" if is_new else ""
    new_tag    = '<span class="tag new-tag">🆕 Added Today</span>' if is_new else ""

    specs_parts = []
    if beds:  specs_parts.append(f"{beds} bed")
    if baths: specs_parts.append(f"{baths} bath")
    if sqft:  specs_parts.append(f"{int(sqft):,} sqft")
    if listed: specs_parts.append(f"Listed {listed[:10]}")
    specs = " · ".join(specs_parts) if specs_parts else "Single-family"

    map_html = map_iframe(addr, city_state_map.get(city, city + ", NY"))

    return f"""
    <div class="card{new_cls}">
      <div class="card-header">
        {new_ribbon}
        <div class="price">{fmt_price(price)}</div>
        <div class="address">{addr}, {city}</div>
        <div class="specs">{specs}</div>
      </div>
      {map_html}
      <div class="card-body">
        <div class="monthly-total">
          <div class="amount-55 rate-55">{fmt_money(total_55)}<span class="rate-label">/mo</span></div>
          <div class="amount-6125 rate-6125" style="display:none">{fmt_money(total_6125)}<span class="rate-label">/mo</span></div>
          <div class="amount-both rate-both" style="display:none">
            <span class="both-55">{fmt_money(total_55)}</span>
            <span class="both-sep"> · </span>
            <span class="both-6125">{fmt_money(total_6125)}</span>
            <span class="rate-label">/mo</span>
          </div>
        </div>
        <div class="down-detail">20% down · {fmt_price(down_55)} · Loan {fmt_price(loan_55)}</div>
        <div class="breakdown">
          <div class="breakdown-row rate-55">P&amp;I (5.5% ARM): {fmt_money(pi_55)}/mo</div>
          <div class="breakdown-row rate-6125" style="display:none">P&amp;I (6.125% Fixed): {fmt_money(pi_6125)}/mo</div>
          <div class="breakdown-row rate-both" style="display:none">
            P&amp;I: {fmt_money(pi_55)}/mo (ARM) · {fmt_money(pi_6125)}/mo (Fixed)
          </div>
          <div class="breakdown-row">Property Tax: {fmt_money(round(tax/12))}/mo ({fmt_price(tax)}/yr)</div>
          <div class="breakdown-row">Home Insurance: $320/mo</div>
        </div>
      </div>
      <div class="card-footer">
        {new_tag}
        <a class="zillow-link" href="{url}" target="_blank">View on Zillow →</a>
      </div>
    </div>"""

def build_pending_item(lst, is_new_today=False):
    badge_cls = "badge new-badge" if is_new_today else "badge"
    badge_txt = f"🆕 NEW TODAY · {lst['city']}" if is_new_today else lst["city"]
    addr      = lst["address"]
    price     = fmt_price(lst["price"])
    beds      = lst.get("beds", "")
    baths     = lst.get("baths", "")
    sqft      = lst.get("sqft", "")
    url       = lst.get("url", "#")
    date_str  = lst.get("pending_date", "")
    specs_parts = []
    if beds:  specs_parts.append(f"{beds}bd")
    if baths: specs_parts.append(f"{baths}ba")
    if sqft:  specs_parts.append(f"{int(sqft):,} sqft")
    specs = "/".join(specs_parts[:2]) + (f" · {specs_parts[2]}" if len(specs_parts) > 2 else "")
    date_label = f"⚡ Went pending {date_str}" if date_str else "Status: Pending"

    return f"""
      <div class="pending-item">
        <span class="{badge_cls}">{badge_txt}</span>
        <div class="info">
          <strong><a href="{url}" target="_blank" style="color:white">{addr}</a> — {price}</strong>
          <span class="date">{date_label} · {specs}</span>
        </div>
      </div>"""

def generate_html(active_listings, pending_listings, last_run_str, today_str):
    """Build the full index.html page."""

    # Group active listings by city
    by_city = {"Cedarhurst": [], "Woodmere": [], "Lawrence": []}
    for lst in active_listings:
        city = lst.get("city", "")
        if city in by_city:
            by_city[city].append(lst)

    # Build city sections
    city_sections = ""
    for city, listings in by_city.items():
        zip_codes = {"Cedarhurst": "11516", "Woodmere": "11598", "Lawrence": "11559"}
        cards_html = "".join(build_card(lst, today_str) for lst in listings)
        city_sections += f"""
    <section class="city-section">
      <h2 class="city-header">{city} <span class="zip">{zip_codes[city]}</span>
        <span class="count">{len(listings)} listings</span></h2>
      <div class="card-grid">
        {cards_html}
      </div>
    </section>"""

    # Pending banner
    pending_items_html = ""
    new_today_zpids = {lst["zpid"] for lst in pending_listings if lst.get("is_new_today")}
    for lst in sorted(pending_listings, key=lambda x: x.get("is_new_today", False), reverse=True):
        is_new_today = lst.get("is_new_today", False)
        pending_items_html += build_pending_item(lst, is_new_today)

    pending_count = len(pending_listings)
    pending_banner = ""
    if pending_listings:
        pending_banner = f"""
  <div class="pending-banner">
    <div class="pending-header">🚨 {pending_count} PROPERTIES NOW PENDING — No longer available</div>
    <div class="pending-list">
      {pending_items_html}
    </div>
  </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Home Search — Cedarhurst · Woodmere · Lawrence</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f3f4f6; color: #1f2937; }}

  /* HEADER */
  .site-header {{ background: #1e293b; color: white; padding: 20px 24px; }}
  .site-header h1 {{ font-size: 22px; font-weight: 700; }}
  .site-header .subtitle {{ font-size: 13px; color: #94a3b8; margin-top: 4px; }}
  .last-updated {{ font-size: 12px; color: #64748b; margin-top: 6px; }}

  /* RATE TOGGLE */
  .rate-bar {{ background: #0f172a; padding: 12px 24px; display: flex; align-items: center; gap: 10px; position: sticky; top: 0; z-index: 100; border-bottom: 1px solid #1e293b; }}
  .rate-bar span {{ color: #94a3b8; font-size: 13px; font-weight: 500; }}
  .rate-btn {{ padding: 6px 16px; border: 1px solid #334155; border-radius: 20px; background: transparent; color: #cbd5e1; font-size: 13px; cursor: pointer; transition: all .15s; }}
  .rate-btn.active {{ background: #3b82f6; border-color: #3b82f6; color: white; }}
  .rate-btn:hover:not(.active) {{ background: #1e293b; }}

  /* PENDING BANNER */
  .pending-banner {{ background: #991b1b; color: white; padding: 16px 24px; }}
  .pending-header {{ font-size: 15px; font-weight: 700; margin-bottom: 12px; }}
  .pending-list {{ display: flex; flex-direction: column; gap: 8px; }}
  .pending-item {{ display: flex; align-items: flex-start; gap: 10px; }}
  .badge {{ background: rgba(255,255,255,0.2); border-radius: 12px; padding: 2px 10px; font-size: 11px; font-weight: 600; white-space: nowrap; }}
  .new-badge {{ background: #f59e0b; color: #1f2937; }}
  .info {{ font-size: 13px; line-height: 1.5; }}
  .info .date {{ display: block; color: rgba(255,255,255,0.75); font-size: 12px; }}

  /* CITY SECTIONS */
  .city-section {{ padding: 24px; max-width: 1400px; margin: 0 auto; }}
  .city-header {{ font-size: 20px; font-weight: 700; margin-bottom: 16px; color: #1e293b; display: flex; align-items: baseline; gap: 10px; }}
  .city-header .zip {{ font-size: 14px; color: #6b7280; font-weight: 400; }}
  .city-header .count {{ font-size: 13px; color: #9ca3af; font-weight: 500; margin-left: auto; }}

  /* CARD GRID */
  .card-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }}
  .card {{ background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); border: 1px solid #e5e7eb; transition: box-shadow .2s; }}
  .card:hover {{ box-shadow: 0 4px 12px rgba(0,0,0,.12); }}
  .new-card {{ border: 2px solid #3b82f6; }}

  /* CARD HEADER */
  .card-header {{ padding: 16px 16px 12px; position: relative; }}
  .new-ribbon {{ position: absolute; top: 12px; right: 12px; background: #3b82f6; color: white; font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 10px; letter-spacing: .5px; }}
  .price {{ font-size: 22px; font-weight: 700; color: #111827; }}
  .address {{ font-size: 14px; color: #4b5563; margin-top: 2px; }}
  .specs {{ font-size: 12px; color: #9ca3af; margin-top: 4px; }}

  /* MAP */
  .card-map {{ border-top: 1px solid #f3f4f6; border-bottom: 1px solid #f3f4f6; overflow: hidden; }}
  .card-map iframe {{ display: block; }}

  /* CARD BODY */
  .card-body {{ padding: 14px 16px; }}
  .monthly-total {{ font-size: 26px; font-weight: 700; color: #059669; }}
  .rate-label {{ font-size: 14px; color: #6b7280; font-weight: 400; }}
  .both-55 {{ color: #059669; }}
  .both-sep {{ font-size: 18px; color: #d1d5db; }}
  .both-6125 {{ color: #7c3aed; }}
  .down-detail {{ font-size: 12px; color: #6b7280; margin-top: 4px; }}
  .breakdown {{ margin-top: 10px; border-top: 1px solid #f3f4f6; padding-top: 10px; }}
  .breakdown-row {{ font-size: 12px; color: #6b7280; line-height: 1.8; }}

  /* CARD FOOTER */
  .card-footer {{ padding: 12px 16px; border-top: 1px solid #f3f4f6; display: flex; justify-content: space-between; align-items: center; }}
  .tag {{ font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 10px; }}
  .new-tag {{ background: #dbeafe; color: #1d4ed8; }}
  .zillow-link {{ font-size: 13px; color: #3b82f6; text-decoration: none; font-weight: 500; }}
  .zillow-link:hover {{ text-decoration: underline; }}

  /* NO LISTINGS MESSAGE */
  .no-listings {{ color: #9ca3af; font-size: 14px; padding: 20px 0; }}
</style>
</head>
<body>

<header class="site-header">
  <h1>🏡 Home Search — Five Towns NY</h1>
  <div class="subtitle">Cedarhurst 11516 · Woodmere 11598 · Lawrence 11559 · Single-family under $1,310,000</div>
  <div class="last-updated">Last updated: {last_run_str}</div>
</header>

<div class="rate-bar">
  <span>Monthly payment at:</span>
  <button class="rate-btn active" id="btn-55"  onclick="setRate('55')">5.5% ARM</button>
  <button class="rate-btn"        id="btn-6125" onclick="setRate('6125')">6.125% Fixed</button>
  <button class="rate-btn"        id="btn-both" onclick="setRate('both')">Show Both</button>
</div>

{pending_banner}

{city_sections}

<script>
function setRate(r) {{
  document.querySelectorAll('.amount-55').forEach(el => el.style.display = r==='55' ? '' : 'none');
  document.querySelectorAll('.amount-6125').forEach(el => el.style.display = r==='6125' ? '' : 'none');
  document.querySelectorAll('.amount-both').forEach(el => el.style.display = r==='both' ? '' : 'none');
  document.querySelectorAll('.breakdown-row.rate-55').forEach(el => el.style.display = r==='55' ? '' : 'none');
  document.querySelectorAll('.breakdown-row.rate-6125').forEach(el => el.style.display = r==='6125' ? '' : 'none');
  document.querySelectorAll('.breakdown-row.rate-both').forEach(el => el.style.display = r==='both' ? '' : 'none');
  ['btn-55','btn-6125','btn-both'].forEach(id => document.getElementById(id).classList.remove('active'));
  document.getElementById('btn-' + r).classList.add('active');
}}
</script>
</body>
</html>"""


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    now = datetime.now(timezone.utc)
    today_str    = now.strftime("%m/%d/%Y")
    last_run_str = now.strftime("%B %d, %Y at %I:%M %p UTC")

    print(f"\n{'='*50}")
    print(f"Home Search Run — {last_run_str}")
    print(f"{'='*50}\n")

    # Load previous state
    state = load_state()

    # ── SEARCH ALL 3 CITIES ──
    all_active  = []
    all_pending = []

    for city, url in SEARCH_URLS.items():
        print(f"Searching {city}...")
        html = fetch(url, delay=4)
        if not html:
            print(f"  FAILED to fetch {city} — using cached data")
            # Fall back to cached listings for this city
            for zpid, lst in state.get("listings", {}).items():
                if lst.get("city") == city:
                    all_active.append(lst)
            continue

        listings = parse_search_page(html, city)
        print(f"  Found {len(listings)} listings under ${MAX_PRICE:,}")

        # Fetch details for each listing (tax, status)
        for i, lst in enumerate(listings):
            print(f"  [{i+1}/{len(listings)}] Fetching details for {lst['address']}...")
            lst = fetch_property_details(lst)
            if lst["status"] == "PENDING":
                all_pending.append(lst)
            else:
                all_active.append(lst)

    print(f"\nTotal active: {len(all_active)}")
    print(f"Total pending: {len(all_pending)}")

    # ── DETECT CHANGES ──
    new_listings, newly_pending, price_changes = detect_changes(all_active + all_pending, state)

    # Mark new listings
    new_zpids = {lst["zpid"] for lst in new_listings}
    for lst in all_active:
        lst["is_new"] = lst["zpid"] in new_zpids

    # Merge with existing pending (keep previous pending even if not found this run)
    all_pending_dict = {lst["zpid"]: lst for lst in all_pending}
    for zpid, lst in state.get("pending", {}).items():
        if zpid not in all_pending_dict:
            all_pending_dict[zpid] = lst

    # Mark newly pending today
    newly_pending_zpids = {lst["zpid"] for lst in newly_pending}
    for lst in all_pending_dict.values():
        lst["is_new_today"] = lst["zpid"] in newly_pending_zpids
        if lst["is_new_today"] and not lst.get("pending_date"):
            lst["pending_date"] = today_str

    pending_list_sorted = sorted(
        all_pending_dict.values(),
        key=lambda x: (not x.get("is_new_today", False), x.get("pending_date", ""))
    )

    # ── NOTIFY ──
    if new_listings or newly_pending or price_changes:
        title = f"🏡 Home Search Update — {today_str}"
        lines = []
        if new_listings:
            lines.append(f"🆕 {len(new_listings)} NEW listing(s):")
            for lst in new_listings:
                lines.append(f"  • {lst['address']}, {lst['city']} — {fmt_price(lst['price'])}")
        if newly_pending:
            lines.append(f"🚨 {len(newly_pending)} newly PENDING:")
            for lst in newly_pending:
                lines.append(f"  • {lst['address']}, {lst['city']} — {fmt_price(lst['price'])}")
        if price_changes:
            lines.append(f"💰 {len(price_changes)} price change(s):")
            for lst in price_changes:
                direction = "↓" if lst["delta"] < 0 else "↑"
                lines.append(f"  • {lst['address']} {direction} {fmt_price(abs(lst['delta']))}")
        body = "\n".join(lines)
        print(f"\nChanges detected:\n{body}")
        send_notification(title, body, priority="high")
    else:
        title = "✅ Home Search — No Changes"
        body  = f"Checked {len(all_active)} active listings across Cedarhurst, Woodmere, Lawrence. Nothing new."
        print(f"\nNo changes detected.")
        send_notification(title, body)

    # ── GENERATE HTML ──
    print("\nGenerating index.html...")
    html_out = generate_html(all_active, pending_list_sorted, last_run_str, today_str)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_out)
    print("  index.html written.")

    # ── SAVE STATE ──
    new_state = {
        "listings": {lst["zpid"]: lst for lst in all_active},
        "pending":  all_pending_dict,
        "last_run": last_run_str,
    }
    save_state(new_state)
    print("  known_listings.json updated.")

    # Output summary for GitHub Actions commit message
    changes_count = len(new_listings) + len(newly_pending) + len(price_changes)
    if changes_count:
        summary = f"{changes_count} change(s): {len(new_listings)} new, {len(newly_pending)} pending, {len(price_changes)} price"
    else:
        summary = "no changes"
    print(f"\nSUMMARY: {summary}")
    # Write for use in commit message
    with open("run_summary.txt", "w") as f:
        f.write(summary)

    print("\nDone.\n")

if __name__ == "__main__":
    main()
