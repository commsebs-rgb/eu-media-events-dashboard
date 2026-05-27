#!/usr/bin/env python3
"""
EU media events dashboard scraper.

Official sources covered:
- POLITICO Europe events: https://www.politico.eu/events/
- Euractiv Events: https://events.euractiv.com/
- Euronews Events: https://events.euronews.com/events plus discovered event microsites
- The Parliament Magazine Events: https://events.theparliamentmagazine.eu/
- Logos / European Defence & Security Conference: https://defencesecurityconference.eu/

Output:
  data/events.json

Default date window:
  Full 2026 calendar year. The dashboard then shows upcoming events first and past events separately.
  Change with START_DATE and END_DATE environment variables in GitHub Actions.

Important:
  Sponsor detection is best-effort. When the official page labels sponsors with text
  such as "Sponsor", "Presented by", "supported by", or image alt text, the scraper
  captures it automatically. Image-only logos without usable alt text still need
  manual review in data/manual_sponsors.csv.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import dateparser
import requests
from bs4 import BeautifulSoup, Tag

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUTPUT_FILE = DATA_DIR / "events.json"
MANUAL_SPONSORS_FILE = DATA_DIR / "manual_sponsors.csv"

START_DATE = date.fromisoformat(os.getenv("START_DATE", "2026-01-01"))
END_DATE = date.fromisoformat(os.getenv("END_DATE", "2026-12-31"))

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (compatible; EUEventDashboard/4.0; +https://github.com/commsebs-rgb/eu-media-events-dashboard)",
)
REQUEST_DELAY_SECONDS = float(os.getenv("REQUEST_DELAY_SECONDS", "0.8"))
TIMEOUT_SECONDS = int(os.getenv("TIMEOUT_SECONDS", "30"))

MONTH_RE = (
    r"January|February|March|April|May|June|July|August|September|October|"
    r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
DATE_PATTERNS = [
    re.compile(rf"\b\d{{1,2}}\s*[-–]\s*\d{{1,2}}\s+(?:{MONTH_RE})\s+\d{{4}}\b", re.I),
    re.compile(rf"\b(?:{MONTH_RE})\s+\d{{1,2}}\s*[-–]\s*\d{{1,2}},?\s+\d{{4}}\b", re.I),
    re.compile(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b"),
    re.compile(rf"\b(?:{MONTH_RE})\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b", re.I),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTH_RE})\s+\d{{4}}\b", re.I),
    re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b"),
    re.compile(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,?\s+(?:" + MONTH_RE + r")\s+\d{1,2},?\s+\d{4}\b", re.I),
]

TIME_RE = re.compile(r"\b\d{1,2}[:.]\d{2}\s*(?:am|pm|AM|PM|CET|CEST|BST)?\b")
MONTH_DAY_NO_YEAR_RE = re.compile(rf"\b(?:{MONTH_RE})\s+\d{{1,2}}(?:st|nd|rd|th)?\b", re.I)

CATEGORY_HINTS = [
    "Policy Events", "Summits", "PM+ Talks", "Awards", "Health and Consumers",
    "Energy and Environment", "Economy and Jobs", "Agriculture and Food", "Technology",
    "Digital", "Transport", "Politics", "Economy", "Health", "Energy", "Environment",
    "Digital & Media", "Mobility", "Sustainability", "Defense", "Trade", "Financial Services",
]

LOCATION_HINTS = [
    "Brussels", "Strasbourg", "Online", "Helsinki", "Nicosia", "Prague", "Valetta",
    "Valletta", "Warsaw", "Bologna", "London", "Paris", "Berlin", "Rome", "Belgium",
]

NAV_NOISE = {
    "home", "program", "programme", "schedule", "panellists", "panelists", "speakers", "partners", "partner with us", "about",
    "about us", "contact", "privacy", "terms", "legal", "log in", "register", "learn more",
    "language", "resource center", "newsletter", "subscribe", "news", "videos", "media",
    "last edition", "get in touch", "calendar", "sign up", "all events", "upcoming events",
    "past events", "become a sponsor", "sponsorship opportunities", "image", "share this event",
    "add to calendar", "google calendar", "outlook calendar", "apple calendar", "yahoo calendar",
    "ics export", "cookie policy", "copyright", "i accept", "search", "filter", "all", "next",
    "previous", "more events", "contact us", "register here", "[email protected]", "email protected",
    "start date", "end date", "event details", "status", "venue", "location", "category",
    "days", "hours", "min", "sec", "on the same topic",
}

SPONSOR_HEADING_RE = re.compile(
    r"\b(sponsors?|partners?|supporters?|with the support of|presented by|supported by|"
    r"in partnership with|media partners?|institutional partners?|knowledge partners?|platinum partner|"
    r"gold partner|silver partner|strategic partner|organised by|organized by|co-organised by|co-organized by|"
    r"co-hosted by|hosted by|brought to you by|event partner|content partner|commercial partner)\b",
    re.I,
)

SPONSOR_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Presented\s+By\s+([^\n\.;|]{2,120})", re.I), "Presented by"),
    (re.compile(r"Sponsored\s+by\s+([^\n\.;|]{2,120})", re.I), "Sponsored by"),
    (re.compile(r"Supported\s+by\s+([^\n\.;|]{2,120})", re.I), "Supported by"),
    (re.compile(r"This\s+panel\s+is\s+supported\s+by\s+([^\n\.;|]{2,120})", re.I), "Panel supported by"),
    (re.compile(r"with\s+the\s+support\s+of\s+([^\n\.;|]{2,120})", re.I), "With the support of"),
    (re.compile(r"in\s+partnership\s+with\s+([^\n\.;|]{2,120})", re.I), "In partnership with"),
    (re.compile(r"(?:organised|organized)\s+by\s+(?:the\s+)?([^\n\.;|:]{2,140})", re.I), "Organised by"),
    (re.compile(r"(?:co[- ]?organised|co[- ]?organized)\s+by\s+(?:the\s+)?([^\n\.;|:]{2,140})", re.I), "Co-organised by"),
    (re.compile(r"(?:co[- ]?hosted|hosted)\s+by\s+(?:the\s+)?([^\n\.;|:]{2,140})", re.I), "Hosted by"),
    (re.compile(r"brought\s+to\s+you\s+by\s+([^\n\.;|]{2,120})", re.I), "Brought to you by"),
]

PUBLIC_ORG_TERMS = [
    "european parliament", "european commission", "council of the eu", "council of european union",
    "dg ", "directorate-general", "ministry", "minister", "government", "permanent representation",
    "united nations", "world health organization", "who", "oecd", "nato", "mep", "commissioner",
    "euronews", "politico", "euractiv", "the parliament", "parliament magazine",
    "european union", "cinea", "european investment bank", "eib", "world bank",
]

GENERIC_IMAGE_ALTS = {
    "image", "logo", "event", "events", "parliament events", "euronews events home - calendar page",
    "star_divider", "previous", "next", "speaker", "speakers", "gallery", "slide 1 of 25",
    "www", "www.", "ship", "ships", "partnership", "sponsorship", "sponsor", "partners", "website",
}

TITLE_NOISE = {"events calendar", "parliament events", "euronews events", "home", "calendar", "panellists", "panelists", "schedule", "contact", "on the same topic", "start date", "end date", "event details", "status", "location", "venue", "category", "days", "hours", "min", "sec"}

KNOWN_PRIVATE_ENTITIES = {
    "visa", "tiktok", "tik tok", "sanofi", "bayer", "uber", "qualcomm", "sobi", "repsol",
    "horse technologies", "fuelseurope", "avio aero", "ge aerospace", "euturbines",
    "international copper association europe", "transport & environment", "norsk hydro", "microsoft",
    "philips", "besins", "chiesi", "corteva", "automotive coalition for europe", "adpa", "airc", "ame", "egea", "figiefa", "insurance europe", "repsol technology lab", "horse technologies", "horse powertrain",
    "chevron", "theon group", "theon", "cyprus chamber of commerce and industry", "aegean", "agean", "getoffers.com", "getoffers", "cleantech for see", "cleantech south east europe", "locatee", "cyprus chamber", "medtech europe", "efpia",
}

# Targeted official-event safeguards for pages whose HTML contains multiple dates
# from agenda items, related events, or tracking widgets. The scraper still crawls
# the official page every 24h for title/venue/sponsor changes, but these guards
# prevent unrelated dates from overriding the event-level date.
KNOWN_EVENT_FIXES = [
    {
        "url_contains": ["health-care-summit-2026", "healthcare-summit-2026"],
        "title_contains": ["health care summit", "healthcare summit"],
        "title": "POLITICO Health Care Summit 2026",
        "date_text": "1–2 December 2026",
        "date": "2026-12-01",
        "end_date": "2026-12-02",
        "time_text": "",
        "city": "Brussels",
        "venue": "",
        "category": "Health",
        "sponsors": ["MedTech Europe", "EFPIA"],
        "confidence": "high",
    },
    {
        "url_contains": ["energy-climate-forum-2026"],
        "title_contains": ["energy climate forum", "energy & climate forum"],
        "title": "POLITICO’s Energy & Climate Forum",
        "date_text": "1 June 2026",
        "date": "2026-06-01",
        "end_date": "",
        "time_text": "",
        "city": "Brussels + online",
        "venue": "",
        "category": "Energy and Climate, Forums",
        "sponsors": [],
        "confidence": "medium",
    },
]


def event_fix_for(title: str = "", url: str = "") -> Optional[dict]:
    haystack_url = (url or "").lower()
    haystack_title = clean(title).lower()
    for fix in KNOWN_EVENT_FIXES:
        url_ok = any(token in haystack_url for token in fix.get("url_contains", []))
        title_ok = any(token in haystack_title for token in fix.get("title_contains", []))
        if url_ok or title_ok:
            return fix
    return None


@dataclass
class Sponsor:
    name: str
    role: str = "Sponsor / partner"
    source_url: str = ""
    extraction: str = "auto"
    confidence: str = "medium"


@dataclass
class Event:
    organization: str
    title: str
    date: str
    date_text: str = ""
    end_date: str = ""
    time_text: str = ""
    city: str = ""
    venue: str = ""
    category: str = ""
    url: str = ""
    description: str = ""
    sponsors: list[Sponsor] = field(default_factory=list)
    confidence: str = "medium"
    source_official: bool = True

    @property
    def event_id(self) -> str:
        key = "|".join([self.organization, self.title.lower(), self.date, self.url])
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def apply_known_event_fix(event: Event) -> None:
    """Apply narrow, official-page date/sponsor safeguards for known tricky pages."""
    fix = event_fix_for(event.title, event.url)
    if not fix:
        return
    event.title = fix.get("title", event.title) or event.title
    event.date = fix.get("date", event.date) or event.date
    event.date_text = fix.get("date_text", event.date_text) or event.date_text
    event.end_date = fix.get("end_date", event.end_date) or event.end_date
    event.time_text = fix.get("time_text", event.time_text) if "time_text" in fix else event.time_text
    event.city = fix.get("city", event.city) or event.city
    event.venue = fix.get("venue", event.venue) if "venue" in fix else event.venue
    event.category = fix.get("category", event.category) or event.category
    event.confidence = fix.get("confidence", event.confidence) or event.confidence
    for name in fix.get("sponsors", []):
        event.sponsors.append(Sponsor(
            name=normalize_company_name(name),
            role="Partner / sponsor",
            source_url=event.url,
            extraction="official-page safeguard",
            confidence="high",
        ))


def apply_known_event_fixes(events: list[Event]) -> None:
    for event in events:
        apply_known_event_fix(event)


class Scraper:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en,en-GB;q=0.9"})
        self.cache: dict[str, str] = {}

    def get(self, url: str) -> Optional[str]:
        if url in self.cache:
            return self.cache[url]
        try:
            resp = self.session.get(url, timeout=TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            resp.raise_for_status()
            self.cache[url] = resp.text
            return resp.text
        except Exception as exc:
            print(f"[warn] Could not fetch {url}: {exc}")
            return None

    def soup(self, url: str) -> Optional[BeautifulSoup]:
        html = self.get(url)
        if not html:
            return None
        return BeautifulSoup(html, "lxml")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def normalize_company_name(name: str) -> str:
    name = clean(name)
    name = re.sub(r"^(and|by|with|from|the)\s+", "", name, flags=re.I)
    # Remove common image filename debris but keep brand words.
    name = re.sub(r"\.(svg|png|jpe?g|webp)$", "", name, flags=re.I)
    name = re.sub(r"[_+]+", " ", name)
    name = re.sub(r"\s+logo\b|\blogo\s+", " ", name, flags=re.I)
    name = re.sub(r"\s{2,}", " ", name).strip()
    name = re.split(r"\s+(?:will|who|where|when|date|time|speaking at|close|register|learn more|convenes|convened)\b", name, 1, flags=re.I)[0]
    name = re.split(r"[\.;\n|•]", name)[0]
    name = re.sub(r"\s{2,}", " ", name).strip(" ,:-–—")
    canon = {
        "theon": "THEON Group",
        "theon group": "THEON Group",
        "aegean": "AEGEAN",
        "agean": "AEGEAN",
        "getoffers": "GetOffers.com",
        "getoffers com": "GetOffers.com",
        "get offers": "GetOffers.com",
        "locatee": "LOCATEE",
        "medtech europe": "MedTech Europe",
        "medtecheurope": "MedTech Europe",
        "efpia": "EFPIA",
        "cleantech for see": "Cleantech for SEE",
        "cyprus chamber": "Cyprus Chamber of Commerce & Industry",
        "cyprus chamber of commerce industry": "Cyprus Chamber of Commerce & Industry",
    }
    key = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    return canon.get(key, name)


def title_from_image_src(src: str) -> str:
    """Best-effort sponsor name from logo filenames when image alt text is empty."""
    if not src:
        return ""
    # srcset may contain several URLs and sizes; keep the first real URL-like token.
    src = clean(src).split(",")[0].strip().split(" ")[0]
    stem = src.split("?")[0].rstrip("/").split("/")[-1]
    stem = re.sub(r"%20", " ", stem, flags=re.I)
    stem = re.sub(r"\.(svg|png|jpe?g|webp|avif)$", "", stem, flags=re.I)
    # Remove common WordPress/image-processing suffixes without destroying the brand name.
    stem = re.sub(r"(?:logo|logos|sponsor|partner|partners|colour|color|white|black|transparent|horizontal|vertical|new|final|copy|scaled|cropped|retina|light|dark)", " ", stem, flags=re.I)
    stem = re.sub(r"(?:-\d+x\d+|_\d+x\d+|\b\d{2,}\b)", " ", stem, flags=re.I)
    stem = re.sub(r"[-_+]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    if stem.lower() in {"www", "www.", "ship", "ships", "partnership", "sponsorship", "sponsor", "partners", "partner", "website"}:
        return ""
    # Some sites name files with useful brand strings, e.g. chevron-logo.png.
    return normalize_company_name(stem.title() if stem.islower() else stem)


def clean_lines(soup: BeautifulSoup) -> list[str]:
    clone = BeautifulSoup(str(soup), "lxml")
    for tag in clone(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = clone.get_text("\n")
    return [clean(line) for line in text.splitlines() if clean(line)]


def first_date_text(text: str) -> str:
    text = clean(text)
    for pattern in DATE_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(0)
    return ""

def infer_date_text_with_year(text: str, url: str = "", title: str = "", *, allow_url_title_year: bool = True) -> str:
    """Find a date even when a page writes only 'June 1' or '18-19 November'.

    Important safeguard: some event pages contain unrelated cards/agenda items with
    month/day dates and the event title or URL contains a year such as 2026. In those
    cases we must not blindly attach the title year to an unrelated date. For strict
    extraction paths, pass allow_url_title_year=False so a year must appear in the
    same text snippet as the date.
    """
    text = clean(text)
    explicit = first_date_text(text)
    if explicit:
        return explicit

    local_year = re.search(r"\b20\d{2}\b", text[:700])
    if local_year:
        year = local_year.group(0)
    elif allow_url_title_year:
        context_year = re.search(r"\b20\d{2}\b", " ".join([url, title]))
        year = context_year.group(0) if context_year else "2026"
    else:
        return ""

    # 18-19 November
    m = re.search(rf"\b(\d{{1,2}})\s*[-–]\s*\d{{1,2}}\s+({MONTH_RE})\b", text, re.I)
    if m:
        return f"{m.group(1)} {m.group(2)} {year}"
    # November 18-19
    m = re.search(rf"\b({MONTH_RE})\s+(\d{{1,2}})\s*[-–]\s*\d{{1,2}}\b", text, re.I)
    if m:
        return f"{m.group(1)} {m.group(2)} {year}"
    # June 1
    m = MONTH_DAY_NO_YEAR_RE.search(text)
    if m:
        return f"{m.group(0)} {year}"
    return ""



def all_date_texts(text: str) -> list[str]:
    out: list[str] = []
    for pattern in DATE_PATTERNS:
        for m in pattern.finditer(text):
            value = m.group(0)
            if value not in out:
                out.append(value)
    return out


def parse_iso_date(text: str) -> str:
    if not text:
        return ""
    t = clean(text)
    # Ranges such as "18-19 November 2026" should use the first day for sorting.
    t = re.sub(rf"\b(\d{{1,2}})\s*[-–]\s*\d{{1,2}}\s+({MONTH_RE})\s+(\d{{4}})\b", r"\1 \2 \3", t, flags=re.I)
    t = re.sub(rf"\b({MONTH_RE})\s+(\d{{1,2}})\s*[-–]\s*\d{{1,2}},?\s+(\d{{4}})\b", r"\1 \2 \3", t, flags=re.I)
    # Drop weekday and keep actual date/time string.
    t = re.sub(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+", "", t, flags=re.I)
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(t.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    parsed = dateparser.parse(
        t,
        languages=["en"],
        settings={"DATE_ORDER": "DMY", "PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False},
    )
    return parsed.date().isoformat() if parsed else ""


def in_range(date_iso: str) -> bool:
    if not date_iso:
        return False
    try:
        d = date.fromisoformat(date_iso)
    except ValueError:
        return False
    return START_DATE <= d <= END_DATE


def parse_end_iso_date(text: str) -> str:
    """Parse the end date from common date ranges, otherwise return empty."""
    if not text:
        return ""
    t = clean(text)
    m = re.search(rf"\b(\d{{1,2}})\s*[-–]\s*(\d{{1,2}})\s+({MONTH_RE})\s+(\d{{4}})\b", t, re.I)
    if m:
        return parse_iso_date(f"{m.group(2)} {m.group(3)} {m.group(4)}")
    m = re.search(rf"\b({MONTH_RE})\s+(\d{{1,2}})\s*[-–]\s*(\d{{1,2}}),?\s+(\d{{4}})\b", t, re.I)
    if m:
        return parse_iso_date(f"{m.group(1)} {m.group(3)} {m.group(4)}")
    return ""


def is_good_event_date_text(text: str) -> bool:
    if not text:
        return False
    low = clean(text).lower()
    # These often surround unrelated dates on media/event pages.
    if any(bad in low for bad in [
        "published", "updated", "posted", "copyright", "privacy policy", "previous edition",
        "last edition", "related events", "you might also love", "on the same topic",
        "latest news", "newsletters", "article", "story", "replay",
    ]):
        return False
    return bool(parse_iso_date(infer_date_text_with_year(text) or first_date_text(text) or text))


def jsonld_event_start_dates(soup: BeautifulSoup) -> list[str]:
    dates: list[str] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text(" ")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop(0)
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("@graph"), list):
                stack.extend(item["@graph"])
            types = item.get("@type", "")
            if isinstance(types, list):
                is_event = any(str(t).lower() == "event" for t in types)
            else:
                is_event = str(types).lower() == "event"
            if is_event and item.get("startDate"):
                dates.append(clean(str(item.get("startDate"))))
    return dates


def structured_date_texts(soup: BeautifulSoup) -> list[str]:
    out: list[str] = []
    out.extend(jsonld_event_start_dates(soup))
    selectors = [
        ("meta", {"property": "event:start_time"}),
        ("meta", {"property": "event:start_date"}),
        ("meta", {"name": "event:start_time"}),
        ("meta", {"name": "event:start_date"}),
        ("meta", {"itemprop": "startDate"}),
    ]
    for name, attrs in selectors:
        for tag in soup.find_all(name, attrs=attrs):
            value = clean(tag.get("content", ""))
            if value:
                out.append(value)
    for tag in soup.find_all(["time", "span", "div"], attrs={"datetime": True}):
        value = clean(tag.get("datetime", ""))
        if value:
            out.append(value)
    seen = set()
    deduped = []
    for value in out:
        key = value.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(value)
    return deduped



def _norm_title_tokens(text: str) -> set[str]:
    text = clean(text).lower()
    text = re.sub(r"\b(20\d{2}|politico|euractiv|euronews|parliament|magazine|events?|summit|forum|conference|roundtable|the|and|with|from|under|live|europe|eu)\b", " ", text)
    return {w for w in re.findall(r"[a-z0-9]{4,}", text) if w not in {"register", "interest", "official", "page"}}


def _title_matches_event(candidate: str, title: str, url: str = "") -> bool:
    """Return True when a structured Event/name or time block appears to be the current page event."""
    c = clean(candidate)
    t = clean(title)
    if not c or not t:
        return False
    ck = re.sub(r"\W+", "", c.lower())
    tk = re.sub(r"\W+", "", t.lower())
    if len(ck) >= 10 and len(tk) >= 10 and (ck in tk or tk in ck):
        return True
    ct = _norm_title_tokens(c)
    tt = _norm_title_tokens(t)
    if ct and tt and len(ct & tt) >= max(1, min(3, int(0.55 * min(len(ct), len(tt))))):
        return True
    slug = title_from_url_slug(url)
    if slug and c and _title_matches_event(c, slug, ""):
        return True
    return False


def jsonld_event_start_dates_matching(soup: BeautifulSoup, title: str, url: str = "") -> list[str]:
    """JSON-LD dates, but only for Event objects that match the current detail page."""
    dates: list[str] = []
    all_event_items: list[dict] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text(" ")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop(0)
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("@graph"), list):
                stack.extend(item["@graph"])
            types = item.get("@type", "")
            is_event = any(str(t).lower() == "event" for t in types) if isinstance(types, list) else str(types).lower() == "event"
            if is_event:
                all_event_items.append(item)

    for item in all_event_items:
        start = clean(str(item.get("startDate", "")))
        if not start:
            continue
        name = clean(str(item.get("name", "")))
        item_url = clean(str(item.get("url", "")))
        same_url = bool(item_url and url and item_url.rstrip("/") == url.rstrip("/"))
        same_title = bool(name and _title_matches_event(name, title, url))
        # If there is only one Event object, accept it; otherwise require title or URL match.
        if same_url or same_title or len(all_event_items) == 1:
            dates.append(start)
    return dates


def structured_date_texts_for_event(soup: BeautifulSoup, title: str, url: str, organization: str) -> list[str]:
    """Return event-level date candidates, avoiding dates from related cards/lists."""
    out: list[str] = []
    org_low = organization.lower()

    out.extend(jsonld_event_start_dates_matching(soup, title, url))

    # These metadata fields are normally page-level, not related-card level.
    selectors = [
        ("meta", {"property": "event:start_time"}),
        ("meta", {"property": "event:start_date"}),
        ("meta", {"name": "event:start_time"}),
        ("meta", {"name": "event:start_date"}),
        ("meta", {"itemprop": "startDate"}),
    ]
    for name, attrs in selectors:
        for tag in soup.find_all(name, attrs=attrs):
            value = clean(tag.get("content", ""))
            if value:
                out.append(value)

    # Generic <time datetime> tags are dangerous on POLITICO because related cards and
    # agenda snippets may contain their own dates. Only accept them if their immediate
    # block contains the current event title and does not look like a related-events block.
    for tag in soup.find_all(["time", "span", "div"], attrs={"datetime": True}):
        value = clean(tag.get("datetime", ""))
        if not value:
            continue
        parent_text = ""
        parent = tag
        for _ in range(4):
            parent = parent.find_parent() if isinstance(parent, Tag) else None
            if not parent:
                break
            parent_text = clean(parent.get_text(" ", strip=True))
            if len(parent_text) > 20:
                break
        parent_low = parent_text.lower()
        if any(bad in parent_low for bad in ["related events", "more events", "past events", "upcoming events", "on the same topic", "latest news"]):
            continue
        if org_low == "politico":
            if _title_matches_event(parent_text, title, url):
                out.append(value)
        else:
            out.append(value)

    seen = set()
    deduped = []
    for value in out:
        key = value.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(value)
    return deduped


def date_text_near_title(lines: list[str], title: str, url: str = "", *, allow_url_title_year: bool = True) -> str:
    if not title:
        return ""
    title_low = re.sub(r"\W+", " ", title.lower()).strip()
    stop_re = re.compile(r"^(related events|more events|latest|news|speakers|partners|sponsors|programme|program|on the same topic|advertisement)$", re.I)
    for i, line in enumerate(lines):
        line_low = re.sub(r"\W+", " ", line.lower()).strip()
        if title_low and (title_low in line_low or line_low in title_low):
            window_lines = []
            for j in range(i, min(i + 22, len(lines))):
                if j > i and stop_re.search(lines[j]):
                    break
                window_lines.append(lines[j])
            window = " ".join(window_lines)
            if any(bad in window.lower() for bad in ["related events", "more events", "on the same topic", "latest news"]):
                continue
            value = infer_date_text_with_year(window, url, title, allow_url_title_year=allow_url_title_year)
            if value and is_good_event_date_text(window):
                return value
    return ""


def select_event_date_text(soup: BeautifulSoup, lines: list[str], full_text: str, title: str, url: str, organization: str) -> str:
    fix = event_fix_for(title, url)
    if fix:
        return fix.get("date_text", "")

    org_low = organization.lower()

    # 1) Event-level structured dates are most reliable, but they must belong to this
    # event page. This avoids pulling dates from related cards or agenda snippets.
    for value in structured_date_texts_for_event(soup, title, url, organization):
        iso = parse_iso_date(value)
        if iso and in_range(iso):
            return value

    # 2) Explicit labels in the event page body. For POLITICO, only accept labels where
    # the year appears in the same snippet, unless a known-event safeguard applies.
    labelled = next_line_after(lines, ["Start Date", "When", "Date", "Date & Time", "Event date"])
    value = infer_date_text_with_year(labelled, url, title, allow_url_title_year=(org_low != "politico"))
    if value and parse_iso_date(value) and in_range(parse_iso_date(value)):
        return value

    # 3) Search a small window around the page title/header, not the entire page.
    near = date_text_near_title(lines, title, url, allow_url_title_year=(org_low != "politico"))
    if near and parse_iso_date(near) and in_range(parse_iso_date(near)):
        return near

    # 4) Final fallback. For POLITICO, deliberately do not infer dates from page-wide
    # text. POLITICO pages frequently include other event cards; a page title containing
    # “2026” can otherwise turn unrelated September 24 / 8:15 snippets into fake 2026 dates.
    if org_low == "politico":
        return ""

    fallback = infer_date_text_with_year(full_text[:3500], url, title)
    if fallback and parse_iso_date(fallback) and in_range(parse_iso_date(fallback)):
        return fallback
    return ""


def category_from_text(text: str) -> str:
    hits = [c for c in CATEGORY_HINTS if re.search(rf"\b{re.escape(c)}\b", text, re.I)]
    return ", ".join(dict.fromkeys(hits[:3]))


def city_from_text(text: str) -> str:
    for city in LOCATION_HINTS:
        if re.search(rf"\b{re.escape(city)}\b", text, re.I):
            return city
    return ""


def is_bad_title(title: str) -> bool:
    low = clean(title).lower()
    if not low or low in NAV_NOISE or low in TITLE_NOISE:
        return True
    if len(low) < 6 or len(low) > 220:
        return True
    if low.startswith(("image:", "http", "www.")):
        return True
    if SPONSOR_HEADING_RE.fullmatch(low.strip(" :")):
        return True
    return False


def is_public_or_media_org(label: str) -> bool:
    low = normalize_company_name(label).lower()
    return any(term in low for term in PUBLIC_ORG_TERMS)


def is_plausible_sponsor(label: str, require_private: bool = True) -> bool:
    label = normalize_company_name(label)
    if not label or len(label) < 2 or len(label) > 100:
        return False
    low = label.lower()
    city_noise = {c.lower() for c in LOCATION_HINTS} | {"brussels", "online", "nicosia", "prague", "renaissance hotel"}
    hard_noise = NAV_NOISE | GENERIC_IMAGE_ALTS | TITLE_NOISE | {"register here", "image", "source", "open", "event details", "start date", "end date", "www", "www.", "ship", "ships", "sponsorship", "partnership", "partner", "partners", "sponsor", "sponsors", "website"}
    if low in hard_noise or low in city_noise:
        return False
    if "@" in label or "email protected" in low or "[email" in low:
        return False
    if any(x in low for x in ["cookie", "privacy", "terms", "copyright", "contact us", "sponsorship opportunities", "google maps", "add to calendar", "want to partner", "click to find", "register here"]):
        return False
    # Reject sentence fragments that sometimes appear near sponsor blocks.
    if re.search(r"\b(will|can|should|would|explore|address|discuss|programme|schedule|register|interested|livestream|appear here|same topic)\b", low):
        return False
    if SPONSOR_HEADING_RE.fullmatch(label):
        return False
    if len(label.split()) > 12:
        return False
    # Reject shouty CTA/navigation labels while still allowing known acronyms such as ACE, ADPA, AIRC.
    if label.isupper() and " " in label and low not in KNOWN_PRIVATE_ENTITIES:
        return False
    # Filter public institutions and the media hosts themselves. Keep private trade associations,
    # coalitions and industry bodies because the dashboard treats co-organisers/partners as sponsors.
    if require_private and is_public_or_media_org(label):
        return False
    company_signals = [
        "ltd", "limited", "gmbh", "sa", "ag", "nv", "inc", "corp", "company", "group",
        "technologies", "technology", "europe", "foundation", "association", "alliance", "coalition",
        "forum", "federation", "institute", "council", "union", "industries", "industry", "chamber", "commerce", "energy", "bank",
        "aerospace", "pharma", "mobility", "systems", "power", "fuels", "copper", "transport",
        "automotive", "payments", "data", "parts", "insurance", "garage", "equipment", "aftermarket",
    ]
    if low in KNOWN_PRIVATE_ENTITIES:
        return True
    if any(sig in low for sig in company_signals):
        return True
    if label.isupper() and 2 <= len(label) <= 12:
        return True
    # Allow strong brand-looking one or two word names such as Visa, Bayer, Sanofi, Sobi.
    if len(label.split()) <= 2 and re.match(r"^[A-Z][A-Za-z0-9&'’.-]{2,}(?:\s+[A-Z][A-Za-z0-9&'’.-]{2,})?$", label):
        return True
    return False


def surrounding_text(tag: Tag, max_chars: int = 1200) -> str:
    # Prefer a containing article/card/row where event pages put date and sponsor nearby.
    for selector in ["article", "tr", "li", "section", "div"]:
        parent = tag.find_parent(selector)
        if parent:
            text = clean(parent.get_text(" ", strip=True))
            if first_date_text(text) or SPONSOR_HEADING_RE.search(text):
                return text[:max_chars]
    parts = [clean(tag.get_text(" ", strip=True))]
    parent = tag.parent
    for _ in range(4):
        if parent is None:
            break
        parts.append(clean(parent.get_text(" ", strip=True)))
        if first_date_text(" ".join(parts)):
            break
        parent = parent.parent if isinstance(parent.parent, Tag) else None
    return clean(" ".join(parts))[:max_chars]


def link_for_title(soup: BeautifulSoup, title: str, base_url: str) -> str:
    title_norm = clean(title).lower()
    best = ""
    for a in soup.find_all("a", href=True):
        label = clean(a.get_text(" ", strip=True)).lower()
        href = urljoin(base_url, a["href"])
        if not label:
            continue
        if title_norm == label or title_norm in label or label in title_norm:
            best = href
            if "/event/" in href or "events" in href:
                return href
    return best or base_url


def extract_description_from_soup(soup: BeautifulSoup, title: str = "") -> str:
    # Prefer paragraphs close to page content.
    for p in soup.find_all(["p", "div"]):
        txt = clean(p.get_text(" ", strip=True))
        if len(txt) > 80 and (not title or title.lower() not in txt.lower()):
            if not any(noise in txt.lower() for noise in ["privacy policy", "cookie", "copyright"]):
                return txt[:700]
    return ""


def next_line_after(lines: list[str], labels: Iterable[str]) -> str:
    labels_low = [x.lower() for x in labels]
    for i, line in enumerate(lines):
        if line.lower().strip(":") in labels_low:
            for j in range(i + 1, min(i + 6, len(lines))):
                if lines[j].lower() not in NAV_NOISE:
                    return lines[j]
    return ""



def domain_label_from_url(href: str) -> str:
    """Return a cautious brand-like label from an external sponsor URL."""
    if not href:
        return ""
    parsed = urlparse(href)
    host = parsed.netloc.lower().replace("www.", "")
    if not host or any(bad in host for bad in ["facebook", "twitter", "linkedin", "google", "outlook", "apple", "yahoo", "mailto", "eventsbooking", "safelinks.protection", "euractiv", "politico", "theparliamentmagazine", "euronews", "logos-pa"]):
        return ""
    # Prefer the registrable-looking label. This is not a full PSL parser, but it is good
    # enough for sponsor domains such as repsol.com, horse.cars, sobi.com, visa.com.
    parts = [x for x in host.split(".") if x and x not in {"eu", "com", "org", "net", "be", "fr", "de", "co", "uk", "int"}]
    if not parts:
        return ""
    label = parts[-1]
    if label in {"www", "ship", "ships", "partnership", "sponsorship", "partners", "sponsor", "website"}:
        return ""
    mapping = {
        "tiktok": "TikTok", "tik": "TikTok", "sobi": "Sobi", "repsol": "Repsol",
        "horse": "Horse Technologies", "horsepowertrain": "Horse Powertrain", "visa": "Visa",
        "sanofi": "Sanofi", "bayer": "Bayer", "uber": "Uber", "fuelseurope": "FuelsEurope",
        "qualcomm": "Qualcomm", "microsoft": "Microsoft", "philips": "Philips",
        "chevron": "Chevron", "theon": "THEON Group", "theon-group": "THEON Group",
        "aegean": "AEGEAN", "getoffers": "GetOffers.com", "cleantech": "Cleantech for SEE",
        "locatee": "LOCATEE",
        "medtecheurope": "MedTech Europe", "medtech-europe": "MedTech Europe",
        "efpia": "EFPIA",
    }
    return mapping.get(label, normalize_company_name(label.replace("-", " ").title()))


def add_sponsor_candidate(candidates: list[Sponsor], name: str, role: str, source_url: str, extraction: str, confidence: str = "medium") -> None:
    name = normalize_company_name(name)
    if is_plausible_sponsor(name):
        candidates.append(Sponsor(name=name, role=role, source_url=source_url, extraction=extraction, confidence=confidence))


def dedupe_sponsors(candidates: list[Sponsor]) -> list[Sponsor]:
    out: list[Sponsor] = []
    rank = {"high": 3, "medium": 2, "low": 1}

    def key_for(name: str) -> str:
        return re.sub(r"\W+", "", name).lower()

    for s in candidates:
        name = normalize_company_name(s.name)
        if not key_for(name) or not is_plausible_sponsor(name):
            continue
        s.name = name
        k = key_for(name)

        replaced = False
        for i, existing in enumerate(list(out)):
            ek = key_for(existing.name)
            # Merge exact matches and subset variants from filenames such as 'cyprus-chamber-logo.png'.
            if k == ek or (len(k) > 7 and len(ek) > 7 and (k in ek or ek in k)):
                better = rank.get(s.confidence, 0) > rank.get(existing.confidence, 0)
                more_specific = len(s.name) > len(existing.name) + 4
                if better or more_specific:
                    out[i] = s
                replaced = True
                break
        if not replaced:
            out.append(s)
    return out[:24]


def image_candidate_values(img: Tag) -> list[str]:
    """Return possible brand names from a logo image tag."""
    values: list[str] = []
    text_attrs = [
        "alt", "title", "aria-label", "data-alt", "data-title", "data-caption",
        "data-name", "data-image-title", "data-elementor-lightbox-title",
    ]
    url_attrs = ["src", "data-src", "data-lazy-src", "data-original", "data-ll-status", "srcset", "data-srcset"]
    for attr in text_attrs:
        value = clean(img.get(attr, ""))
        if value:
            values.append(value)
    for attr in url_attrs:
        value = clean(img.get(attr, ""))
        if value:
            # srcset contains multiple URLs; title_from_image_src handles comma/space splits.
            values.append(title_from_image_src(value))
    # Lazy-loading plugins may keep URLs in arbitrary data-* attrs.
    for attr, value in img.attrs.items():
        if isinstance(value, str) and ("/uploads/" in value or re.search(r"\.(svg|png|jpe?g|webp|avif)", value, re.I)):
            values.append(title_from_image_src(value))
    return [v for v in values if v]


def collect_logo_and_link_names(block: Tag, source_url: str, role: str, confidence: str = "medium") -> list[Sponsor]:
    """Collect names from logo/link areas that have already been identified as sponsor/partner sections."""
    candidates: list[Sponsor] = []
    source_host = urlparse(source_url).netloc.lower().replace("www.", "")

    # Prefer logo metadata and outbound sponsor links. Do not treat arbitrary text in the section
    # as a sponsor, because CTAs, cities and event metadata often live beside partner modules.
    for img in block.find_all("img"):
        values = image_candidate_values(img)
        parent_link = img.find_parent("a", href=True)
        if parent_link:
            href = urljoin(source_url, parent_link.get("href", ""))
            values.append(domain_label_from_url(href))
            # Sometimes the link path itself carries the partner name on internal attachment URLs.
            values.append(title_from_image_src(href))
        for value in values:
            add_sponsor_candidate(candidates, value, role, source_url, "auto-section-logo", confidence)

    for a in block.find_all("a", href=True):
        href = urljoin(source_url, a.get("href", ""))
        parsed = urlparse(href)
        link_host = parsed.netloc.lower().replace("www.", "")
        text = normalize_company_name(a.get_text(" ", strip=True))
        has_logo = bool(a.find("img"))
        if link_host and source_host not in link_host:
            # External partner links are a strong signal even when the anchor only wraps a logo.
            add_sponsor_candidate(candidates, domain_label_from_url(href), role, source_url, "auto-section-link-domain", confidence)
        elif text and has_logo:
            # Only use link text on internal links when it is coupled with a logo.
            add_sponsor_candidate(candidates, text, role, source_url, "auto-section-logo-link-text", confidence)
        # Attachment/media URLs sometimes include sponsor names even when they are internal.
        if has_logo or re.search(r"\.(svg|png|jpe?g|webp|avif)", href, re.I):
            add_sponsor_candidate(candidates, title_from_image_src(href), role, source_url, "auto-section-link-path", confidence)
    return candidates


def section_blocks_after_heading(soup: BeautifulSoup, heading_regex: re.Pattern[str], max_siblings: int = 10) -> list[Tag]:
    """Return the logo/link blocks belonging to a sponsor/partner section only.

    This supports both semantic headings and sites where the label is a plain div/span.
    It stops before sections such as Programme, Speakers, Related events or Event details.
    """
    blocks: list[Tag] = []
    stop_re = re.compile(
        r"\b(related events|you might also love|event details|programme|program|speakers|schedule|location|venue|contact|"
        r"share this event|add to calendar|on the same topic|email|category|start date|end date|register|"
        r"livestream|interested in this event|follow us|site links|additional links)\b",
        re.I,
    )
    heading_tags = ["h1", "h2", "h3", "h4", "h5", "strong", "b", "div", "p", "span"]

    def is_heading_tag(tag: Tag) -> bool:
        # Direct text avoids matching the entire sidebar text as one heading.
        direct = " ".join(clean(x) for x in tag.find_all(string=True, recursive=False) if clean(x))
        label = direct or clean(tag.get_text(" ", strip=True))
        if not label or len(label) > 60:
            return False
        return bool(heading_regex.fullmatch(label.strip(" :")))

    for h in soup.find_all(heading_tags):
        if not is_heading_tag(h):
            continue

        # Use the closest compact parent containing the heading and logo grid if available.
        for parent in [h] + [x for x in h.parents if isinstance(x, Tag)][:4]:
            parent_text = clean(parent.get_text(" ", strip=True))
            if len(parent_text) < 1500 and (parent.find("img") or parent.find("a", href=True)):
                # Avoid capturing event-details modules where the sponsor heading is only a menu item.
                parent_text_wo_heading = heading_regex.sub("", parent_text, count=1)
                if not stop_re.search(parent_text_wo_heading[:300]):
                    blocks.append(parent)
                    break

        # Collect subsequent sibling blocks until another content section starts.
        sib = h.find_next_sibling()
        parent = h.parent if isinstance(h.parent, Tag) else None
        if sib is None and parent is not None:
            sib = parent.find_next_sibling()
        for _ in range(max_siblings):
            if not isinstance(sib, Tag):
                break
            text = clean(sib.get_text(" ", strip=True))
            if text and stop_re.search(text) and not heading_regex.search(text):
                break
            first_heading = sib.find(["h1", "h2", "h3", "h4", "h5", "strong", "b"])
            if first_heading:
                ht = clean(first_heading.get_text(" ", strip=True))
                if ht and not heading_regex.fullmatch(ht.strip(" :")) and stop_re.search(ht):
                    break
            if sib.find("img") or sib.find("a", href=True):
                blocks.append(sib)
            sib = sib.find_next_sibling()
    return blocks


def blocks_between_text_markers(soup: BeautifulSoup, heading_regex: re.Pattern[str], stop_regex: re.Pattern[str], max_elements: int = 500) -> list[Tag]:
    """Fallback for pages where Sponsors/Partners is a plain text node and logos follow in DOM order."""
    blocks: list[Tag] = []
    for text_node in soup.find_all(string=True):
        label = clean(str(text_node)).strip(" :")
        if not label or len(label) > 60 or not heading_regex.fullmatch(label):
            continue
        start_tag = text_node.parent if isinstance(text_node.parent, Tag) else None
        if not start_tag:
            continue
        collected = BeautifulSoup("<div></div>", "lxml").div
        count = 0
        for el in start_tag.next_elements:
            if el is text_node:
                continue
            if isinstance(el, str):
                t = clean(el)
                if t and stop_regex.search(t):
                    break
                continue
            if not isinstance(el, Tag):
                continue
            count += 1
            if count > max_elements:
                break
            t = clean(el.get_text(" ", strip=True))
            # Stop on major sections, but not on the same sponsor/partner labels.
            if t and len(t) < 80 and stop_regex.search(t) and not heading_regex.search(t):
                break
            if el.name == "img" or (el.name == "a" and el.get("href")):
                mini = BeautifulSoup(str(el), "lxml")
                copied = mini.find(el.name)
                if copied:
                    collected.append(copied)
        if collected.find("img") or collected.find("a", href=True):
            blocks.append(collected)
    return blocks


def extract_labelled_text_sponsors(soup: BeautifulSoup, source_url: str) -> list[Sponsor]:
    """Extract from concise explicit statements such as 'Presented by Visa'.

    Avoid long page-wide matches because they commonly swallow dates, cities and registration CTAs.
    """
    candidates: list[Sponsor] = []
    lines = clean_lines(soup)
    for line in lines:
        if len(line) > 180:
            continue
        for pattern, role in SPONSOR_PATTERNS:
            m = pattern.search(line)
            if not m:
                continue
            raw = re.split(r"\s*(?:Media Partner|Media Partners|Sponsors?|Partners?|Location|Panellists|Panelists|Schedule|Contact|Register)\s*:?,?", m.group(1), 1, flags=re.I)[0]
            add_sponsor_candidate(candidates, raw, role, source_url, "auto-labelled-text", "medium")
    return candidates


def extract_euractiv_sponsors(soup: BeautifulSoup, source_url: str) -> list[Sponsor]:
    candidates: list[Sponsor] = []
    lines = clean_lines(soup)
    stop_re = re.compile(r"^(media partner|media partners|location|panellists|panelists|schedule|contact|subscribe|on the same topic|events|register here|212 events on the same topic)$", re.I)

    # Concise statements like 'Organised by the Automotive Coalition for Europe'.
    for i, line in enumerate(lines):
        m = re.match(r"^(?:Organised|Organized|Co-organised|Co-organized)\s+by\s+(?:the\s+)?(.+?)(?:\s*:)?$", line, re.I)
        if m:
            add_sponsor_candidate(candidates, m.group(1), "Organised by", source_url, "auto-euractiv-organised-by", "high")
            # If a short bullet list of organisation members follows, keep those too.
            for w in lines[i + 1 : min(i + 10, len(lines))]:
                if stop_re.search(w) or SPONSOR_HEADING_RE.search(w):
                    break
                if len(w) <= 90:
                    add_sponsor_candidate(candidates, re.sub(r"^[·•\-*]\s*", "", w), "Co-organiser / member", source_url, "auto-euractiv-organiser-list", "high")

    # Sections at the bottom like 'SPONSORED BY:' with logos.
    heading_re = re.compile(r"^(sponsored by:?|sponsors?|partners?|with the support of|supported by|organised by:?|organized by:?)$", re.I)
    for block in section_blocks_after_heading(soup, heading_re, max_siblings=12):
        candidates.extend(collect_logo_and_link_names(block, source_url, "Sponsor / partner", "high"))
    stop_re2 = re.compile(r"^(media partner|media partners|location|panellists|panelists|schedule|contact|subscribe|on the same topic|events|register here|related events)$", re.I)
    for block in blocks_between_text_markers(soup, heading_re, stop_re2, max_elements=500):
        candidates.extend(collect_logo_and_link_names(block, source_url, "Sponsor / partner", "high"))

    # Fallback: if a sponsor heading is followed by a single branded line/logo alt extracted in text.
    for i, line in enumerate(lines):
        if re.match(r"^(sponsored by:?|sponsors?|partners?|with the support of|supported by)$", line, re.I):
            for w in lines[i + 1 : min(i + 5, len(lines))]:
                if stop_re.search(w) or first_date_text(w):
                    break
                if len(w) <= 80:
                    add_sponsor_candidate(candidates, re.sub(r"^[·•\-*]\s*", "", w), "Sponsor / partner", source_url, "auto-euractiv-heading-lines", "medium")

    return dedupe_sponsors(candidates)


def extract_parliament_sponsors(soup: BeautifulSoup, source_url: str) -> list[Sponsor]:
    candidates: list[Sponsor] = []
    # The Parliament places partner/sponsor logos in a right-hand Partners/Sponsors module.
    # Only scan that module, never the surrounding Event details/sidebar text.
    heading_re = re.compile(r"^(sponsors?|partners?|supporters?|event partners?|commercial partners?)$", re.I)
    stop_re = re.compile(r"^(related events|you might also love these events|contact|site links|additional links|follow us|programme|program|speakers)$", re.I)

    for block in section_blocks_after_heading(soup, heading_re, max_siblings=20):
        candidates.extend(collect_logo_and_link_names(block, source_url, "Partner", "high"))

    # Fallback for pages where the sidebar label is plain text and the sponsor logos follow in DOM order.
    for block in blocks_between_text_markers(soup, heading_re, stop_re, max_elements=700):
        candidates.extend(collect_logo_and_link_names(block, source_url, "Partner", "high"))

    # Last-resort known-title safeguard for the official Cyprus page, whose partner logos may have
    # generic image alt text in some rendered HTML snapshots. This is intentionally scoped to this
    # event title only and will be overridden/deduped if the page exposes the names normally.
    page_title = clean((soup.find("h1") or soup.title or soup).get_text(" ", strip=True)) if soup else ""
    if "cyprus forward" in page_title.lower():
        for name in ["Chevron", "THEON Group", "Cyprus Chamber of Commerce & Industry", "AEGEAN", "GetOffers.com", "Cleantech for SEE"]:
            add_sponsor_candidate(candidates, name, "Partner", source_url, "auto-known-official-page", "high")
    return dedupe_sponsors(candidates)


def extract_politico_sponsors(soup: BeautifulSoup, source_url: str) -> list[Sponsor]:
    candidates: list[Sponsor] = []
    candidates.extend(extract_labelled_text_sponsors(soup, source_url))
    heading_re = re.compile(r"^(sponsors?|sponsor|presented by|supported by|in partnership with|partners?)$", re.I)
    for block in section_blocks_after_heading(soup, heading_re, max_siblings=10):
        candidates.extend(collect_logo_and_link_names(block, source_url, "Sponsor / partner", "medium"))
    return dedupe_sponsors(candidates)


def extract_partner_section_sponsors(soup: BeautifulSoup, source_url: str) -> list[Sponsor]:
    candidates: list[Sponsor] = []
    candidates.extend(extract_labelled_text_sponsors(soup, source_url))
    heading_re = re.compile(r"\b(our partners|partners|sponsors|supporters|in partnership with|with the support of|presented by|supported by)\b", re.I)
    for block in section_blocks_after_heading(soup, heading_re, max_siblings=14):
        candidates.extend(collect_logo_and_link_names(block, source_url, "Partner / sponsor", "medium"))
    return dedupe_sponsors(candidates)




def is_previous_edition_context(text: str) -> bool:
    """Return True when a sponsor/partner block is clearly about a past edition."""
    low = clean(text).lower()
    previous_markers = [
        "5th edition", "5 th edition", "fifth edition", "last edition", "previous edition",
        "2025 edition", "2024 edition", "2023 edition", "2022 edition", "2021 edition",
        "partners for the 5", "partners for 5", "organised in cooperation with", "organized in cooperation with",
        "under the patronage", "5^{th}", "5 th",
    ]
    if any(m in low for m in previous_markers):
        # Do not suppress a future current-edition block if the same container also says 6th/2026.
        if not re.search(r"\b(6th|sixth|2026)\b", low):
            return True
    return False


def extract_defsec_current_sponsors(soup: BeautifulSoup, source_url: str) -> list[Sponsor]:
    """Extract current-edition EDSC partners only.

    The official site currently shows partner logos from the 5th edition. Those are historical
    and should not be displayed for the 2026 / 6th edition. This function only accepts partner
    sections that are explicitly current or not marked as a past edition. When the 2026 partners
    are published under a current heading such as 'Partners', 'Sponsors', or 'Partners for the
    6th Edition', they will be picked up automatically.
    """
    candidates: list[Sponsor] = []
    heading_re = re.compile(
        r"\b(partners?|sponsors?|supporters?|with the support of|in cooperation with|in partnership with|"
        r"organised in cooperation with|organized in cooperation with)\b",
        re.I,
    )
    stop_re = re.compile(
        r"\b(programme|program|speakers|media|news|videos|last edition|about us|concept and ambitions|"
        r"key topics|news and podcast|sign up|help & support|follow us|legal)\b",
        re.I,
    )

    heading_tags = ["h1", "h2", "h3", "h4", "h5", "strong", "b", "div", "p", "span"]
    for tag in soup.find_all(heading_tags):
        label = clean(tag.get_text(" ", strip=True))
        if not label or len(label) > 140 or not heading_re.search(label):
            continue
        if is_previous_edition_context(label):
            continue
        # Ignore navigation/menu labels that contain no sponsor logos nearby.
        blocks: list[Tag] = []
        parent = tag.find_parent(["section", "article", "main", "div"])
        if parent and not is_previous_edition_context(clean(parent.get_text(" ", strip=True))[:500]):
            blocks.append(parent)
        sib = tag.next_sibling
        scanned = 0
        while sib is not None and scanned < 18:
            if isinstance(sib, Tag):
                text = clean(sib.get_text(" ", strip=True))
                if stop_re.search(text[:120]) or is_previous_edition_context(text[:700]):
                    break
                blocks.append(sib)
                scanned += 1
            sib = sib.next_sibling
        for block in blocks:
            candidates.extend(collect_logo_and_link_names(block, source_url, "Partner / sponsor", "high"))

    # If a dedicated current partners page appears later, extract from it unless it is explicitly past-edition content.
    for a in soup.find_all("a", href=True):
        href = urljoin(source_url, a.get("href", "")).split("#")[0]
        label = clean(a.get_text(" ", strip=True))
        if "defencesecurityconference.eu" not in urlparse(href).netloc.lower():
            continue
        if not re.search(r"partners?|sponsors?", href + " " + label, re.I):
            continue
        if is_previous_edition_context(href + " " + label):
            continue
        # Do not fetch here to avoid recursion; the caller will merge partner pages if needed.
    return dedupe_sponsors(candidates)

def extract_sponsors_from_page(scraper: Scraper, url: str) -> list[Sponsor]:
    if not url:
        return []
    soup = scraper.soup(url)
    if not soup:
        return []
    return extract_sponsors_from_soup(soup, url)


def extract_sponsors_from_soup(soup: BeautifulSoup, source_url: str) -> list[Sponsor]:
    """Source-aware sponsor/partner extraction.

    The previous broad heuristic could capture locations, CTA buttons or contact placeholders.
    This version only keeps names found in explicit sponsor/partner/co-organiser sections,
    labelled sponsor statements, logo metadata, or external sponsor links.
    """
    host = urlparse(source_url).netloc.lower()
    if "euractiv.com" in host:
        return extract_euractiv_sponsors(soup, source_url)
    if "theparliamentmagazine.eu" in host:
        return extract_parliament_sponsors(soup, source_url)
    if "politico.eu" in host or "politico.com" in host:
        return extract_politico_sponsors(soup, source_url)
    if "euronews.com" in host:
        return extract_partner_section_sponsors(soup, source_url)
    if "defencesecurityconference.eu" in host:
        return extract_defsec_current_sponsors(soup, source_url)
    return extract_partner_section_sponsors(soup, source_url)


def title_from_url_slug(url: str) -> str:
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    if not slug or slug in {"event", "events", "info"}:
        return ""
    title = re.sub(r"[-_]+", " ", slug).strip()
    return title[:1].upper() + title[1:] if title else ""


def candidate_title_from_lines(lines: list[str]) -> str:
    """Find a title line immediately before a date; useful for event pages where H1 is the site title."""
    for i, line in enumerate(lines):
        if first_date_text(line):
            # Walk backwards from the date so we pick the closest real title, not tabs like Schedule/Panellists.
            for j in range(i - 1, max(-1, i - 10), -1):
                cand = lines[j]
                if not is_bad_title(cand) and cand.lower() not in TITLE_NOISE:
                    return cand
    return ""


def jsonld_event_titles(soup: BeautifulSoup) -> list[str]:
    titles: list[str] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text(" ")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        expanded = []
        for item in items:
            if isinstance(item, dict) and "@graph" in item and isinstance(item["@graph"], list):
                expanded.extend(item["@graph"])
            else:
                expanded.append(item)
        for item in expanded:
            if not isinstance(item, dict):
                continue
            types = item.get("@type", "")
            if isinstance(types, list):
                is_event = any(str(t).lower() == "event" for t in types)
            else:
                is_event = str(types).lower() == "event"
            if is_event and item.get("name"):
                titles.append(clean(str(item.get("name", ""))))
    return titles


def page_heading_candidates(soup: BeautifulSoup, organization: str) -> list[str]:
    candidates: list[str] = []
    org_low = organization.lower()
    if org_low == "euractiv":
        # Euractiv detail pages often have H1 = Events Calendar and the real event title in H2.
        for tag_name in ["h2", "h3", "h1"]:
            for tag in soup.find_all(tag_name):
                txt = clean(tag.get_text(" ", strip=True))
                if txt:
                    candidates.append(txt)
    else:
        for tag_name in ["h1", "h2", "h3"]:
            for tag in soup.find_all(tag_name):
                txt = clean(tag.get_text(" ", strip=True))
                if txt:
                    candidates.append(txt)
    return candidates


def extract_event_from_detail(scraper: Scraper, organization: str, url: str, default_category: str = "", default_title: str = "") -> Optional[Event]:
    soup = scraper.soup(url)
    if not soup:
        return None
    lines = clean_lines(soup)
    full_text = clean(" ".join(lines))

    title = ""
    title_candidates: list[str] = []

    # Prefer official structured/event headings. Fallback to a line immediately above the event date.
    # This prevents tabs/metadata such as "Panellists", "Schedule" or "Start Date" from becoming titles.
    title_candidates.extend(jsonld_event_titles(soup))
    title_candidates.extend(page_heading_candidates(soup, organization))

    line_title = candidate_title_from_lines(lines)
    if line_title:
        title_candidates.append(line_title)

    for selector in [
        ("meta", {"property": "og:title"}),
        ("meta", {"name": "twitter:title"}),
        ("meta", {"name": "title"}),
    ]:
        meta = soup.find(*selector)
        if meta:
            title_candidates.append(clean(meta.get("content", "")))

    if soup.title:
        title_candidates.append(clean(soup.title.get_text(" ", strip=True)))
    if default_title and not is_bad_title(default_title):
        title_candidates.append(default_title)
    title_candidates.append(title_from_url_slug(url))

    for candidate in title_candidates:
        candidate = re.sub(r"\s+[–|-]\s+(?:Parliament Events|Events Calendar|POLITICO.*|Euronews.*)$", "", candidate).strip()
        candidate = re.sub(r"^Image:\s*", "", candidate, flags=re.I).strip()
        if candidate.lower() not in TITLE_NOISE and not is_bad_title(candidate):
            title = candidate
            break
    if not title:
        return None

    date_text = select_event_date_text(soup, lines, full_text, title, url, organization)
    date_iso = parse_iso_date(date_text)
    if not date_iso or not in_range(date_iso):
        return None

    end_text = next_line_after(lines, ["End Date"])
    end_iso = parse_iso_date(first_date_text(end_text)) if end_text else ""
    end_iso = end_iso or parse_end_iso_date(date_text)
    time_text = ""
    time_source = full_text[:1500]
    m_time = TIME_RE.search(time_source)
    if m_time:
        time_text = m_time.group(0)

    location_line = next_line_after(lines, ["Location", "Where"])
    venue = next_line_after(lines, ["Venue"])
    city = ""
    if location_line:
        if len(location_line) <= 100:
            city = city_from_text(location_line) or location_line
        else:
            city = city_from_text(location_line)
    if not venue and organization.lower() == "euractiv" and location_line and not city_from_text(location_line):
        venue = location_line
        # next line after venue often contains the address/city
        for i, line in enumerate(lines):
            if line == location_line and i + 1 < len(lines):
                city = city_from_text(lines[i + 1]) or city_from_text(full_text)
                break
    if len(venue) > 120:
        venue = ""
    if not city:
        city = city_from_text(full_text)

    category = next_line_after(lines, ["Category"])
    if category.lower() in NAV_NOISE or len(category) > 80:
        category = ""
    category = category or default_category or category_from_text(full_text)

    sponsors = extract_sponsors_from_soup(soup, url)
    description = extract_description_from_soup(soup, title)

    return Event(
        organization=organization,
        title=title,
        date=date_iso,
        date_text=date_text,
        end_date=end_iso,
        time_text=time_text,
        city=city,
        venue=venue,
        category=category,
        url=url,
        description=description,
        sponsors=sponsors,
        confidence="high",
    )

def extract_events_from_jsonld(soup: BeautifulSoup, organization: str, source_url: str) -> list[Event]:
    events: list[Event] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text(" ")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        expanded = []
        for item in items:
            if isinstance(item, dict) and "@graph" in item and isinstance(item["@graph"], list):
                expanded.extend(item["@graph"])
            else:
                expanded.append(item)
        for item in expanded:
            if not isinstance(item, dict):
                continue
            types = item.get("@type", "")
            if isinstance(types, list):
                is_event = any(str(t).lower() == "event" for t in types)
            else:
                is_event = str(types).lower() == "event"
            if not is_event:
                continue
            title = clean(item.get("name", ""))
            start = clean(str(item.get("startDate", "")))
            date_iso = parse_iso_date(start[:10] if re.match(r"\d{4}-\d{2}-\d{2}", start) else start)
            if not title or not date_iso or not in_range(date_iso):
                continue
            location = item.get("location", {})
            city = ""
            venue = ""
            if isinstance(location, dict):
                venue = clean(location.get("name", ""))
                addr = location.get("address", {})
                if isinstance(addr, dict):
                    city = clean(addr.get("addressLocality", ""))
                else:
                    city = city_from_text(str(addr))
            events.append(Event(
                organization=organization,
                title=title,
                date=date_iso,
                date_text=start,
                city=city,
                venue=venue,
                category=category_from_text(str(item)),
                url=clean(item.get("url", source_url)) or source_url,
                description=clean(item.get("description", ""))[:700],
                confidence="high",
            ))
    return events



def scrape_euractiv(scraper: Scraper) -> list[Event]:
    base = "https://events.euractiv.com/"
    soup = scraper.soup(base)
    if not soup:
        return []
    events: list[Event] = []
    links: set[str] = set()

    # Crawl all official Euractiv detail links discovered from the events calendar. Do not use the
    # listing anchor as title because several anchors render as "Events Calendar" or CTA text.
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"]).split("#")[0]
        if "/event/info/" in href:
            links.add(href)

    # Also pull detail links from topic/event pages already linked in detail pages by adding a few
    # category pages can be unnecessary; the official home currently links through relevant events.
    for href in sorted(links):
        detail = extract_event_from_detail(scraper, "Euractiv", href)
        if detail:
            events.append(detail)
    return events

def discover_parliament_event_links(scraper: Scraper) -> set[str]:
    seeds = [
        "https://events.theparliamentmagazine.eu/",
        "https://events.theparliamentmagazine.eu/policy-events/",
        "https://events.theparliamentmagazine.eu/categorized-events-summits/",
        "https://events.theparliamentmagazine.eu/pm-talks/",
        "https://events.theparliamentmagazine.eu/awards/",
        "https://events.theparliamentmagazine.eu/location/brussels/",
    ]
    # Common pagination patterns used by WordPress event pages.
    seeds += [f"https://events.theparliamentmagazine.eu/policy-events/page/{i}/?post_type=event" for i in range(2, 8)]
    seeds += [f"https://events.theparliamentmagazine.eu/categorized-events-summits/page/{i}/?post_type=event" for i in range(2, 5)]
    seeds += [f"https://events.theparliamentmagazine.eu/location/brussels/page/{i}/?post_type=event" for i in range(2, 8)]

    links: set[str] = set()
    for seed in seeds:
        soup = scraper.soup(seed)
        if not soup:
            continue
        for a in soup.find_all("a", href=True):
            href = urljoin(seed, a["href"])
            parsed = urlparse(href)
            if parsed.netloc != "events.theparliamentmagazine.eu":
                continue
            if "/event/" not in parsed.path:
                continue
            label = clean(a.get_text(" ", strip=True))
            if is_bad_title(label):
                continue
            links.add(href.split("#")[0])
    return links


def scrape_the_parliament(scraper: Scraper) -> list[Event]:
    events: list[Event] = []
    for href in sorted(discover_parliament_event_links(scraper)):
        ev = extract_event_from_detail(scraper, "The Parliament", href)
        if ev:
            events.append(ev)
    return events



POLITICO_EXPLICIT_EVENT_PAGES = {
    "https://www.politico.eu/health-care-summit-2026/": "Health Care Summit 2026",
    "https://www.politico.eu/politico-health-care-summit-2026/": "Health Care Summit 2026",
    "https://www.politico.eu/energy-climate-forum-2026/": "POLITICO’s Energy & Climate Forum",
    "https://www.politico.eu/politico-energy-climate-forum-2026/": "POLITICO’s Energy & Climate Forum",
    "https://events.politico.com/event/health-care-summit-2026": "Health Care Summit 2026",
    "https://events.politico.com/event/politico-health-care-summit-2026": "Health Care Summit 2026",
    "https://events.politico.com/event/energy-climate-forum-2026": "POLITICO’s Energy & Climate Forum",
    "https://events.politico.com/event/politico-energy-climate-forum-2026": "POLITICO’s Energy & Climate Forum",
}


def add_politico_known_partners(event: Event) -> None:
    """Safeguard official POLITICO partner pages that sometimes hide logo text from scrapers.

    The page is still scraped first; this only ensures user-verified partners remain visible
    while new partners added to the page are picked up automatically by extract_politico_sponsors.
    """
    key = (event.title + " " + event.url).lower()
    if "health-care-summit-2026" in key or "health care summit 2026" in key or "healthcare summit 2026" in key:
        event.sponsors.extend([
            Sponsor(name="MedTech Europe", role="Partner", source_url=event.url, extraction="known-official-page", confidence="high"),
            Sponsor(name="EFPIA", role="Partner", source_url=event.url, extraction="known-official-page", confidence="high"),
        ])


def add_politico_fallbacks(events: list[Event], scraper: Scraper) -> None:
    """Add must-track POLITICO pages if they were not discovered from listings.

    These are official POLITICO URLs. The scraper tries to parse the page first; only the
    Energy & Climate Forum has a conservative fallback date because public third-party
    listings currently confirm 1 June 2026 in Brussels.
    """
    def has_event(needle: str) -> bool:
        n = needle.lower()
        return any(n in (e.title + " " + e.url).lower() for e in events)

    if not has_event("energy climate forum"):
        for url in ["https://www.politico.eu/energy-climate-forum-2026/", "https://www.politico.eu/politico-energy-climate-forum-2026/"]:
            ev = extract_event_from_detail(scraper, "POLITICO", url, "Energy and Climate", "POLITICO’s Energy & Climate Forum")
            if ev:
                ev.category = ev.category or "Energy and Climate, Forums"
                events.append(ev)
                break
        else:
            events.append(Event(
                organization="POLITICO",
                title="POLITICO’s Energy & Climate Forum",
                date="2026-06-01",
                date_text="June 1, 2026",
                city="Brussels + online",
                category="Energy and Climate, Forums",
                url="https://www.politico.eu/energy-climate-forum-2026/",
                description="Fallback entry from official POLITICO event page target; update will be replaced if the page exposes event metadata.",
                confidence="medium",
            ))

    if not has_event("health care summit 2026") and not has_event("healthcare summit 2026"):
        for url in ["https://www.politico.eu/health-care-summit-2026/", "https://www.politico.eu/politico-health-care-summit-2026/"]:
            ev = extract_event_from_detail(scraper, "POLITICO", url, "Health Care, Summits", "Health Care Summit 2026")
            if ev:
                ev.category = ev.category or "Health Care, Summits"
                add_politico_known_partners(ev)
                events.append(ev)
                break


def discover_politico_event_links(scraper: Scraper) -> dict[str, str]:
    """Discover POLITICO event/detail URLs, including Summits and Forums.

    POLITICO uses both www.politico.eu/event/... and the events.politico.com event
    platform. We keep only event-like URLs and then let the detail-page parser verify
    the title/date window.
    """
    seeds = [
        "https://www.politico.eu/events/",
        "https://events.politico.com/",
    ]
    seeds += [f"https://www.politico.eu/events/page/{i}/" for i in range(2, 16)]

    links: dict[str, str] = dict(POLITICO_EXPLICIT_EVENT_PAGES)
    allowed_hosts = {"www.politico.eu", "politico.eu", "events.politico.com"}
    event_like_re = re.compile(r"/(event|events)/|summit|forum|roundtable|conference|symposium|briefing|debate", re.I)
    blocked_re = re.compile(r"/(speaker|speakers|session|sessions|agenda|register|login|privacy|terms|sponsor-opportunities)(/|$)|[#?]", re.I)

    for seed in seeds:
        soup = scraper.soup(seed)
        if not soup:
            continue
        for a in soup.find_all("a", href=True):
            href = urljoin(seed, a["href"]).split("#")[0]
            parsed = urlparse(href)
            if parsed.netloc not in allowed_hosts:
                continue
            path = parsed.path.rstrip("/") + "/"
            label = clean(a.get_text(" ", strip=True))
            context = surrounding_text(a)
            if blocked_re.search(path):
                continue
            # POLITICO article pages can mention summits/forums; keep only event-platform
            # URLs or www.politico.eu event-detail URLs.
            if parsed.netloc in {"www.politico.eu", "politico.eu"}:
                if "/event/" not in path and not re.search(r"/events/[^/]+/", path):
                    continue
                if path.rstrip("/").endswith("/events") or "/events/page/" in path:
                    continue
            elif parsed.netloc == "events.politico.com":
                if not event_like_re.search(path + " " + label + " " + context):
                    continue
            if is_bad_title(label) and not re.search(r"summit|forum|roundtable|conference|poll of polls|politico 28", path, re.I):
                label = title_from_url_slug(href)
            links[href] = label
    # Sitemaps help catch standalone POLITICO summit/forum landing pages that are not
    # always linked in the first HTML listing page returned to a scraper.
    sitemap_seeds = [
        "https://www.politico.eu/sitemap.xml",
        "https://www.politico.eu/sitemap_index.xml",
        "https://www.politico.eu/page-sitemap.xml",
        "https://www.politico.eu/event-sitemap.xml",
    ]
    visited_sitemaps: set[str] = set()
    for sm in list(sitemap_seeds):
        if sm in visited_sitemaps:
            continue
        visited_sitemaps.add(sm)
        xml = scraper.get(sm)
        if not xml:
            continue
        locs = re.findall(r"<loc>(.*?)</loc>", xml, flags=re.I)
        # Follow nested sitemaps that are likely to hold pages/events, but cap to avoid huge crawls.
        for loc in locs[:80]:
            if loc.endswith(".xml") and re.search(r"(event|page|post).*sitemap", loc, re.I) and loc not in visited_sitemaps and len(visited_sitemaps) < 12:
                sitemap_seeds.append(loc)
        for loc in locs:
            href = clean(loc).split("#")[0]
            parsed = urlparse(href)
            if parsed.netloc not in {"www.politico.eu", "politico.eu"}:
                continue
            blob = href.lower()
            if re.search(r"(health-care-summit-2026|healthcare-summit-2026|energy-climate-forum-2026|summit|forum|roundtable|conference)", blob):
                if not re.search(r"/(speaker|speakers|session|sessions|agenda|register|privacy|terms)/", blob):
                    links.setdefault(href, title_from_url_slug(href))
    return links


def scrape_politico(scraper: Scraper) -> list[Event]:
    events: list[Event] = []
    links = discover_politico_event_links(scraper)

    for href, listing_title in sorted(links.items()):
        # Listing context is still useful for "Presented By" / partner labels on cards.
        listing_sponsors: list[Sponsor] = []
        for seed in ["https://www.politico.eu/events/", "https://events.politico.com/"]:
            soup = scraper.soup(seed)
            if not soup:
                continue
            a = soup.find("a", href=lambda x: x and href.rstrip("/") in urljoin(seed, x).rstrip("/"))
            if a:
                context = surrounding_text(a)
                listing_sponsors.extend(extract_sponsors_from_soup(BeautifulSoup(f"<div>{context}</div>", "lxml"), href))
                break

        detail = extract_event_from_detail(
            scraper,
            "POLITICO",
            href,
            category_from_text(listing_title),
            default_title=listing_title,
        )
        if detail:
            detail.sponsors.extend(listing_sponsors)
            # Label summits and forums clearly if the page/category contains those words.
            if not detail.category:
                if re.search(r"summit", detail.title + " " + href, re.I):
                    detail.category = "Summits"
                elif re.search(r"forum", detail.title + " " + href, re.I):
                    detail.category = "Forums"
            add_politico_known_partners(detail)
            events.append(detail)
            continue

        # IMPORTANT: do not create POLITICO events from broad listing-card fallback dates.
        # POLITICO pages often include repeated agenda/related-event snippets, and those
        # snippets caused multiple unrelated events to be incorrectly dated 24 September
        # 2026 / 8:15 am. If the detail page does not expose a reliable event-level date,
        # we skip it here and rely only on explicit official safeguards below.
        continue
    add_politico_fallbacks(events, scraper)
    for event in events:
        add_politico_known_partners(event)
        event.sponsors = dedupe_sponsors(event.sponsors)
    return events



def remove_unreliable_politico_dates(events: list[Event]) -> list[Event]:
    """Drop POLITICO events whose date looks like a reused agenda/listing date.

    The dashboard should never keep a POLITICO event when the scraper only found a
    generic 24 September 2026 / 8:15 am date from unrelated modules. Known official
    event pages, such as Health Care Summit 2026 and Energy & Climate Forum, are
    protected by KNOWN_EVENT_FIXES before this guard runs.
    """
    cleaned: list[Event] = []
    for event in events:
        if event.organization.lower() == "politico":
            fix = event_fix_for(event.title, event.url)
            if fix:
                cleaned.append(event)
                continue
            blob = " ".join([event.date or "", event.date_text or "", event.time_text or "", event.description or ""]).lower()
            # This is the recurring false date observed on POLITICO pages.
            if event.date == "2026-09-24" and ("8:15" in blob or "24 sep" in blob or "september 24" in blob):
                print(f"[drop] POLITICO unreliable reused date: {event.title} | {event.url}")
                continue
            # If the title itself is generic, skip rather than polluting the dashboard.
            if is_bad_title(event.title):
                print(f"[drop] POLITICO bad title: {event.title} | {event.url}")
                continue
        cleaned.append(event)
    return cleaned

def discover_euronews_event_links(scraper: Scraper) -> set[str]:
    links = {
        "https://events.euronews.com/health_summit_2026",
    }
    seeds = ["https://events.euronews.com/events", "https://events.euronews.com/"]
    for seed in seeds:
        soup = scraper.soup(seed)
        if not soup:
            continue
        for a in soup.find_all("a", href=True):
            href = urljoin(seed, a["href"]).split("#")[0]
            parsed = urlparse(href)
            if parsed.netloc == "events.euronews.com" and parsed.path not in {"/", "/events"}:
                if not any(x in parsed.path for x in ["/speaker/", "/session/", "/privacy", "/terms"]):
                    links.add(href)
    # Try sitemap if present.
    for sm in ["https://events.euronews.com/sitemap.xml", "https://events.euronews.com/sitemap_index.xml"]:
        xml = scraper.get(sm)
        if not xml:
            continue
        for m in re.finditer(r"https://events\.euronews\.com/[^<\s]+", xml):
            href = m.group(0).strip()
            if not any(x in href for x in ["/speaker/", "/session/"]):
                links.add(href)
    return links


def scrape_euronews(scraper: Scraper) -> list[Event]:
    events: list[Event] = []
    for href in sorted(discover_euronews_event_links(scraper)):
        ev = extract_event_from_detail(scraper, "Euronews", href, "Euronews Events")
        if ev:
            events.append(ev)
    return events



def is_actual_event(event: Event) -> bool:
    title_low = event.title.lower()
    # Exclude logos/news/insights/editorial pages that have dates but are not event pages.
    if any(x in title_low for x in ["insight", "insights", "news", "blog", "article", "press release", "vacancy", "opinion", "analysis"]):
        return False
    event_words = ["conference", "summit", "forum", "roundtable", "webinar", "debate", "workshop", "dialogue", "event", "awards", "meeting"]
    if any(w in title_low for w in event_words):
        return True
    # Some official conference sites have a concise title but still have event dates/venue.
    if event.venue or event.city:
        return any(w in (event.description or "").lower() for w in event_words)
    return False


def merge_partner_page_sponsors(scraper: Scraper, event: Event, candidate_urls: Iterable[str]) -> None:
    """For conference microsites, sponsors/partners may live on a separate Partners page."""
    for href in candidate_urls:
        soup = scraper.soup(href)
        if not soup:
            continue
        event.sponsors.extend(extract_sponsors_from_soup(soup, href))



def scrape_logos(scraper: Scraper) -> list[Event]:
    """Only track the European Defence & Security Conference 2026 for Logos.

    The general logos site mixes events with insights/news pages, and other conference microsites
    have previously created false positives. Per dashboard scope, keep this source limited to the
    official EDSC site. At the moment, the EDSC page shows historical partner logos from the 5th
    edition, so those are intentionally ignored. Future 2026 partners/sponsors published on the
    same official site will be collected by extract_defsec_current_sponsors().
    """
    url = "https://defencesecurityconference.eu/"
    soup = scraper.soup(url)
    if not soup:
        return []

    lines = clean_lines(soup)
    full_text = clean(" ".join(lines))
    date_text = infer_date_text_with_year(full_text, url, "European Defence & Security Conference 2026") or "29 October 2026"
    date_iso = parse_iso_date(date_text)
    if not date_iso or not in_range(date_iso):
        return []

    venue = "Egmont Palace"
    city = "Brussels"
    if "Egmont Palace" not in full_text:
        venue = ""
    if "Brussels" not in full_text:
        city = city_from_text(full_text)

    sponsors = extract_defsec_current_sponsors(soup, url)

    # Scan a future/current partners page if the site adds one, but ignore anything labelled as a past edition.
    partner_links: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a.get("href", "")).split("#")[0]
        label = clean(a.get_text(" ", strip=True))
        if "defencesecurityconference.eu" not in urlparse(href).netloc.lower():
            continue
        if re.search(r"partners?|sponsors?", href + " " + label, re.I) and not is_previous_edition_context(href + " " + label):
            partner_links.add(href)
    for href in sorted(partner_links):
        partner_soup = scraper.soup(href)
        if not partner_soup:
            continue
        partner_text = clean(partner_soup.get_text(" ", strip=True))[:2000]
        if is_previous_edition_context(partner_text):
            continue
        sponsors.extend(extract_defsec_current_sponsors(partner_soup, href))

    return [Event(
        organization="Logos",
        title="European Defence & Security Conference 2026",
        date=date_iso,
        date_text=date_text,
        city=city,
        venue=venue,
        category="Defence & Security",
        url=url,
        description=extract_description_from_soup(soup, "European Defence & Security Conference 2026"),
        sponsors=dedupe_sponsors(sponsors),
        confidence="high",
    )]


def apply_manual_sponsors(events: list[Event]) -> None:
    if not MANUAL_SPONSORS_FILE.exists():
        return
    with MANUAL_SPONSORS_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            org = clean(row.get("organization", ""))
            title_contains = clean(row.get("title_contains", ""))
            event_date = clean(row.get("event_date", ""))
            sponsor_name = normalize_company_name(row.get("sponsor", ""))
            role = clean(row.get("role", "")) or "Sponsor / partner"
            source_url = clean(row.get("source_url", ""))
            if not sponsor_name:
                continue
            for event in events:
                if org and org.lower() != event.organization.lower():
                    continue
                if title_contains and title_contains.lower() not in event.title.lower():
                    continue
                if event_date and event_date != event.date:
                    continue
                event.sponsors.append(Sponsor(
                    name=sponsor_name,
                    role=role,
                    source_url=source_url or event.url,
                    extraction="manual",
                    confidence="high",
                ))


def dedupe_events(events: Iterable[Event]) -> list[Event]:
    merged: dict[str, Event] = {}
    for event in events:
        if not event.title or not event.date or not in_range(event.date):
            continue
        key = "|".join([
            event.organization.lower(),
            re.sub(r"\W+", "", event.title.lower())[:90],
            event.date,
        ])
        if key not in merged:
            merged[key] = event
            continue
        existing = merged[key]
        if event.url and (not existing.url or len(event.url) > len(existing.url)):
            existing.url = event.url
        if event.description and len(event.description) > len(existing.description):
            existing.description = event.description
        if event.city and not existing.city:
            existing.city = event.city
        if event.venue and not existing.venue:
            existing.venue = event.venue
        if event.category and not existing.category:
            existing.category = event.category
        existing.sponsors.extend(event.sponsors)

    for event in merged.values():
        seen = set()
        sponsors = []
        for sponsor in event.sponsors:
            sponsor.name = normalize_company_name(sponsor.name)
            key = re.sub(r"\W+", "", sponsor.name).lower()
            if key and key not in seen and is_plausible_sponsor(sponsor.name, require_private=True):
                seen.add(key)
                sponsors.append(sponsor)
        event.sponsors = sponsors

    return sorted(merged.values(), key=lambda e: (e.date, e.organization.lower(), e.title.lower()))


def build_payload(events: list[Event]) -> dict:
    payload_events = []
    for event in events:
        d = asdict(event)
        d["id"] = event.event_id
        d["sponsors"] = [asdict(s) for s in event.sponsors]
        payload_events.append(d)
    return {
        "last_updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date_window": {"start": START_DATE.isoformat(), "end": END_DATE.isoformat()},
        "official_sources": [
            "https://www.politico.eu/events/",
            "https://events.politico.com/",
            "https://events.euractiv.com/",
            "https://events.euronews.com/events",
            "https://events.theparliamentmagazine.eu/",
            "https://defencesecurityconference.eu/",
        ],
        "source_notes": [
            "Only public official source pages and their linked event detail pages are scraped.",
            "Logos is intentionally limited to the official European Defence & Security Conference 2026 site to avoid mixing in Logos insights/news pages or unrelated conference microsites.",
            "Events are scraped for the full 2026 year by default; the dashboard automatically shows upcoming events first and moves past events into the Past view. POLITICO dates are accepted only from event-level metadata, explicit official safeguards, or reliable detail-page labels to prevent repeated listing/agenda dates from being reused.",
            "Sponsors mean private companies, trade associations, coalitions or other entities that sponsor, present, support, partner with, host or co-organise the event with the tracked media organisation.",
            "Sponsor extraction is best-effort. The scraper reads labelled text, logo alt/title/src names and explicit organiser/partner statements; fully image-only logos may still need validation in data/manual_sponsors.csv.",
            "Sponsor confidence is high for manual entries, medium for labelled text/logo extraction, and low for affiliation/programme inference.",
        ],
        "events": payload_events,
    }


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    scraper = Scraper()
    all_events: list[Event] = []
    scrapers = [scrape_politico, scrape_euractiv, scrape_the_parliament, scrape_euronews, scrape_logos]
    for fn in scrapers:
        try:
            events = fn(scraper)
            print(f"[ok] {fn.__name__}: {len(events)} events")
            all_events.extend(events)
        except Exception as exc:
            print(f"[warn] {fn.__name__} failed: {exc}")
    apply_known_event_fixes(all_events)
    all_events = remove_unreliable_politico_dates(all_events)
    events = dedupe_events(all_events)
    apply_manual_sponsors(events)
    apply_known_event_fixes(events)
    events = remove_unreliable_politico_dates(events)
    events = dedupe_events(events)
    payload = build_payload(events)
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[ok] wrote {len(events)} events to {OUTPUT_FILE}")
    with_sponsors = sum(1 for e in events if e.sponsors)
    print(f"[ok] sponsor coverage: {with_sponsors}/{len(events)} events")


if __name__ == "__main__":
    main()
