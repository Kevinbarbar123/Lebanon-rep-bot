import html
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
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
DASHBOARD_DIR = os.getenv("DASHBOARD_DIR", "offline_site")
LBP_PER_USD = float(os.getenv("LBP_PER_USD", "89500"))
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://incredible-creativity-production.up.railway.app").strip()
CHECK_LOCK = Lock()

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

AREA_ALIASES = {
    "Antelias": ["antelias", "antelia"],
    "Awkar": ["awkar", "aoukar", "aouqer"],
    "Beit Mery": ["beit mery", "beit merry", "beitmeri"],
    "Bikfaya": ["bikfaya", "bickfaya", "bekfaya"],
    "Broummana": ["broummana", "brummana", "broumana"],
    "Dbayeh": ["dbayeh", "d bayeh", "dbaieh"],
    "Dekwaneh": ["dekwaneh", "dekwane", "dekouaneh"],
    "Dora": ["dora", "daoura"],
    "Fanar": ["fanar", "furn el chebbak"],
    "Jal El Dib": ["jal el dib", "jal el-deeb", "jal el dibb"],
    "Jdeideh": ["jdeideh", "jdeide", "new jdeideh"],
    "Mansourieh": ["mansourieh", "mansouriya"],
    "Mtayleb": ["mtayleb", "mtaileb", "matayleb"],
    "Naccache": ["naccache", "naccash", "naqache"],
    "Rabieh": ["rabieh", "rabyeh"],
    "Sabtiyeh": ["sabtiyeh", "sabtieh"],
    "Sin El Fil": ["sin el fil", "sin el-fil", "sinn el fil"],
    "Zalka": ["zalka", "zalqa", "zalkaa"],
    "Zaarour": ["zaarour", "zarour"],
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
    contact: str = ""
    raw_text: str = ""
    price_usd: float | None = None
    area_sqm: float | None = None
    price_per_sqm: float | None = None
    area_name: str = "Matn Other"
    area_slug: str = "matn-other"
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS listing_history (
                listing_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                raw_price TEXT,
                price_usd REAL,
                area_sqm REAL,
                price_per_sqm REAL,
                area_name TEXT NOT NULL,
                area_slug TEXT NOT NULL,
                location TEXT,
                seller TEXT,
                contact TEXT,
                is_probable_owner INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                raw_text TEXT,
                first_seen_at TEXT NOT NULL,
                latest_seen_at TEXT NOT NULL,
                seen_count INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute("DELETE FROM listing_history WHERE is_probable_owner != 1")
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


def save_listing_history(listings: Iterable[Listing]) -> None:
    rows = []
    now = datetime.now(timezone.utc).isoformat()
    for listing in listings:
        enriched = enrich_listing(listing)
        if not enriched.is_probable_owner:
            continue
        rows.append(
            (
                enriched.listing_id,
                enriched.source,
                enriched.title,
                enriched.url,
                enriched.price,
                enriched.price_usd,
                enriched.area_sqm,
                enriched.price_per_sqm,
                enriched.area_name,
                enriched.area_slug,
                enriched.location,
                enriched.seller,
                enriched.contact,
                1 if enriched.is_probable_owner else 0,
                enriched.reason,
                enriched.raw_text,
                now,
                now,
            )
        )
    if not rows:
        return

    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.executemany(
            """
            INSERT INTO listing_history (
                listing_id, source, title, url, raw_price, price_usd, area_sqm,
                price_per_sqm, area_name, area_slug, location, seller, contact,
                is_probable_owner, reason, raw_text, first_seen_at, latest_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(listing_id) DO UPDATE SET
                source = excluded.source,
                title = excluded.title,
                url = excluded.url,
                raw_price = excluded.raw_price,
                price_usd = excluded.price_usd,
                area_sqm = excluded.area_sqm,
                price_per_sqm = excluded.price_per_sqm,
                area_name = excluded.area_name,
                area_slug = excluded.area_slug,
                location = excluded.location,
                seller = COALESCE(NULLIF(excluded.seller, ''), listing_history.seller),
                contact = COALESCE(NULLIF(excluded.contact, ''), listing_history.contact),
                is_probable_owner = excluded.is_probable_owner,
                reason = COALESCE(NULLIF(excluded.reason, ''), listing_history.reason),
                raw_text = COALESCE(NULLIF(excluded.raw_text, ''), listing_history.raw_text),
                latest_seen_at = excluded.latest_seen_at,
                seen_count = listing_history.seen_count + 1
            """,
            rows,
        )
        conn.commit()


def purge_non_owner_history() -> None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute("DELETE FROM listing_history WHERE is_probable_owner != 1")
        conn.commit()


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unknown"


def detect_area(text: str) -> tuple[str, str]:
    text_lower = clean_text(text).lower()
    for area, aliases in AREA_ALIASES.items():
        if any(alias in text_lower for alias in aliases):
            return area, slugify(area)
    return "Matn Other", "matn-other"


def parse_number(value: str) -> float | None:
    match = re.search(r"\d[\d,.]*", value or "")
    if not match:
        return None
    cleaned = match.group(0).replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_price_usd(price: str, text: str) -> float | None:
    haystack = f"{price} {text}"
    usd_match = re.search(r"(?:USD|\$)\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*(?:USD|\$)", haystack, re.I)
    if usd_match:
        return parse_number(usd_match.group(1) or usd_match.group(2) or "")

    lbp_match = re.search(r"([\d,]+(?:\.\d+)?)\s*LBP|LBP\s*([\d,]+(?:\.\d+)?)", haystack, re.I)
    if lbp_match:
        lbp = parse_number(lbp_match.group(1) or lbp_match.group(2) or "")
        if lbp:
            return round(lbp / LBP_PER_USD, 2)

    return None


def parse_area_sqm(text: str) -> float | None:
    patterns = [
        r"(?:surface area|area|floorSize)[^\d]{0,20}(\d{2,4}(?:\.\d+)?)\s*(?:m2|sqm|m²|mtk)?",
        r"(\d{2,4}(?:\.\d+)?)\s*(?:m2|sqm|m²|sq\.?\s?m)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            sqm = parse_number(match.group(1))
            if sqm and 15 <= sqm <= 1500:
                return sqm
    return None


def extract_contact(text: str) -> str:
    phone_match = re.search(r"(?:phone_number|phone|mobile)['\"]?\s*[:=]\s*['\"]?([+\d][+\d\s().-]{5,20}X{0,4})", text, re.I)
    if phone_match:
        return clean_text(phone_match.group(1))
    masked_match = re.search(r"\b\d{6,}X{2,4}\b", text)
    if masked_match:
        return masked_match.group(0)
    return ""


def enrich_listing(listing: Listing) -> Listing:
    text = f"{listing.title} {listing.location} {listing.price} {listing.seller} {listing.contact} {listing.raw_text} {listing.reason}"
    area_name, area_slug = detect_area(text)
    price_usd = listing.price_usd if listing.price_usd is not None else parse_price_usd(listing.price, text)
    area_sqm = listing.area_sqm if listing.area_sqm is not None else parse_area_sqm(text)
    price_per_sqm = listing.price_per_sqm
    if price_per_sqm is None and price_usd and area_sqm:
        price_per_sqm = round(price_usd / area_sqm, 2)
    contact = listing.contact or extract_contact(text)

    return Listing(
        **{
            **listing.__dict__,
            "area_name": area_name,
            "area_slug": area_slug,
            "price_usd": price_usd,
            "area_sqm": area_sqm,
            "price_per_sqm": price_per_sqm,
            "contact": contact,
        }
    )


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
    contact = extract_contact(text)

    if owner_hits and not agency_hits:
        return enrich_listing(Listing(**{**listing.__dict__, "seller": seller, "contact": contact, "is_probable_owner": True, "reason": f"owner words: {', '.join(owner_hits[:4])}"}))
    if agency_hits:
        return enrich_listing(Listing(**{**listing.__dict__, "seller": seller, "contact": contact, "is_probable_owner": False, "reason": f"agency words: {', '.join(agency_hits[:4])}"}))

    title_lower = listing.title.lower()
    looks_like_ref = bool(re.search(r"\b(ref|cpf|ray|ph|id)\s*#?\s*\d+", title_lower))
    if looks_like_ref:
        return enrich_listing(Listing(**{**listing.__dict__, "seller": seller, "contact": contact, "is_probable_owner": False, "reason": "reference-code style listing"}))

    return enrich_listing(Listing(**{**listing.__dict__, "seller": seller, "contact": contact, "is_probable_owner": True, "reason": "no agency markers found"}))


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


def send_telegram(text: str, reply_markup: dict | None = None) -> None:
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
    if reply_markup:
        payload["reply_markup"] = reply_markup
    response = requests.post(url, json=payload, timeout=25)
    response.raise_for_status()


def telegram_request(method: str, payload: dict | None = None, timeout: int = 30) -> dict:
    if not BOT_TOKEN:
        return {"ok": False, "description": "missing bot token"}
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = requests.post(url, json=payload or {}, timeout=timeout)
    response.raise_for_status()
    return response.json()


def command_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "Search now"}, {"text": "Dashboard"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def dashboard_link() -> str:
    if not DASHBOARD_URL:
        return ""
    if DASHBOARD_URL.startswith(("http://", "https://")):
        return DASHBOARD_URL
    return f"https://{DASHBOARD_URL}"


def setup_telegram_commands() -> None:
    if not BOT_TOKEN:
        return
    try:
        telegram_request(
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "Show bot menu"},
                    {"command": "search", "description": "Search for apartments now"},
                    {"command": "dashboard", "description": "Open dashboard"},
                ]
            },
            timeout=15,
        )
        log("Telegram commands registered: /start, /search, /dashboard")
    except Exception as exc:
        log(f"Could not register Telegram commands: {exc}")


