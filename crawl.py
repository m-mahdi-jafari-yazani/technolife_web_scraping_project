"""
crawl.py - Technolife laptop scraper (requests + BeautifulSoup)

Scrapes the first 100 laptops of
https://www.technolife.com/category/laptop-equipment/laptop
and writes them to technolife.csv.

Run:  python crawl.py
"""
import csv
import json
import re
import time
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

BASE = "https://www.technolife.com"
LIST_URL = BASE + "/category/laptop-equipment/laptop?page={}"
TARGET = 100
DELAY = 0.5  # seconds between ANY two requests (assignment requires >= 2)
OUT_FILE = "technolife.csv"
INCLUDE_ID = True  # id is needed for the database; set False to drop the column

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
}

COLUMNS = [
    "name", "brand", "price", "old_price", "available", "rating",
    "rating_count", "cpu", "ram", "storage", "screen_size", "gpu", "weight",
]
if INCLUDE_ID:
    COLUMNS = ["id"] + COLUMNS

session = requests.Session()
session.headers.update(HEADERS)
_last_request = 0.0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def en_digits(text):
    """Persian/Arabic digits -> English digits, unify decimal/thousand separators."""
    if text is None:
        return None
    text = text.translate(_DIGITS)
    return text.replace("٫", ".").replace("٬", ",").replace("،", ",")


def clean(text):
    """Normalize whitespace / zero-width chars."""
    text = text.replace("\u200c", " ").replace("\u200f", "").replace("\u200e", "")
    return re.sub(r"\s+", " ", text).strip()


def fetch(url, retries=3):
    """GET with a polite delay between requests and simple retries."""
    global _last_request
    for attempt in range(retries):
        wait = DELAY - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = session.get(url, timeout=30)
            _last_request = time.time()
            if resp.status_code == 200:
                return resp.text
            print(f"  [warn] {resp.status_code} for {url}")
        except requests.RequestException as exc:
            _last_request = time.time()
            print(f"  [warn] {exc} for {url}")
        time.sleep(DELAY * (attempt + 1))
    return None


# --------------------------------------------------------------------------
# listing pages
# --------------------------------------------------------------------------
def collect_product_links(limit=TARGET):
    links, seen = [], set()
    page = 1
    while len(links) < limit:
        html = fetch(LIST_URL.format(page))
        if html is None:
            break
        soup = BeautifulSoup(html, "html.parser")
        new = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not re.match(r"^(https://www\.technolife\.com)?/product-\d+", href):
                continue
            if href.startswith("/"):
                href = BASE + href
            if href in seen:  # every card has 2 identical links (image + title)
                continue
            seen.add(href)
            links.append(href)
            new += 1
            if len(links) >= limit:
                break
        print(f"page {page}: {new} new links (total {len(links)})")
        if new == 0:
            break
        page += 1
    return links[:limit]


# --------------------------------------------------------------------------
# product page
# --------------------------------------------------------------------------
CART_TEXT = "افزودن به سبد خرید"
UNAVAILABLE_TEXT = "ناموجود"
SPECS_TITLE = "مشخصات فنی"
REVIEWS_TITLE = "نظرات کاربران"
PRICE_RE = re.compile(r"\s*([\d,]{4,})\s*(?:تومان)?\s*")

SPEC_LABELS = {
    "cpu_series": "سری پردازنده مرکزی",
    "cpu_model": "مدل پردازنده مرکزی",
    "ram": "ظرفیت حافظه RAM",
    "storage": "ظرفیت حافظه داخلی",
    "screen_size": "ابعاد نمایشگر",
    "gpu_maker": "سازنده پردازنده گرافیکی",
    "gpu_model": "مدل پردازنده گرافیکی",
    "weight": "وزن",
}


def to_int(text):
    return int(en_digits(text).replace(",", ""))


def is_strike(tag):
    if tag.name in ("del", "s", "strike"):
        return True
    cls = " ".join(tag.get("class", []))
    style = tag.get("style", "") or ""
    return "line-through" in cls or "line-through" in style


def parse_prices(soup):
    """Return (price, old_price) from the buy box next to the cart button."""
    cart = soup.find(string=lambda s: s and CART_TEXT in s)
    if cart is None:
        return None, None

    # walk up from the cart button until the container holds a price
    node, found = cart.parent, None
    for _ in range(10):
        node = node.parent
        if node is None:
            break
        texts = [clean(s) for s in node.stripped_strings]
        if any(PRICE_RE.fullmatch(en_digits(t)) for t in texts):
            found = node
            break
    if found is None:
        return None, None

    # climb up to 2 more levels if the strike-through / discount label sits higher
    box = found
    for _ in range(3):
        if box.find(is_strike) is not None or "تخفیف" in box.get_text():
            break
        if box.parent is None:
            break
        box = box.parent

    strike_texts = set()
    for st in box.find_all(is_strike):
        strike_texts.update(clean(s) for s in st.stripped_strings)

    price, old_price, others = None, None, []
    for raw in box.stripped_strings:
        t = clean(raw)
        m = PRICE_RE.fullmatch(en_digits(t))
        if not m or "تخفیف" in t:
            continue
        value = int(m.group(1).replace(",", ""))
        if t in strike_texts:
            old_price = value
        else:
            others.append(value)

    if others:
        price = others[0]
    # fallback if the strike-through markup was not detected
    if old_price is None and len(others) >= 2 and "تخفیف" in box.get_text():
        price, old_price = min(others[:2]), max(others[:2])
    return price, old_price


