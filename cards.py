#!/usr/bin/env python3
"""FlipScanner cards: numbered cards (/500 down to 1/1) for Konnor Griffin, Shohei Ohtani,
Yoshinobu Yamamoto and Victor Wembanyama.

Two tracks:
  SLEEPER: misspelled listings (Connor, Conner, Konner, Griffen...). Collectors searching
           "Konnor Griffin" never see these, so they sit cheap or auctions end low.
  DEAL:    correctly spelled listings priced far under comparable listings.

Comparables = correctly spelled Buy It Now listings of the same card number, serial
number (/99 etc), auto or not, and grade (PSA 10, raw...). One price per seller.
"""
import argparse, json, os, re, statistics, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flipscanner import load_env, get_token

CAT = "261328"  # Sports Trading Card Singles
MAX_SERIAL = 500
SLEEPER_MAX_RATIO = 0.85   # misspelled listing must be under 85% of comps (or have no comps)
DEAL_RATIO = 0.60          # correct spelling must be at or under 60% of comps
MIN_COMPS = 3
MIN_FEEDBACK, MIN_FEEDBACK_PCT = 3, 95.0

DENOMS = [1, 5, 10, 25, 50, 75, 99, 150, 199, 250, 299, 500]

# Each player: correct name, regex that finds the player in any spelling, misspelled
# tokens (the sleepers), and extra context needed to be sure it's the right person.
PLAYERS = {
    "Konnor Griffin": {
        "correct": "konnor griffin",
        "present": r"\b[kc]on+?[eo]r\b.{0,4}\bgr[iy]f+[ie]?ns?\b|\bgr[iy]f+[ie]?n\b.{0,40}pirates",
        "bad": r"\b(connor|conner|konner|conor|konor|griffen|grifin|griffn|griffins)\b",
        "context": r"pirates|pittsburgh|bowman|topps|panini|prizm|leaf|\brc\b|rookie|1st|prospect|refractor|chrome|psa|bgs|auto",
        "misspell_queries": ["connor griffin", "conner griffin", "konner griffin", "konnor griffen",
                             "connor griffen", "konner griffen", "conner griffen", "conor griffin",
                             "konor griffin", "konnor grifin"],
    },
    "Shohei Ohtani": {
        "correct": "shohei ohtani",
        "present": r"\b(ohtani|otani|othani|ohtoni|ohatani|ohanti|ohtnai|ohtanni|ohtini)\b",
        "bad": r"\b(otani|othani|ohtoni|ohatani|ohanti|ohtnai|ohtanni|ohtini|shohie|shoei|shohi|sohei|shoehi|shohai)\b",
        "context": r"dodgers|angels|topps|bowman|panini|prizm|\brc\b|rookie|psa|bgs|auto|wbc|japan|refractor|chrome",
        "misspell_queries": ["shohei otani", "shohie ohtani", "shohei othani", "shoei ohtani",
                             "shohei ohtoni", "shohei ohatani", "sohei ohtani", "shohei ohanti"],
    },
    "Yoshinobu Yamamoto": {
        "correct": "yoshinobu yamamoto",
        "present": r"\b(yamamoto|yamamota|yamomoto|yamamto|yamamotto|yammamoto|yamamato)\b.{0,60}|\byosh?i?n[ou]b?[ou]+\b",
        "bad": r"\b(yamamota|yamomoto|yamamto|yamamotto|yammamoto|yamamato|yoshinubo|yoshinbu|yoshinoubu|yoshonobu|yoshinob|yoshimobu|yoshinbou)\b",
        "context": r"dodgers|orix|japan|wbc|yoshi",
        "misspell_queries": ["yoshinobu yamamota", "yoshinobu yamomoto", "yoshinubo yamamoto",
                             "yamamoto dodgers yoshinobo", "yoshinbu yamamoto", "yoshinobu yamamotto",
                             "yamomoto dodgers"],
    },
    "Victor Wembanyama": {
        "correct": "wembanyama",
        "present": r"\b(wembanyama|wembenyama|wembanyma|wembayama|wenbanyama|wembamyama|wembanyamma|wembenyamma|wembanayama|wembyama|wemby)\b",
        "bad": r"\b(wembenyama|wembanyma|wembayama|wenbanyama|wembamyama|wembanyamma|wembenyamma|wembanayama|wembyama|wemby)\b",
        "context": r"spurs|prizm|select|optic|mosaic|hoops|panini|topps|donruss|\brc\b|rookie|psa|bgs|auto|victor",
        "misspell_queries": ["victor wembenyama", "victor wembanyma", "wembayama", "wenbanyama",
                             "victor wembamyama", "wembanyamma", "wemby rookie /", "wemby prizm"],
    },
}


