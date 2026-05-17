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
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.olx.com.lb"
SEARCH_URLS = [
    "https://www.olx.com.lb/properties/apartments-villas-for-rent/metn/",
    "https://www.olx.com.lb/properties/apartments-villas-for-rent/metn/q-apartment-rent/",
    "https://www.olx.com.lb/properties/apartments-villas-for-rent/metn/q-rent-by-owner/",
]

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "1800"))
DETAIL_LIMIT = int(os.getenv("DETAIL_LIMIT", "12"))
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
    "property",
    "sarl",
    "group",
    "holdings",
    "investment",
    "investments",
    "verified business",
    "trusted seller",
    "trust lebanon",
    "c-properties",
    "concept properties",
    "mm group",
}

OWNER_HINTS = {
    "owner",
    "direct owner",
    "no commission",
    "no agency",
    "without commission",
    "private",
    "from owner",
}

@dataclass(frozen=True)
class Listing:
    listing_id: str
    title: str
    url: str
    price: str = ""
    location: str = "Metn"
    seller: str = ""
    is_probable_owner: bool = False
    reason: str = ""


def log(message: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {message}", flush=True)


def request_text(url: str, timeout: int = 25) -> str:
    last_error = None
    for attempt in range(1, 4):
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            log(f"Request failed ({attempt}/3) for {url}: {exc}")
            time.sleep(2 * attempt)
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


def listing_id_from_url(url: str) -> str:
    match = re.search(r"/ad/([^/?#]+)", url)
    if match:
        return match.group(1)
    return re.sub(r"\W+", "-", url).strip("-")[-120:]


def parse_embedded_json(soup: BeautifulSoup) -> list[Listing]:
    listings: list[Listing] = []
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return listings

    try:
        data = json.loads(script.string)
    except json.JSONDecodeError:
        return listings

    def walk(node):
        if isinstance(node, dict):
            values = node.values()
            possible_url = node.get("url") or node.get("href") or node.get("slug")
            possible_title = node.get("title") or node.get("name")
            if possible_url and possible_title and "/ad/" in str(possible_url):
                url = urljoin(BASE_URL, str(possible_url))
                price = clean_text(str(node.get("price") or node.get("displayPrice") or ""))
                location = clean_text(str(node.get("location") or node.get("area") or "Metn"))
                listings.append(
                    Listing(
                        listing_id=listing_id_from_url(url),
                        title=clean_text(str(possible_title)),
                        url=url,
                        price=price,
                        location=location,
                    )
                )
            for child in values:
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(data)
    return dedupe(listings)


def parse_listing_cards(page_html: str) -> list[Listing]:
    soup = BeautifulSoup(page_html, "lxml")
    listings = parse_embedded_json(soup)
    if listings:
        return listings

    cards: list[Listing] = []
    for anchor in soup.select('a[href*="/ad/"]'):
        href = anchor.get("href") or ""
        url = urljoin(BASE_URL, href)
        title = clean_text(anchor.get("title") or anchor.get_text(" "))
        if not title or len(title) < 8:
            continue

        container = anchor
        for _ in range(4):
            if container.parent is None:
                break
            container = container.parent
        card_text = clean_text(container.get_text(" "))
        price_match = re.search(r"USD\s?[\d,]+|\$\s?[\d,]+", card_text, re.I)
        price = clean_text(price_match.group(0)) if price_match else ""
        location_match = re.search(r"([A-Za-z ]+,\s*Metn)", card_text)
        location = clean_text(location_match.group(1)) if location_match else "Metn"
        cards.append(
            Listing(
                listing_id=listing_id_from_url(url),
                title=title[:180],
                url=url,
                price=price,
                location=location,
            )
        )
    return dedupe(cards)


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
        details_html = request_text(listing.url, timeout=20)
        soup = BeautifulSoup(details_html, "lxml")
        details = clean_text(soup.get_text(" "))[:12000]
        seller = extract_seller(details)
    except Exception as exc:
        log(f"Could not fetch listing details for {listing.url}: {exc}")

    text = f"{listing.title} {seller} {details}".lower()
    owner_hits = sorted(word for word in OWNER_HINTS if word in text)
    agency_hits = sorted(word for word in AGENCY_WORDS if word in text)

    if owner_hits and not agency_hits:
        return Listing(**{**listing.__dict__, "seller": seller, "is_probable_owner": True, "reason": f"owner words: {', '.join(owner_hits)}"})
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
    for search_url in SEARCH_URLS:
        try:
            page_html = request_text(search_url)
            found = parse_listing_cards(page_html)
            log(f"Found {len(found)} listing candidates from {search_url}")
            candidates.extend(found)
        except Exception as exc:
            log(f"Search failed for {search_url}: {exc}")
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
    log("Metn apartment Telegram bot starting.")
    log(f"Check interval: {CHECK_INTERVAL_SECONDS} seconds")

    if SEND_STARTUP_MESSAGE:
        try:
            send_telegram("✅ Metn apartment bot is online. I will check OLX every 30 minutes.")
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