def parse_specs(strings):
    """Read 'label : value' rows, only from the technical-specs section."""
    start = 0
    candidates = [i for i, s in enumerate(strings) if s == SPECS_TITLE]
    for i in reversed(candidates):  # last heading that is followed by real rows
        if any(SPEC_LABELS["cpu_series"] in s or SPEC_LABELS["ram"] in s
               for s in strings[i:i + 60]):
            start = i
            break
    else:
        if candidates:
            start = candidates[-1]

    wanted = set(SPEC_LABELS.values())
    specs = {}
    i = start
    while i < len(strings):
        s = strings[i]
        label, value = None, None
        if ":" in s:
            left, _, right = s.partition(":")
            if clean(left) in wanted:
                label = clean(left)
                if clean(right):
                    value = clean(right)
        elif s in wanted:
            label = s
        if label and value is None:
            j = i + 1
            if j < len(strings) and strings[j] == ":":
                j += 1
            if j < len(strings):
                value = strings[j]
        if label and label not in specs and value:
            specs[label] = value
        i += 1
    return specs, start


def join_nonempty(*parts):
    parts = [p for p in parts if p]
    return " ".join(parts) if parts else None


def parse_ld_rating(soup):
    """Return (rating, count) from the Product JSON-LD in <head>, or None if absent.

    The block looks like:
      "aggregateRating": {"reviewCount": 45, "ratingValue": "4.2", ...}
    A product without reviews has no aggregateRating at all -> (0.0, 0).
    """
    found_product = False
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text())
        except (ValueError, TypeError):
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict) or item.get("@type") != "Product":
                continue
            found_product = True
            agg = item.get("aggregateRating")
            if isinstance(agg, dict):
                try:
                    rating = float(en_digits(str(agg.get("ratingValue", 0))))
                    count = int(float(en_digits(str(agg.get("reviewCount", 0)))))
                    return rating, count
                except ValueError:
                    pass
    return (0.0, 0) if found_product else None


def parse_product(url, html):
    soup = BeautifulSoup(html, "html.parser")
    ld_rating = parse_ld_rating(soup)  # must run before <script> tags are removed
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    strings = [clean(s) for s in soup.stripped_strings if clean(s)]

    row = {c: None for c in COLUMNS}

    m = re.search(r"/product-(\d+)", url)
    row["id"] = int(m.group(1)) if m else None

    h1 = soup.find("h1")
    row["name"] = clean(h1.get_text()) if h1 else None

    # brand: link of the form /brand/asus/
    for a in soup.find_all("a", href=True):
        bm = re.search(r"/brand/([^/?#]+)", a["href"])
        if bm:
            row["brand"] = unquote(bm.group(1)).lower()
            break

    # specs (also tells us where the "top" part of the page ends)
    specs, spec_start = parse_specs(strings)
    top = strings[:spec_start] if spec_start else strings

    # availability + price
    unavailable = any(UNAVAILABLE_TEXT == s or s.startswith(UNAVAILABLE_TEXT) for s in top)
    has_cart = any(CART_TEXT in s for s in top)
    row["available"] = bool(has_cart and not unavailable)
    if row["available"]:
        row["price"], row["old_price"] = parse_prices(soup)

    # rating + rating_count: primary source is the Product JSON-LD (aggregateRating)
    if ld_rating is not None:
        row["rating"], row["rating_count"] = ld_rating
    else:
        # fallback: visible text ("N دیدگاه" under the title, big number in reviews)
        count = 0
        for s in top:
            cm = re.search(r"([\d,]+)\s*دیدگاه", en_digits(s))
            if cm:
                count = int(cm.group(1).replace(",", ""))
                break
        rating = 0.0
        if count > 0:
            for i, s in enumerate(strings):
                if s == REVIEWS_TITLE:
                    for t in strings[i + 1:i + 10]:
                        if re.fullmatch(r"\d(?:\.\d+)?", en_digits(t)):
                            val = float(en_digits(t))
                            if 0 <= val <= 5:
                                rating = val
                                break
                    break
        row["rating"], row["rating_count"] = rating, count

    # technical specs (kept as text, digits converted to English like the sample)
    row["cpu"] = join_nonempty(specs.get(SPEC_LABELS["cpu_series"]),
                               specs.get(SPEC_LABELS["cpu_model"]))
    row["ram"] = specs.get(SPEC_LABELS["ram"])
    row["storage"] = specs.get(SPEC_LABELS["storage"])
    row["screen_size"] = specs.get(SPEC_LABELS["screen_size"])
    row["gpu"] = join_nonempty(specs.get(SPEC_LABELS["gpu_maker"]),
                               specs.get(SPEC_LABELS["gpu_model"]))
    row["weight"] = specs.get(SPEC_LABELS["weight"])
    for key in ("cpu", "ram", "storage", "screen_size", "gpu", "weight"):
        if row[key]:
            row[key] = en_digits(row[key])
    return row


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    links = collect_product_links(TARGET)
    print(f"collected {len(links)} product links")

    rows = []
    for n, link in enumerate(links, 1):
        print(f"[{n}/{len(links)}] {link[:90]}")
        html = fetch(link)
        if html is None:
            print("  skipped (could not fetch)")
            continue
        try:
            rows.append(parse_product(link, html))
        except Exception as exc:  # keep going even if a single page breaks
            print(f"  [error] {exc}")

    with open(OUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    print(f"saved {len(rows)} rows to {OUT_FILE}")


if __name__ == "__main__":
    main()