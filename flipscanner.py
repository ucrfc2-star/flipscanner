#!/usr/bin/env python3
"""FlipScanner: finds underpriced Apple listings on eBay.

How it works:
  1. Searches eBay (newest listings first) for each tracked Apple product.
  2. Groups listings by exact model + storage, and uses the median Buy It Now
     price of that group as the "market price".
  3. Flags listings priced well under market, from sellers with good feedback.

Usage:
  python3 flipscanner.py                 # all listings, any age
  python3 flipscanner.py --since 70      # only listings posted in the last 70 minutes
Keys come from environment vars EBAY_APP_ID and EBAY_CERT_ID (or a .env file).
"""
import argparse, base64, json, os, re, statistics, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

DEAL_RATIO = 0.70      # flag anything at or under 70% of market price
FLOOR_RATIO = 0.25     # under 25% of market is almost always a scam or broken
MIN_FEEDBACK = 5
MIN_FEEDBACK_PCT = 95.0
MIN_GROUP = 6          # need this many comparable listings to trust a market price

# Searches: (eBay search text, eBay category id)
# 9355 = Cell Phones & Smartphones, 80077 = Headphones, 178893 = Smart Watches, 171485 = Tablets
SEARCHES = [
    ("iphone 13", "9355"), ("iphone 13 pro", "9355"), ("iphone 13 pro max", "9355"),
    ("iphone 14", "9355"), ("iphone 14 pro", "9355"), ("iphone 14 pro max", "9355"),
    ("iphone 15", "9355"), ("iphone 15 pro", "9355"), ("iphone 15 pro max", "9355"),
    ("airpods pro", "80077"),
    ("apple watch series 7", "178893"), ("apple watch series 8", "178893"),
    ("apple watch series 9", "178893"),
    ("ipad air", "171485"), ("ipad pro", "171485"),
    # Common misspellings: buyers can't find these, so they sit cheap
    ("iphne", "9355"), ("ipone", "9355"), ("iphon 15", "9355"), ("iphone15", "9355"),
    ("air pods pro", "80077"), ("airpod pro", "80077"),
    ("aple watch", "178893"), ("appel watch", "178893"), ("ipda", "171485"),
]

JUNK = re.compile(
    r"\b(case|cases|cover|screen protector|protector|box only|empty box|for parts|parts only|"
    r"broken|cracked|crack|icloud|locked|activation|blacklist|bad esn|read desc|read description|"
    r"as is|as-is|not working|no power|charger|cable|replacement|housing|back glass|lcd|"
    r"battery only|skin|dummy|fake|replica|clone|lot of|bundle of|left only|right only|"
    r"left airpod|right airpod|case only|charging case only|band only|strap|mount|stand|"
    r"keyboard only|pencil only|sticker|decal|logic board|motherboard|camera lens|"
    r"tempered|glass film|holder|wallet|stylus|digitizer|frame|non oem|non-oem|aftermarket|"
    r"defective|spots?|lines|burn|left side|right side|ear tips?|read|issue|issues|damaged|"
    r"faulty|repair|refurb kit|demo|mdm|managed|finance|financed|unpaid)\b", re.I)

CONDITIONS = "1000|1500|2000|2010|2020|2030|2500|3000"  # new through used, excludes for parts


def load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        for line in open(path):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)


def get_token():
    app, cert = os.environ["EBAY_APP_ID"], os.environ["EBAY_CERT_ID"]
    req = urllib.request.Request(
        "https://api.ebay.com/identity/v1/oauth2/token",
        data=b"grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Authorization": "Basic " + base64.b64encode(f"{app}:{cert}".encode()).decode()})
    return json.load(urllib.request.urlopen(req, timeout=30))["access_token"]


def search(token, q, cat, limit=200):
    params = {
        "q": q, "category_ids": cat, "limit": str(limit), "sort": "newlyListed",
        "filter": f"buyingOptions:{{FIXED_PRICE}},conditionIds:{{{CONDITIONS}}},"
                  "itemLocationCountry:US,priceCurrency:USD,price:[40..2500]",
    }
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=30)).get("itemSummaries", [])
    except Exception as e:
        print(f"  search failed for {q!r}: {e}", file=sys.stderr)
        return []


def storage(t):
    m = re.search(r"\b(64|128|256|512)\s?gb\b|\b(1|2)\s?tb\b", t)
    if not m:
        return None
    return f"{m.group(1)}GB" if m.group(1) else f"{m.group(2)}TB"