def send_start_menu() -> None:
    link = dashboard_link()
    message = (
        "Hi. I am online and watching owner-style Metn apartment listings.\n\n"
        "Use <b>Search now</b> or /search whenever you want me to check immediately.\n"
    )
    if link:
        message += f"\nDashboard: {html.escape(link)}"
    send_telegram(message, reply_markup=command_keyboard())


def send_dashboard_link() -> None:
    link = dashboard_link()
    if link:
        send_telegram(f"Dashboard: {html.escape(link)}", reply_markup=command_keyboard())
    else:
        send_telegram("Dashboard URL is not configured yet.", reply_markup=command_keyboard())


def format_listing_message(listing: Listing) -> str:
    owner_name = listing.seller or "Unknown owner"
    phone = listing.contact or "Phone hidden"
    parts = [
        "🏠 <b>Owner apartment in Metn</b>",
        f"<b>{html.escape(listing.title)}</b>",
        f"Owner: {html.escape(owner_name)}",
        f"Phone: {html.escape(phone)}",
    ]
    if listing.price:
        parts.append(f"Price: {html.escape(listing.price)}")
    if listing.location:
        parts.append(f"Location: {html.escape(listing.location)}")
    parts.append(f"Source: {html.escape(listing.source)}")
    parts.append(html.escape(listing.url))
    return "\n".join(parts)


