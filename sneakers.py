#!/usr/bin/env python3
"""FlipScanner sneakers: groups listings by style code (e.g. DH6927-111) and size,
then flags pairs priced well under the median for that exact shoe."""
import argparse, json, re, statistics, sys, os
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flipscanner import load_env, get_token, total_price
import urllib.parse, urllib.request

DEAL_RATIO, FLOOR_RATIO = 0.70, 0.30
MIN_FEEDBACK, MIN_FEEDBACK_PCT = 5, 97.0
MIN_GROUP = 5

CAT = "15709"  # Men's Athletic Shoes
SEARCHES = [
    "jordan 1 low", "air jordan 1 low og", "jordan 3", "air jordan 3 retro",
    "jordan 4", "air jordan 4 retro", "travis scott jordan", "travis scott jordan 1 low",
    "yeezy boost 350", "yeezy 350 v2",
    # misspellings
    "jordon 4", "jordon 1 low", "jordan iv", "jordan lll", "yezzy 350", "yeezey 350",
    "yeezy boost 305", "travis scot jordan", "travis scoot jordan",
]

JUNK = re.compile(
    r"\b(replica|rep|reps|ua|unauthorized|1:1|batch|fake|custom|customized|painted|"
    r"box only|empty box|laces only|insoles?|keychain|sticker|poster|socks|shirt|hoodie|"
    r"display|toddler|infant|crib|td|ps|gs|youth|kids?|womens?|wmns|left shoe|right shoe|"
    r"single shoe|one shoe|damaged|defect|defective|read|issue|cleaning|cleaner|crep|"
    r"protector|lot of|bundle)\b", re.I)

STYLE = re.compile(r"\b([a-z]{2}\d{4}|\d{6})[- ](\d{3})\b", re.I)  # DH6927-111, 555088 134
SIZE = re.compile(r"\b(?:size|sz|us|men'?s|mens|m)\s?:?\s?(\d{1,2}(?:\.5)?)\b", re.I)


def search(token, q, limit=200):
    params = {"q": q, "category_ids": CAT, "limit": str(limit), "sort": "newlyListed",
              "filter": "buyingOptions:{FIXED_PRICE},itemLocationCountry:US,"
                        "priceCurrency:USD,price:[60..3000]"}
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=30)).get("itemSummaries", [])
    except Exception as e:
        print(f"  search failed for {q!r}: {e}", file=sys.stderr)
        return []


def classify(title):
    if JUNK.search(title):
        return None, None
    st = STYLE.search(title)
    if not st:
        return None, None
    code = f"{st.group(1).upper()}-{st.group(2)}"
    sz = SIZE.search(title)
    size = sz.group(1) if sz and 4 <= float(sz.group(1)) <= 16 else None
    return code, size


def scan(since_minutes=None):
    token = get_token()
    seen, rows = set(), []
    for q in SEARCHES:
        for it in search(token, q):
            iid = it.get("itemId")
            if iid in seen or "price" not in it:
                continue
            seen.add(iid)
            code, size = classify(it.get("title", ""))
            if code:
                new = str(it.get("condition", "")).lower().startswith("new") and not re.search(
                    r"\b(worn|used|pre-?owned|vnds|tried on)\b", it.get("title", ""), re.I)
                code = f"{code} {'New' if new else 'Used'}"
                rows.append((code, size, it, total_price(it)))

    # One price per seller per group (their cheapest), so a big store listing
    # every size at a high price can't drag the market price up.
    bc, bcs = {}, {}
    for code, size, it, p in rows:
        seller = it.get("seller", {}).get("username", it.get("itemId"))
        k = (code, seller)
        bc[k] = min(bc.get(k, p), p)
        if size:
            k2 = (code, size, seller)
            bcs[k2] = min(bcs.get(k2, p), p)
    by_code, by_code_size = {}, {}
    for (code, _), p in bc.items():
        by_code.setdefault(code, []).append(p)
    for (code, size, _), p in bcs.items():
        by_code_size.setdefault((code, size), []).append(p)

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes) if since_minutes else None
    deals = []
    for code, size, it, p in rows:
        comps = by_code_size.get((code, size)) if size else None
        basis = f"{code} size {size}"
        if not comps or len(comps) < MIN_GROUP:
            comps, basis = by_code.get(code, []), f"{code} all sizes"
        if len(comps) < MIN_GROUP:
            continue
        market = statistics.median(comps)
        if not (FLOOR_RATIO * market <= p <= DEAL_RATIO * market):
            continue
        seller = it.get("seller", {})
        fb = int(seller.get("feedbackScore", 0) or 0)
        pct = float(seller.get("feedbackPercentage", 0) or 0)
        if fb < MIN_FEEDBACK or pct < MIN_FEEDBACK_PCT:
            continue
        created = it.get("itemCreationDate")
        if cutoff and created and datetime.fromisoformat(created.replace("Z", "+00:00")) < cutoff:
            continue
        deals.append({
            "product": f"Sneakers {code}" + (f" sz {size}" if size else ""), "title": it["title"],
            "price": round(p, 2), "market": round(market, 2), "basis": basis,
            "save_pct": round(100 * (1 - p / market)), "profit_est": round(market * 0.87 - p, 2),
            "authenticity_guarantee": p >= 150,
            "condition": it.get("condition", ""), "seller_feedback": f"{fb} ({pct}%)",
            "comps": len(comps), "listed": created, "url": it.get("itemWebUrl"),
        })
    deals.sort(key=lambda d: -d["profit_est"])
    return deals, {k: (len(v), round(statistics.median(v), 2)) for k, v in by_code.items()
                   if len(v) >= MIN_GROUP}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    load_env()
    deals, markets = scan(a.since)
    if a.json:
        print(json.dumps({"deals": deals, "markets": markets}, indent=1))
        return
    print(f"Tracked {len(markets)} sneaker style codes. Deals found: {len(deals)}\n")
    for d in deals:
        ag = " | eBay authenticity check" if d["authenticity_guarantee"] else ""
        print(f"{d['product']}: ${d['price']} vs market ${d['market']} ({d['basis']}, "
              f"{d['save_pct']}% under, est profit ${d['profit_est']})")
        print(f"  {d['title']}\n  {d['condition']} | seller {d['seller_feedback']}{ag} | {d['url']}\n")


if __name__ == "__main__":
    main()