def classify(title):
    """Return a product key like 'iPhone 15 Pro Max 256GB', or None if unsure."""
    t = title.lower()
    if JUNK.search(t):
        return None
    t = re.sub(r"i\s?ph?o?ne?(?=\s?\d)", "iphone ", t)   # iphne 15, ipone15, iphone15
    t = re.sub(r"\s+", " ", t)

    m = re.search(r"iphone (1[345])(?!\d)( pro max| pro| plus| mini)?", t)
    if m:
        if len(set(re.findall(r"iphone (1\d)", t))) > 1:
            return None  # "fits iphone 13 14 15" style listings
        model = "iPhone " + m.group(1) + (m.group(2) or "").title()
        s = storage(t)
        if not s:
            return None
        carrier = re.search(r"at&t|att\b|verizon|t-?mobile|sprint|cricket|spectrum|xfinity|"
                            r"boost|metro|tracfone|straight talk|visible|us cellular|carrier", t)
        lock = "Unlocked" if ("unlocked" in t and not re.search(r"locked to|carrier locked", t)) \
            else ("Carrier" if carrier else None)
        return f"{model} {s} {lock}" if lock else None

    if re.search(r"air\s?pods? pro", t):
        if re.search(r"\b(2nd|second|gen 2|2 gen|usb-?c|2023)\b|pro 2\b", t):
            return "AirPods Pro 2"
        if re.search(r"\b(1st|first|gen 1)\b", t):
            return "AirPods Pro 1"
        return None

    m = re.search(r"(?:apple|aple|appel) watch (?:series )?([789])\b", t)
    if m and "series" in t:
        size = re.search(r"\b(41|45)\s?mm\b", t)
        cell = " Cellular" if re.search(r"cellular|lte|gps ?\+ ?cell", t) else " GPS"
        return f"Apple Watch S{m.group(1)} {size.group(1)}mm{cell}" if size else None

    if re.search(r"\bipad\b|\bipda\b", t):
        if "ipad air" in t or "ipda air" in t:
            g = re.search(r"\b(4th|5th|6th)\b|\bm(1|2)\b", t)
            s = storage(t)
            if g and s:
                return f"iPad Air {g.group(0).upper()} {s}"
        if "ipad pro" in t:
            size = re.search(r"\b(11|12\.9)\s?(?:in|inch|\")", t)
            chip = re.search(r"\bm([124])\b", t)
            s = storage(t)
            if size and chip and s:
                return f"iPad Pro {size.group(1)}in M{chip.group(1)} {s}"
    return None


def total_price(it):
    p = float(it["price"]["value"])
    ship = it.get("shippingOptions") or []
    if ship and "shippingCost" in ship[0]:
        p += float(ship[0]["shippingCost"].get("value", 0))
    return p


def scan(since_minutes=None):
    token = get_token()
    groups, seen = {}, set()
    for q, cat in SEARCHES:
        for it in search(token, q, cat):
            iid = it.get("itemId")
            if iid in seen or "price" not in it:
                continue
            seen.add(iid)
            key = classify(it.get("title", ""))
            if key:
                groups.setdefault(key, []).append(it)

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes) if since_minutes else None
    deals = []
    for key, items in groups.items():
        prices = [total_price(i) for i in items]
        if len(prices) < MIN_GROUP:
            continue
        market = statistics.median(prices)
        for it, p in zip(items, prices):
            if not (FLOOR_RATIO * market <= p <= DEAL_RATIO * market):
                continue
            seller = it.get("seller", {})
            fb = int(seller.get("feedbackScore", 0) or 0)
            pct = float(seller.get("feedbackPercentage", 0) or 0)
            if fb < MIN_FEEDBACK or pct < MIN_FEEDBACK_PCT:
                continue
            created = it.get("itemCreationDate")
            if cutoff and created:
                if datetime.fromisoformat(created.replace("Z", "+00:00")) < cutoff:
                    continue
            deals.append({
                "product": key, "title": it["title"], "price": round(p, 2),
                "market": round(market, 2), "save_pct": round(100 * (1 - p / market)),
                "profit_est": round(market * 0.87 - p, 2),  # rough: about 13% eBay fees on resale
                "condition": it.get("condition", ""), "seller_feedback": f"{fb} ({pct}%)",
                "comps": len(prices), "listed": created, "url": it.get("itemWebUrl"),
            })
    deals.sort(key=lambda d: -d["profit_est"])
    return deals, {k: (len(v), round(statistics.median([total_price(i) for i in v]), 2))
                   for k, v in groups.items() if len(v) >= MIN_GROUP}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, help="only listings posted within this many minutes")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    load_env()
    deals, markets = scan(a.since)
    if a.json:
        print(json.dumps({"deals": deals, "markets": markets}, indent=1))
        return
    print(f"Tracked {len(markets)} product groups. Deals found: {len(deals)}\n")
    for d in deals:
        print(f"{d['product']}: ${d['price']} vs market ${d['market']} "
              f"({d['save_pct']}% under, est profit ${d['profit_est']})")
        print(f"  {d['title']}\n  {d['condition']} | seller {d['seller_feedback']} | {d['url']}\n")


if __name__ == "__main__":
    main()
