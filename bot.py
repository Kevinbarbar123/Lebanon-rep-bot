import html
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "43200"))
DETAIL_LIMIT = int(os.getenv("DETAIL_LIMIT", "18"))
SEND_FIRST_RUN = os.getenv("SEND_FIRST_RUN", "false").lower() in {"1", "true", "yes"}
SEND_STARTUP_MESSAGE = os.getenv("SEND_STARTUP_MESSAGE", "true").lower() in {"1", "true", "yes"}
DATABASE_PATH = os.getenv("DATABASE_PATH", "seen_listings.sqlite3")

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}

SOURCES = [
    {
        "name": "OLX Lebanon",
        "base_url": "https://www.olx.com.lb",
        "urls": [
            "https://www.olx.com.lb/properties/apartments-villas-for-rent/metn/",
            "https://www.olx.com.lb/properties/apartments-villas-for-rent/metn/q-owner/",
            "https://www.olx.com.lb/properties/apartments-villas-for-rent/metn/q-no-commission/",
        ],
    },
    {
        "name": "Dubizzle Lebanon",
        "base_url": "https://www.dubizzle.com.lb",
        "urls": [
            "https://www.dubizzle.com.lb/properties/apartments-villas-for-rent/metn/",
            "https://www.dubizzle.com.lb/properties/apartments-villas-for-rent/metn/q-owner/",
        ],
    },
    {
        "name": "OpenSooq Lebanon",
        "base_url": "https://lb.opensooq.com",
        "urls": [
            "https://lb.opensooq.com/en/property/apartments-for-rent",
            "https://lb.opensooq.com/en/property/property-for-rent",
        ],
    },
    {
        "name": "Mourjan Lebanon",
        "base_url": "https://www.mourjan.com",
        "urls": [
            "https://www.mourjan.com/lb/el-metn/apartments/rental/c-3-2/",
            "https://www.mourjan.com/lb/el-metn/apartments/rental/en/c-3-2/",
            "https://www.mourjan.com/lb/el-metn/furnished-apartments/rental/c-3-2/",
            "https://www.mourjan.com/lb/el-metn/furnished-apartments/rental/en/c-3-2/",
        ],
    },
]

TARGET_AREA_WORDS = {
    "metn",
    "matn",
    "el metn",
    "el-metn",
    "المتن",
    "mount lebanon",
    "جبل لبنان",
    "dekwaneh",
    "fanar",
    "zalqa",
    "zalka",
    "jal el dib",
    "antelia",
    "antelias",
    "sin el fil",
    "mansourieh",
    "broummana",
    "beit mery",
    "dbayeh",
    "awkar",
    "sabtiyeh",
}

AGENCY_WORDS = {
    "agency",
    "agencies",
    "agent",
    "broker",
    "brokers",
    "brokerage",
    "realtor",
    "real estate",
    "properties",
    "property consultant",
    "sarl",
    "group",
    "holdings",
    "investment",
    "investments",
    "verified business",
    "trusted seller",
    "commission",
    "office commission",
    "عموله مكتب",
    "عمولة مكتب",
    "عمولة",
    "مكتب عقاري",
    "مكاتب عقارية",
    "مكتب",
    "وسيط",
    "سمسار",
    "شركة",
}

OWNER_HINTS = {
    "owner",
    "landlord",
    "direct owner",
    "from owner",
    "by owner",
    "no commission",
    "no agency",
    "without commission",
    "private owner",
    "من المالك",
    "مالك",
    "بدون عمولة",
    "بلا عمولة",
    "بدون مكتب",
    "مباشرة",
}

@dataclass(frozen=True)
class Listing:
    listing_id: str
    source: str
    title: str
    url: str
    price: str = ""
    location: str = "Metn"
    seller: str = ""
    raw_text: str = ""
    is_probable_owner: bool = False
    reason: str = ""