def correct_queries(name):
    return [f"{name} /{d}" for d in DENOMS] + [f"{name} 1/1", f"{name} auto", f"{name} psa"]

JUNK = re.compile(r"\b(lot|lots|you pick|pick your|choose|complete your set|reprint|custom|"
                  r"digital|sticker|base set|team set|break|breaks|random|mystery|repack|"
                  r"read description|damaged|crease|creased|proxy|novelty|art card|aceo)\b", re.I)
SERIAL = re.compile(r"(?<![\d/])(?:\d{1,4}\s?/\s?|#\s?/\s?|/\s?)(\d{1,4})(?![\d/])")
ONE_OF_ONE = re.compile(r"\b1\s?(?:/|of)\s?1\b|\bone of one\b|\b1-1\b", re.I)
AUTO = re.compile(r"\b(auto|autograph|autographed|signed|au)\b", re.I)
GRADE = re.compile(r"\b(psa|bgs|sgc|cgc|beckett)\s?(?:gem\s?(?:mint|mt)\s?)?(10|9\.5|9|8\.5|8|7)\b", re.I)
CARDNO = re.compile(r"#\s?([a-z]{0,4}\d{0,2}-?[a-z]{0,3}-?\d{1,3})(?![\d/])", re.I)
FAMILIES = ["bowman chrome", "bowman draft", "bowman's best", "bowman sterling", "bowman",
            "topps chrome", "topps update", "topps finest", "finest", "topps", "prizm",
            "national treasures", "flawless", "immaculate", "select", "optic", "mosaic", "hoops",
            "donruss", "chronicles", "contenders", "spectra", "revolution", "obsidian", "noir",
            "panini", "leaf", "allen & ginter", "heritage", "stadium club"]


def search(token, q, limit=200, sort="newlyListed"):
    params = {"q": q, "category_ids": CAT, "limit": str(limit), "sort": sort,
              "filter": "buyingOptions:{FIXED_PRICE|AUCTION|BEST_OFFER},itemLocationCountry:US,"
                        "priceCurrency:USD"}
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=30)).get("itemSummaries", [])
    except Exception as e:
        print(f"  search failed for {q!r}: {e}", file=sys.stderr)
        return []


def price(it):
    p = it.get("price") or it.get("currentBidPrice")
    if not p:
        return None
    v = float(p["value"])
    ship = it.get("shippingOptions") or []
    if ship and "shippingCost" in ship[0]:
        v += float(ship[0]["shippingCost"].get("value", 0))
    return v


def parse(title):
    t = title.lower()
    if JUNK.search(t):
        return None
    # Ohtani "50/50" (50 HR / 50 SB) cards are not serial numbered
    t = re.sub(r"\b50\s?(?:hr)?\s?/\s?50\s?(?:sb)?\b|\b40\s?(?:hr)?\s?/\s?40\b", " ", t)
    if ONE_OF_ONE.search(t):
        serial = 1
    else:
        nums = [int(m.group(1)) for m in SERIAL.finditer(t)]
        nums = [n for n in nums if 1 <= n <= MAX_SERIAL]
        if not nums:
            return None
        serial = min(nums)
    g = GRADE.search(t)
    grade = f"{g.group(1).upper().replace('BECKETT', 'BGS')} {g.group(2)}" if g else "Raw"
    auto = bool(AUTO.search(t))
    cn = CARDNO.search(t)
    cardno = cn.group(1).lower().replace(" ", "") if cn else None
    if cardno and cardno.isdigit() and int(cardno) == serial:
        cardno = None
    yr = re.search(r"\b(20[12]\d)\b", t)
    fam = next((f for f in FAMILIES if f in t), "other")
    return {"serial": serial, "grade": grade, "auto": auto, "cardno": cardno,
            "family": f"{yr.group(1) if yr else ''} {fam}".strip()}


def which_player(title):
    """Return (player, misspelled) or (None, False)."""
    t = title.lower()
    for name, P in PLAYERS.items():
        if not re.search(P["present"], t):
            continue
        miss = P["correct"] not in t and bool(re.search(P["bad"], t))
        if miss and not re.search(P["context"], t):
            return None, False
        if not miss and P["correct"] not in t:
            # e.g. "Ohtani" alone is fine; Yamamoto alone could be another player
            if name == "Yoshinobu Yamamoto" and not re.search(P["context"], t):
                return None, False
        return name, miss
    return None, False