def fmt_money(value: float | None) -> str:
    if value is None:
        return ""
    return f"${value:,.0f}"


def fmt_number(value: float | None) -> str:
    if value is None:
        return ""
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def load_history() -> list[dict]:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM listing_history
            WHERE is_probable_owner = 1
            ORDER BY latest_seen_at DESC, first_seen_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def build_summary(listings: list[dict]) -> dict:
    priced = [row for row in listings if row.get("price_per_sqm")]
    owner_rows = [row for row in listings if row.get("is_probable_owner")]
    areas = {}
    clients = {}

    for row in listings:
        area = row.get("area_name") or "Matn Other"
        areas.setdefault(area, {"count": 0, "owner_count": 0, "price_per_sqm": [], "slug": row.get("area_slug") or slugify(area)})
        areas[area]["count"] += 1
        if row.get("is_probable_owner"):
            areas[area]["owner_count"] += 1
        if row.get("price_per_sqm"):
            areas[area]["price_per_sqm"].append(float(row["price_per_sqm"]))

        name = row.get("seller") or "Unknown owner"
        phone = row.get("contact") or "Phone hidden"
        key = f"{name} | {phone} | {row.get('source') or ''}"
        clients.setdefault(key, {"name": name, "phone": phone, "source": row.get("source") or "", "count": 0, "latest_seen_at": "", "areas": set()})
        clients[key]["count"] += 1
        clients[key]["latest_seen_at"] = max(clients[key]["latest_seen_at"], row.get("latest_seen_at") or "")
        clients[key]["areas"].add(area)

    area_rows = []
    for area, data in areas.items():
        values = data["price_per_sqm"]
        area_rows.append(
            {
                "area": area,
                "slug": data["slug"],
                "count": data["count"],
                "owner_count": data["owner_count"],
                "avg_price_per_sqm": round(sum(values) / len(values), 2) if values else None,
            }
        )
    area_rows.sort(key=lambda row: (row["area"] == "Matn Other", row["area"]))

    client_rows = []
    for data in clients.values():
        client_rows.append(
            {
                "name": data["name"],
                "phone": data["phone"],
                "source": data["source"],
                "count": data["count"],
                "latest_seen_at": data["latest_seen_at"],
                "areas": ", ".join(sorted(data["areas"])),
            }
        )
    client_rows.sort(key=lambda row: (row["name"] == "Unknown owner", -row["count"], row["name"], row["phone"]))

    return {
        "total": len(listings),
        "owner_count": len(owner_rows),
        "avg_price_per_sqm": round(sum(float(row["price_per_sqm"]) for row in priced) / len(priced), 2) if priced else None,
        "areas": area_rows,
        "clients": client_rows,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def render_listing_rows(listings: list[dict]) -> str:
    if not listings:
        return "<tr><td colspan=\"9\">No owner listings saved yet.</td></tr>"
    rows = []
    for row in listings:
        owner_name = row.get("seller") or "Unknown owner"
        phone = row.get("contact") or "Phone hidden"
        rows.append(
            "<tr>"
            f"<td>{html.escape(owner_name)}</td>"
            f"<td>{html.escape(phone)}</td>"
            f"<td><a href=\"{html.escape(row.get('url') or '')}\">{html.escape(row.get('title') or 'Open listing')}</a></td>"
            f"<td>{html.escape(row.get('area_name') or '')}</td>"
            f"<td>{html.escape(row.get('source') or '')}</td>"
            f"<td>{html.escape(row.get('raw_price') or '')}</td>"
            f"<td>{fmt_number(row.get('area_sqm'))}</td>"
            f"<td>{fmt_money(row.get('price_per_sqm'))}</td>"
            f"<td>{html.escape((row.get('latest_seen_at') or '')[:19])}</td>"
            "</tr>"
        )
    return "\n".join(rows)


def render_area_rows(summary: dict, root: str = "") -> str:
    rows = []
    for area in summary["areas"]:
        href = f"{root}areas/{area['slug']}/index.html"
        rows.append(
            "<tr>"
            f"<td><a href=\"{href}\">{html.escape(area['area'])}</a></td>"
            f"<td>{area['count']}</td>"
            f"<td>{area['owner_count']}</td>"
            f"<td>{fmt_money(area['avg_price_per_sqm'])}</td>"
            "</tr>"
        )
    return "\n".join(rows) or "<tr><td colspan=\"4\">No area data yet.</td></tr>"


def render_client_rows(summary: dict) -> str:
    rows = []
    for client in summary["clients"]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(client['name'])}</td>"
            f"<td>{html.escape(client['phone'])}</td>"
            f"<td>{html.escape(client['source'])}</td>"
            f"<td>{client['count']}</td>"
            f"<td>{html.escape(client['areas'])}</td>"
            f"<td>{html.escape(client['latest_seen_at'][:19])}</td>"
            "</tr>"
        )
    return "\n".join(rows) or "<tr><td colspan=\"6\">No owner contact history yet.</td></tr>"