def log(message: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {message}", flush=True)


def request_text(url: str, timeout: int = 20) -> str:
    last_error = None
    for attempt in range(1, 3):
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            log(f"Request failed ({attempt}/2) for {url}: {exc}")
            time.sleep(3 * attempt)
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def init_db() -> None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_listings (
                listing_id TEXT PRIMARY KEY,
                first_seen_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def already_seen(listing_id: str) -> bool:
    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_listings WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        return row is not None


def mark_seen(listings: Iterable[Listing]) -> None:
    rows = [
        (listing.listing_id, datetime.now(timezone.utc).isoformat())
        for listing in listings
    ]
    if not rows:
        return
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO seen_listings (listing_id, first_seen_at) VALUES (?, ?)",
            rows,
        )
        conn.commit()


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def is_target_area(text: str) -> bool:
    text_lower = clean_text(text).lower()
    return any(word in text_lower for word in TARGET_AREA_WORDS)


def listing_id_from_url(source: str, url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.replace("www.", "")
    path = parsed.path.rstrip("/")
    return f"{source}:{host}{path}"


def make_listing(source: dict, title: str, url: str, price: str = "", location: str = "", raw_text: str = "") -> Listing | None:
    title = clean_text(title)
    url = urljoin(source["base_url"], url)
    price = clean_text(price)
    location = clean_text(location or "Metn")
    raw_text = clean_text(raw_text)

    if len(title) < 6:
        return None
    if not is_target_area(f"{title} {location} {raw_text} {url}"):
        return None

    return Listing(
        listing_id=listing_id_from_url(source["name"], url),
        source=source["name"],
        title=title[:180],
        url=url,
        price=price,
        location=location,
        raw_text=raw_text[:1200],
    )


def parse_json_object(source: dict, obj) -> list[Listing]:
    listings: list[Listing] = []

    def walk(node):
        if isinstance(node, dict):
            item = node.get("itemOffered") if isinstance(node.get("itemOffered"), dict) else node
            possible_url = (
                node.get("url")
                or node.get("post_url")
                or node.get("href")
                or item.get("url") if isinstance(item, dict) else None
            )
            possible_title = (
                node.get("title")
                or node.get("name")
                or item.get("name") if isinstance(item, dict) else None
            )
            address = item.get("address") if isinstance(item, dict) and isinstance(item.get("address"), dict) else {}
            location = " ".join(
                clean_text(str(part))
                for part in [
                    node.get("city_label"),
                    node.get("city_reporting"),
                    node.get("nhood_label"),
                    node.get("location"),
                    node.get("area"),
                    address.get("addressRegion") if isinstance(address, dict) else "",
                    address.get("addressLocality") if isinstance(address, dict) else "",
                ]
                if part
            )
            price = clean_text(str(node.get("price_amount") or node.get("price") or ""))
            if not price and isinstance(node.get("priceSpecification"), dict):
                price = clean_text(str(node["priceSpecification"].get("price") or ""))
            raw_text = json.dumps(node, ensure_ascii=False)[:3000]
            if possible_url and possible_title:
                candidate = make_listing(source, str(possible_title), str(possible_url), price, location, raw_text)
                if candidate:
                    listings.append(candidate)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(obj)
    return listings


def parse_json_scripts(source: dict, soup: BeautifulSoup) -> list[Listing]:
    listings: list[Listing] = []
    for script in soup.find_all("script"):
        content = script.string or script.get_text() or ""
        content = content.strip()
        if not content:
            continue
        if script.get("id") != "__NEXT_DATA__" and script.get("type") != "application/ld+json":
            if '"post_url"' not in content and '"itemListElement"' not in content:
                continue
        try:
            listings.extend(parse_json_object(source, json.loads(content)))
        except json.JSONDecodeError:
            continue
    return dedupe(listings)


def parse_anchor_cards(source: dict, soup: BeautifulSoup) -> list[Listing]:
    listings: list[Listing] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        url = urljoin(source["base_url"], href)
        parsed = urlparse(url)
        if urlparse(source["base_url"]).netloc not in parsed.netloc:
            continue
        path = parsed.path.lower()
        if not any(token in path for token in ["/ad/", "/search/", "/apartments/"]):
            continue
        if any(skip in path for skip in ["/rental/", "/for-rent", "/apartments-for-rent"]):
            continue

        container = anchor
        for _ in range(4):
            if container.parent is None:
                break
            container = container.parent
        card_text = clean_text(container.get_text(" "))
        title = clean_text(anchor.get("title") or anchor.get_text(" "))
        if not title or len(title) < 8:
            title = card_text[:120]
        price_match = re.search(r"USD\s?[\d,]+|\$\s?[\d,]+|[\d,]+\s?LBP", card_text, re.I)
        price = price_match.group(0) if price_match else ""
        location_match = re.search(r"(Metn|Matn|El Metn|المتن|[A-Za-z -]+,\s*Metn)", card_text, re.I)
        location = location_match.group(0) if location_match else "Metn"
        candidate = make_listing(source, title, url, price, location, card_text)
        if candidate:
            listings.append(candidate)
    return dedupe(listings)


def parse_listing_cards(source: dict, page_html: str) -> list[Listing]:
    soup = BeautifulSoup(page_html, "lxml")
    return dedupe(parse_json_scripts(source, soup) + parse_anchor_cards(source, soup))


def dedupe(listings: list[Listing]) -> list[Listing]:
    seen: set[str] = set()
    unique: list[Listing] = []
    for listing in listings:
        if listing.listing_id in seen:
            continue
        seen.add(listing.listing_id)
        unique.append(listing)
    return unique


def classify_listing(listing: Listing) -> Listing:
    details = ""
    seller = ""
    try:
        details_html = request_text(listing.url, timeout=18)
        soup = BeautifulSoup(details_html, "lxml")
        details = clean_text(soup.get_text(" "))[:12000]
        seller = extract_seller(details)
    except Exception as exc:
        log(f"Could not fetch listing details for {listing.url}: {exc}")

    text = f"{listing.title} {listing.source} {listing.seller} {seller} {listing.raw_text} {details}".lower()
    owner_hits = sorted(word for word in OWNER_HINTS if word in text)
    agency_hits = sorted(word for word in AGENCY_WORDS if word in text)

    if owner_hits and not agency_hits:
        return Listing(**{**listing.__dict__, "seller": seller, "is_probable_owner": True, "reason": f"owner words: {', '.join(owner_hits[:4])}"})
    if agency_hits:
        return Listing(**{**listing.__dict__, "seller": seller, "is_probable_owner": False, "reason": f"agency words: {', '.join(agency_hits[:4])}"})

    title_lower = listing.title.lower()
    looks_like_ref = bool(re.search(r"\b(ref|cpf|ray|ph|id)\s*#?\s*\d+", title_lower))
    if looks_like_ref:
        return Listing(**{**listing.__dict__, "seller": seller, "is_probable_owner": False, "reason": "reference-code style listing"})

    return Listing(**{**listing.__dict__, "seller": seller, "is_probable_owner": True, "reason": "no agency markers found"})


def extract_seller(text: str) -> str:
    patterns = [
        r"([A-Za-z0-9 .&-]{2,80}) Member since",
        r"Seller description ([A-Za-z0-9 .&-]{2,80})",
        r"member_display_name['\"]?\s*[:=]\s*['\"]([^'\"]{2,80})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return clean_text(match.group(1))
    return ""


def send_telegram(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        log("Telegram variables are missing; cannot send message.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text[:3900],
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    response = requests.post(url, json=payload, timeout=25)
    response.raise_for_status()


def format_listing_message(listing: Listing) -> str:
    parts = [
        "🏠 <b>Possible owner apartment in Metn</b>",
        f"Source: {html.escape(listing.source)}",
        f"<b>{html.escape(listing.title)}</b>",
    ]
    if listing.price:
        parts.append(f"Price: {html.escape(listing.price)}")
    if listing.location:
        parts.append(f"Location: {html.escape(listing.location)}")
    if listing.seller:
        parts.append(f"Seller: {html.escape(listing.seller)}")
    parts.append(f"Why: {html.escape(listing.reason)}")
    parts.append(html.escape(listing.url))
    return "\n".join(parts)


def fetch_candidates() -> list[Listing]:
    candidates: list[Listing] = []
    for source in SOURCES:
        for search_url in source["urls"]:
            try:
                page_html = request_text(search_url)
                found = parse_listing_cards(source, page_html)
                log(f"Found {len(found)} listing candidates from {source['name']} - {search_url}")
                candidates.extend(found)
            except Exception as exc:
                log(f"Search failed for {source['name']} - {search_url}: {exc}")
    return dedupe(candidates)


def run_check(first_run: bool = False) -> None:
    candidates = fetch_candidates()
    if not candidates:
        log("No candidates found in this check.")
        return

    new_candidates = [listing for listing in candidates if not already_seen(listing.listing_id)]
    if first_run and not SEND_FIRST_RUN:
        mark_seen(new_candidates)
        log(f"First run: marked {len(new_candidates)} existing listings as seen without alerting.")
        return

    checked = []
    for listing in new_candidates[:DETAIL_LIMIT]:
        checked.append(classify_listing(listing))
        time.sleep(1)

    probable_owner_listings = [listing for listing in checked if listing.is_probable_owner]
    for listing in probable_owner_listings:
        try:
            send_telegram(format_listing_message(listing))
            log(f"Sent listing alert: {listing.url}")
            time.sleep(1)
        except Exception as exc:
            log(f"Telegram send failed for {listing.url}: {exc}")

    mark_seen(new_candidates)
    log(
        f"Check complete: {len(candidates)} total, {len(new_candidates)} new, "
        f"{len(probable_owner_listings)} probable owner listings alerted."
    )


def main() -> int:
    init_db()
    hours = round(CHECK_INTERVAL_SECONDS / 3600, 2)
    log("Metn apartment Telegram bot starting.")
    log(f"Check interval: {CHECK_INTERVAL_SECONDS} seconds ({hours} hours)")
    log("Sources: " + ", ".join(source["name"] for source in SOURCES))

    if SEND_STARTUP_MESSAGE:
        try:
            send_telegram(f"✅ Metn apartment bot is online. I will check owner-style listings every {hours:g} hours.")
        except Exception as exc:
            log(f"Startup Telegram message failed: {exc}")

    first_run = True
    while True:
        try:
            run_check(first_run=first_run)
            first_run = False
        except Exception as exc:
            log(f"Unexpected check error: {exc}")
        time.sleep(max(CHECK_INTERVAL_SECONDS, 60))


if __name__ == "__main__":
    sys.exit(main())