def keys(c):
    base = (c["player"], c["serial"], c["auto"], c["grade"])
    yr = c["family"][:4] if c["family"][:2] == "20" else ""
    k = [("card", c["cardno"], yr) + base] if c["cardno"] else []
    return k + [("fam", c["family"]) + base]


def label(c):
    s = "1/1" if c["serial"] == 1 else f"/{c['serial']}"
    return f"{c['player']} {s}{' Auto' if c['auto'] else ''} {c['grade']}"


def scan(since_minutes=None):
    token = get_token()
    seen, rows = set(), []
    queries = []
    for name, P in PLAYERS.items():
        queries += correct_queries(P["correct"]) + P["misspell_queries"]
    for q in queries:
        for it in search(token, q):
            iid = it.get("itemId")
            if iid in seen:
                continue
            seen.add(iid)
            c = parse(it.get("title", ""))
            p = price(it)
            player, miss = which_player(it.get("title", ""))
            if c and p and player:
                c["player"] = player
                rows.append((c, it, p, miss))

    # Market comps: correctly spelled Buy It Now listings, one (cheapest) price per seller
    per_seller = {}
    for c, it, p, miss in rows:
        if miss or "FIXED_PRICE" not in it.get("buyingOptions", []):
            continue
        seller = it.get("seller", {}).get("username", it.get("itemId"))
        for k in keys(c):
            per_seller[(k, seller)] = min(per_seller.get((k, seller), p), p)
    comps = {}
    for (k, _), p in per_seller.items():
        comps.setdefault(k, []).append(p)

    now = datetime.now(timezone.utc)
    deals = []
    for c, it, p, miss in rows:
        seller = it.get("seller", {})
        fb = int(seller.get("feedbackScore", 0) or 0)
        pct = float(seller.get("feedbackPercentage", 0) or 0)
        if fb < MIN_FEEDBACK or pct < MIN_FEEDBACK_PCT:
            continue
        market, n, exact = None, 0, False
        for k in keys(c):
            v = comps.get(k, [])
            if len(v) >= MIN_COMPS:
                market, n, exact = statistics.median(v), len(v), k[0] == "card"
                break
        auction = "AUCTION" in it.get("buyingOptions", [])
        created = it.get("itemCreationDate")
        ends = it.get("itemEndDate")

        if since_minutes:
            new = created and datetime.fromisoformat(created.replace("Z", "+00:00")) >= now - timedelta(minutes=since_minutes)
            # misspelled auctions get a second alert when they are about to end
            ending = auction and ends and now + timedelta(minutes=55) <= \
                datetime.fromisoformat(ends.replace("Z", "+00:00")) < now + timedelta(minutes=55 + since_minutes)
            if not (new or (miss and ending)):
                continue

        if miss:
            if market and p > SLEEPER_MAX_RATIO * market:
                continue
            track = "SLEEPER (misspelled)"
        else:
            # only trust exact matches (same card number + serial + auto + grade)
            if auction or not exact or n < MIN_COMPS + 1 or p > DEAL_RATIO * market:
                continue
            track = "DEAL"
        deals.append({
            "track": track, "card": label(c), "title": it["title"], "price": round(p, 2),
            "auction": auction, "bids": it.get("bidCount"), "ends": ends,
            "market": round(market, 2) if market else None, "comps": n, "exact_match": exact,
            "save_pct": round(100 * (1 - p / market)) if market else None,
            "seller_feedback": f"{fb} ({pct}%)", "listed": created, "url": it.get("itemWebUrl"),
        })
    deals.sort(key=lambda d: (d["track"] != "SLEEPER (misspelled)",
                              -((d["market"] or d["price"]) - d["price"])))
    return deals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    load_env()
    deals = scan(a.since)
    if a.json:
        print(json.dumps({"deals": deals}, indent=1))
        return
    print(f"Numbered cards ({', '.join(PLAYERS)}). Finds: {len(deals)}\n")
    # exact = same card number; rough = same set and serial only
    for d in deals:
        mk = f"vs comps ${d['market']} ({d['save_pct']}% under, {d['comps']} {'exact' if d['exact_match'] else 'rough'} comps)" if d["market"] \
            else "no comps, eyeball it"
        au = f" | AUCTION, {d['bids']} bids, ends {d['ends']}" if d["auction"] else ""
        print(f"[{d['track']}] {d['card']}: ${d['price']} {mk}{au}")
        print(f"  {d['title']}\n  seller {d['seller_feedback']} | {d['url']}\n")


if __name__ == "__main__":
    main()