def render_dashboard(title: str, listings: list[dict], summary: dict, root: str = "") -> str:
    data_json = json.dumps({"summary": summary, "listings": listings}, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: light; --ink:#172026; --muted:#66727c; --line:#d9e0e5; --paper:#f7f9fa; --accent:#126b55; --warn:#9f5b14; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family: Arial, Helvetica, sans-serif; color:var(--ink); background:var(--paper); }}
    header {{ background:#ffffff; border-bottom:1px solid var(--line); padding:24px; }}
    main {{ max-width:1240px; margin:0 auto; padding:20px; }}
    h1 {{ margin:0 0 6px; font-size:28px; }}
    h2 {{ margin:28px 0 12px; font-size:20px; }}
    a {{ color:var(--accent); }}
    .muted {{ color:var(--muted); }}
    .metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-top:18px; }}
    .metric {{ background:#fff; border:1px solid var(--line); border-radius:8px; padding:14px; }}
    .metric strong {{ display:block; font-size:24px; margin-top:4px; }}
    .toolbar {{ display:flex; gap:10px; flex-wrap:wrap; margin:16px 0; }}
    .button {{ display:inline-flex; align-items:center; min-height:36px; padding:0 12px; border-radius:8px; background:#fff; border:1px solid var(--line); text-decoration:none; }}
    .table-wrap {{ overflow:auto; background:#fff; border:1px solid var(--line); border-radius:8px; }}
    table {{ width:100%; border-collapse:collapse; min-width:860px; }}
    th, td {{ padding:10px 12px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; font-size:14px; }}
    th {{ background:#eef3f4; position:sticky; top:0; }}
    tr:last-child td {{ border-bottom:0; }}
    .pill {{ display:inline-block; padding:3px 8px; border-radius:999px; background:#e5f2ec; color:#126b55; font-size:12px; }}
    .note {{ color:var(--warn); margin-top:10px; }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(title)}</h1>
    <div class="muted">Offline dashboard generated {html.escape(summary["updated_at"][:19])} UTC</div>
    <div class="metrics">
      <div class="metric">Owner listings<strong>{summary["total"]}</strong></div>
      <div class="metric">Owner contacts<strong>{len(summary["clients"])}</strong></div>
      <div class="metric">Average USD/sqm<strong>{fmt_money(summary["avg_price_per_sqm"]) or "N/A"}</strong></div>
      <div class="metric">Areas with folders<strong>{len(summary["areas"])}</strong></div>
    </div>
  </header>
  <main>
    <div class="toolbar">
      <a class="button" href="{root}index.html">Overview</a>
      <a class="button" href="{root}data/listings.json">Listings JSON</a>
      <a class="button" href="{root}data/summary.json">Summary JSON</a>
    </div>
    <p class="note">Price per sqm is calculated only when both a usable price and surface area are found. LBP prices use {LBP_PER_USD:,.0f} LBP/USD.</p>

    <h2>Areas</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Area folder</th><th>Total</th><th>Likely owners</th><th>Avg USD/sqm</th></tr></thead>
        <tbody>{render_area_rows(summary, root)}</tbody>
      </table>
    </div>

    <h2>Owner Contact History</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Name</th><th>Phone</th><th>Source</th><th>Listings</th><th>Areas</th><th>Latest seen</th></tr></thead>
        <tbody>{render_client_rows(summary)}</tbody>
      </table>
    </div>

    <h2>Owner Listing History</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Name</th><th>Phone</th><th>Listing link</th><th>Area</th><th>Source</th><th>Price</th><th>sqm</th><th>USD/sqm</th><th>Latest seen</th></tr></thead>
        <tbody>{render_listing_rows(listings)}</tbody>
      </table>
    </div>
  </main>
  <script id="dashboard-data" type="application/json">{data_json}</script>
</body>
</html>"""


def generate_dashboard() -> None:
    purge_non_owner_history()
    dashboard_path = Path(DASHBOARD_DIR)
    data_path = dashboard_path / "data"
    areas_path = dashboard_path / "areas"
    data_path.mkdir(parents=True, exist_ok=True)
    areas_path.mkdir(parents=True, exist_ok=True)

    listings = load_history()
    summary = build_summary(listings)

    (data_path / "listings.json").write_text(json.dumps(listings, ensure_ascii=False, indent=2), encoding="utf-8")
    (data_path / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (dashboard_path / "index.html").write_text(render_dashboard("Metn Owner Apartment Dashboard", listings, summary), encoding="utf-8")

    for area in summary["areas"]:
        area_listings = [row for row in listings if row.get("area_slug") == area["slug"]]
        area_dir = areas_path / area["slug"]
        area_dir.mkdir(parents=True, exist_ok=True)
        (area_dir / "listings.json").write_text(json.dumps(area_listings, ensure_ascii=False, indent=2), encoding="utf-8")
        (area_dir / "index.html").write_text(
            render_dashboard(f"{area['area']} Listings", area_listings, summary, root="../../"),
            encoding="utf-8",
        )

    log(f"Offline dashboard generated at {dashboard_path.resolve()}")


def start_dashboard_server() -> ThreadingHTTPServer | None:
    port = os.getenv("PORT", "").strip()
    if not port:
        return None

    generate_dashboard()
    handler = partial(SimpleHTTPRequestHandler, directory=str(Path(DASHBOARD_DIR).resolve()))
    server = ThreadingHTTPServer(("0.0.0.0", int(port)), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log(f"Dashboard web server running on port {port}")
    return server


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
        generate_dashboard()
        return

    new_candidates = [listing for listing in candidates if not already_seen(listing.listing_id)]
    if first_run and not SEND_FIRST_RUN:
        checked = []
        for listing in new_candidates[:DETAIL_LIMIT]:
            checked.append(classify_listing(listing))
            time.sleep(1)
        save_listing_history(checked)
        mark_seen(new_candidates)
        generate_dashboard()
        log(f"First run: marked {len(new_candidates)} existing listings as seen without alerting.")
        return

    checked = []
    for listing in new_candidates[:DETAIL_LIMIT]:
        checked.append(classify_listing(listing))
        time.sleep(1)

    save_listing_history(checked)
    probable_owner_listings = [listing for listing in checked if listing.is_probable_owner]
    for listing in probable_owner_listings:
        try:
            send_telegram(format_listing_message(listing))
            log(f"Sent listing alert: {listing.url}")
            time.sleep(1)
        except Exception as exc:
            log(f"Telegram send failed for {listing.url}: {exc}")

    mark_seen(new_candidates)
    generate_dashboard()
    log(
        f"Check complete: {len(candidates)} total, {len(new_candidates)} new, "
        f"{len(probable_owner_listings)} probable owner listings alerted."
    )


def run_check_guarded(first_run: bool = False, triggered_by: str = "schedule") -> bool:
    if not CHECK_LOCK.acquire(blocking=False):
        log(f"Skipping {triggered_by} check because another check is already running.")
        return False
    try:
        log(f"Starting {triggered_by} check.")
        run_check(first_run=first_run)
        return True
    finally:
        CHECK_LOCK.release()


def run_manual_check() -> None:
    try:
        send_telegram("Searching now. I will update the dashboard and alert you if I find new likely owner listings.", reply_markup=command_keyboard())
    except Exception as exc:
        log(f"Could not send manual-search start message: {exc}")

    started = run_check_guarded(first_run=False, triggered_by="manual Telegram")
    if not started:
        try:
            send_telegram("A search is already running. Try again in a few minutes.", reply_markup=command_keyboard())
        except Exception as exc:
            log(f"Could not send busy message: {exc}")
        return

    try:
        send_telegram("Manual search finished. Dashboard updated.", reply_markup=command_keyboard())
    except Exception as exc:
        log(f"Could not send manual-search done message: {exc}")


def handle_telegram_message(message: dict) -> None:
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if CHAT_ID and chat_id != CHAT_ID:
        log(f"Ignoring Telegram message from unauthorized chat {chat_id}.")
        return

    text = clean_text(message.get("text") or "").lower()
    if text.startswith("/start"):
        send_start_menu()
    elif text.startswith("/dashboard") or text == "dashboard":
        send_dashboard_link()
    elif text.startswith("/search") or text in {"search now", "search", "check now", "run now"}:
        Thread(target=run_manual_check, daemon=True).start()
    else:
        send_telegram("Use /start for the menu, /search to search now, or /dashboard for the dashboard link.", reply_markup=command_keyboard())


def poll_telegram_commands() -> None:
    if not BOT_TOKEN:
        log("Telegram command polling disabled because token is missing.")
        return

    setup_telegram_commands()
    offset = None
    log("Telegram command listener started.")
    while True:
        try:
            payload = {"timeout": 25}
            if offset is not None:
                payload["offset"] = offset
            result = telegram_request("getUpdates", payload, timeout=35)
            for update in result.get("result", []):
                offset = int(update["update_id"]) + 1
                if "message" in update:
                    handle_telegram_message(update["message"])
        except Exception as exc:
            log(f"Telegram polling error: {exc}")
            time.sleep(10)


def start_telegram_listener() -> None:
    thread = Thread(target=poll_telegram_commands, daemon=True)
    thread.start()


def main() -> int:
    init_db()
    start_dashboard_server()
    start_telegram_listener()
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
        if run_check_guarded(first_run=first_run, triggered_by="scheduled"):
            first_run = False
        time.sleep(max(CHECK_INTERVAL_SECONDS, 60))


if __name__ == "__main__":
    sys.exit(main())
