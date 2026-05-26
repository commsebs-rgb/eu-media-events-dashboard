#!/usr/bin/env python3
"""
EU policy events dashboard scraper.

Sources covered:
- POLITICO Live / POLITICO event landing pages
- Euractiv Events
- The Parliament Magazine Events
- Euronews Events landing pages
- logos / Business Bridge Europe conference pages

The scraper is intentionally conservative:
- It reads public pages only.
- It uses a slow request rate.
- It stores the source URL and an extraction confidence for every event.
- Sponsor/partner extraction is heuristic because many websites render partner logos as images.
  You can add guaranteed sponsor mappings in data/manual_sponsors.csv.

Output:
  data/events.json
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import dateparser
import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUTPUT_FILE = DATA_DIR / "events.json"
MANUAL_SPONSORS_FILE = DATA_DIR / "manual_sponsors.csv"

USER_AGENT = (
    "Mozilla/5.0 (compatible; EUEventDashboard/1.0; "
    "+https://github.com/your-org/eu-event-dashboard)"
)

REQUEST_DELAY_SECONDS = 1.0
TIMEOUT_SECONDS = 25

MONTH_RE = (
    r"January|February|March|April|May|June|July|August|September|October|"
    r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
DATE_PATTERNS = [
    # 27-05-2026
    re.compile(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b"),
    # June 2, 2026
    re.compile(rf"\b(?:{MONTH_RE})\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b", re.I),
    # 2 June 2026
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTH_RE})\s+\d{{4}}\b", re.I),
    # 29 October 2026
    re.compile(rf"\b\d{{1,2}}\s+(?:{MONTH_RE})\s+\d{{4}}\b", re.I),
    # 24.03.26
    re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b"),
]

CATEGORY_HINTS = [
    "Policy Events",
    "Summits",
    "PM+ Talks",
    "Awards",
    "Health and Consumers",
    "Energy and Environment",
    "Economy and Jobs",
    "Agriculture and Food",
    "Technology",
    "Digital",
    "Transport",
    "Politics",
    "Economy",
    "Health",
    "Energy",
    "Environment",
    "Digital & Media",
]

LOCATION_HINTS = [
    "Brussels",
    "Strasbourg",
    "Online",
    "Helsinki",
    "Nicosia",
    "Prague",
    "Valetta",
    "Warsaw",
    "Bologna",
    "London",
    "Belgium",
]

SPONSOR_HEADING_RE = re.compile(
    r"\b(partners?|sponsors?|supporters?|supported by|in partnership with|"
    r"media partners?|institutional partners?|knowledge partners?)\b",
    re.I,
)

NAV_NOISE = {
    "home", "program", "programme", "speakers", "partners", "partner with us",
    "about", "about us", "contact", "privacy", "terms", "legal", "log in",
    "register", "learn more", "language", "resource center", "newsletter",
    "subscribe", "news", "videos", "media", "last edition", "get in touch",
}


@dataclass
class Sponsor:
    name: str
    role: str = "Sponsor / partner"
    source_url: str = ""
    extraction: str = "auto"


@dataclass
class Event:
    organization: str
    title: str
    date: str  # ISO date YYYY-MM-DD
    date_text: str = ""
    time_text: str = ""
    city: str = ""
    venue: str = ""
    category: str = ""
    url: str = ""
    description: str = ""
    sponsors: list[Sponsor] = field(default_factory=list)
    confidence: str = "medium"

    @property
    def event_id(self) -> str:
        key = "|".join([self.organization, self.title.lower(), self.date, self.url])
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


class Scraper:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def get(self, url: str) -> Optional[str]:
        try:
            resp = self.session.get(url, timeout=TIMEOUT_SECONDS)
            time.sleep(REQUEST_DELAY_SECONDS)
            resp.raise_for_status()
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


def clean_lines(soup: BeautifulSoup) -> list[str]:
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text("\n")
    return [clean(line) for line in text.splitlines() if clean(line)]


def first_date_text(text: str) -> str:
    for pattern in DATE_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(0)
    return ""


def parse_iso_date(text: str) -> str:
    if not text:
        return ""
    # Handle two common numeric formats explicitly.
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            pass

    parsed = dateparser.parse(
        text,
        languages=["en"],
        settings={
            "DATE_ORDER": "DMY",
            "PREFER_DATES_FROM": "future",
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )
    return parsed.date().isoformat() if parsed else ""


def link_for_title(soup: BeautifulSoup, title: str, base_url: str) -> str:
    title_norm = clean(title).lower()
    if not title_norm:
        return base_url
    best = ""
    for a in soup.find_all("a", href=True):
        label = clean(a.get_text(" ", strip=True)).lower()
        href = urljoin(base_url, a["href"])
        if label and (title_norm == label or title_norm in label or label in title_norm):
            # Prefer same-domain event links.
            best = href
            if "event" in href or "conference" in href:
                return href
    return best or base_url


def category_from_text(text: str) -> str:
    hits = [c for c in CATEGORY_HINTS if re.search(rf"\b{re.escape(c)}\b", text, re.I)]
    return ", ".join(dict.fromkeys(hits[:3]))


def city_from_text(text: str) -> str:
    for city in LOCATION_HINTS:
        if re.search(rf"\b{re.escape(city)}\b", text, re.I):
            return city
    return ""


def extract_description(lines: list[str], start_index: int, max_lines: int = 3) -> str:
    desc_parts: list[str] = []
    skip_re = re.compile(r"^(image|upcoming|free|\d+|search|category|location|venue)$", re.I)
    for line in lines[start_index : start_index + 12]:
        if skip_re.match(line):
            continue
        if first_date_text(line):
            continue
        if line in CATEGORY_HINTS or line in LOCATION_HINTS:
            continue
        if len(line) > 40:
            desc_parts.append(line)
        if len(desc_parts) >= max_lines:
            break
    return clean(" ".join(desc_parts))[:500]


def extract_sponsors_from_page(scraper: Scraper, url: str) -> list[Sponsor]:
    """Best-effort sponsor/partner extraction from public event pages."""
    if not url:
        return []
    soup = scraper.soup(url)
    if not soup:
        return []

    candidates: list[str] = []
    blocks: list[BeautifulSoup] = []

    # 1) Find headings/sections that look sponsor-related and inspect nearby siblings.
    for node in soup.find_all(string=SPONSOR_HEADING_RE):
        parent = node.parent
        if parent:
            blocks.append(parent)
            sib = parent.find_next_sibling()
            for _ in range(6):
                if not sib:
                    break
                blocks.append(sib)
                sib = sib.find_next_sibling()

    # 2) If there is a nav link to a partner tab on the same page, the content may already be present.
    for tag in blocks:
        for img in tag.find_all("img"):
            alt = clean(img.get("alt", ""))
            if alt and alt.lower() not in NAV_NOISE:
                candidates.append(alt)

        for a in tag.find_all("a"):
            label = clean(a.get_text(" ", strip=True))
            if is_plausible_sponsor(label):
                candidates.append(label)

        # Some partner names are plain text under headings.
        for line in clean(tag.get_text("\n")).split("\n"):
            line = clean(line)
            if is_plausible_sponsor(line):
                candidates.append(line)

    # 3) Pattern-based extraction: "in partnership with X", "supported by X".
    full_text = clean(soup.get_text(" "))
    for pattern in [
        r"in partnership with ([A-Z][A-Za-z0-9&., \-]{2,80})",
        r"supported by ([A-Z][A-Za-z0-9&., \-]{2,80})",
        r"sponsored by ([A-Z][A-Za-z0-9&., \-]{2,80})",
    ]:
        for m in re.finditer(pattern, full_text, re.I):
            name = clean(m.group(1))
            # Stop at sentence boundaries if the regex grabbed too much.
            name = re.split(r"[\.;:|]", name)[0].strip()
            if is_plausible_sponsor(name):
                candidates.append(name)

    deduped = []
    seen = set()
    for name in candidates:
        normalized = re.sub(r"\W+", "", name).lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped.append(Sponsor(name=name, source_url=url, extraction="auto"))

    return deduped[:20]


def is_plausible_sponsor(label: str) -> bool:
    label = clean(label)
    if not label or len(label) < 3 or len(label) > 80:
        return False
    low = label.lower()
    if low in NAV_NOISE:
        return False
    if any(x in low for x in ["cookie", "privacy", "terms", "copyright", "contact us"]):
        return False
    if SPONSOR_HEADING_RE.fullmatch(label):
        return False
    # Avoid long paragraphs.
    if len(label.split()) > 8:
        return False
    return True


def dedupe_events(events: Iterable[Event]) -> list[Event]:
    merged: dict[str, Event] = {}
    for event in events:
        if not event.title or not event.date:
            continue
        key = "|".join([
            event.organization.lower(),
            re.sub(r"\W+", "", event.title.lower())[:80],
            event.date,
        ])
        if key in merged:
            existing = merged[key]
            # Prefer the record with a specific event URL and longer description.
            if event.url and (not existing.url or len(event.url) > len(existing.url)):
                existing.url = event.url
            if event.description and len(event.description) > len(existing.description):
                existing.description = event.description
            if event.sponsors:
                existing.sponsors.extend(event.sponsors)
        else:
            merged[key] = event

    # Dedupe sponsors per event.
    for event in merged.values():
        seen = set()
        sponsors = []
        for sponsor in event.sponsors:
            key = re.sub(r"\W+", "", sponsor.name).lower()
            if key and key not in seen:
                seen.add(key)
                sponsors.append(sponsor)
        event.sponsors = sponsors

    return sorted(merged.values(), key=lambda e: (e.date, e.organization, e.title))


def scrape_euractiv(scraper: Scraper) -> list[Event]:
    url = "https://events.euractiv.com/"
    soup = scraper.soup(url)
    if not soup:
        return []

    events: list[Event] = []

    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        if "/event/info/" not in href:
            continue
        title = clean(a.get_text(" ", strip=True))
        if not title or title.lower() in NAV_NOISE:
            continue

        # Try table row / parent context first.
        parent_text = clean(a.find_parent(["tr", "li", "div", "p"]).get_text(" ", strip=True)) if a.find_parent(["tr", "li", "div", "p"]) else ""
        page_text = clean(soup.get_text(" "))
        idx = page_text.find(title)
        context = parent_text
        if idx >= 0:
            context += " " + page_text[idx : idx + 350]

        date_text = first_date_text(context)
        date_iso = parse_iso_date(date_text)
        if not date_iso:
            continue

        category = category_from_text(context)
        city = city_from_text(context)
        sponsors = extract_sponsors_from_page(scraper, href)

        events.append(
            Event(
                organization="Euractiv",
                title=title,
                date=date_iso,
                date_text=date_text,
                city=city,
                category=category,
                url=href,
                sponsors=sponsors,
                confidence="high",
            )
        )

    return events


def scrape_the_parliament(scraper: Scraper) -> list[Event]:
    base = "https://events.theparliamentmagazine.eu/location/brussels/"
    urls = [
        base,
        "https://events.theparliamentmagazine.eu/location/brussels/page/2/?post_type=event",
        "https://events.theparliamentmagazine.eu/location/brussels/page/3/?post_type=event",
    ]
    events: list[Event] = []

    for url in urls:
        soup = scraper.soup(url)
        if not soup:
            continue
        lines = clean_lines(soup)

        # Parse the predictable text pattern:
        # Upcoming / title / category / date / time / location / description
        for i, line in enumerate(lines):
            if line.lower() != "upcoming":
                continue

            # Next meaningful line is normally the event title.
            if i + 1 >= len(lines):
                continue
            title = lines[i + 1]
            if title.lower() in NAV_NOISE or len(title) < 5:
                continue

            date_text = ""
            time_text = ""
            city = ""
            category = ""

            for j in range(i + 2, min(i + 10, len(lines))):
                if first_date_text(lines[j]):
                    date_text = first_date_text(lines[j])
                    # A time often follows the date.
                    if j + 1 < len(lines) and re.search(r"\b\d{1,2}:\d{2}\s*(am|pm)?\b", lines[j + 1], re.I):
                        time_text = lines[j + 1]
                    break

            if not date_text:
                continue

            window = " ".join(lines[i : i + 12])
            category = category_from_text(window)
            city = city_from_text(window)
            event_url = link_for_title(soup, title, url)
            sponsors = extract_sponsors_from_page(scraper, event_url) if event_url else []

            events.append(
                Event(
                    organization="The Parliament",
                    title=title,
                    date=parse_iso_date(date_text),
                    date_text=date_text,
                    time_text=time_text,
                    city=city,
                    category=category,
                    url=event_url,
                    description=extract_description(lines, i + 4),
                    sponsors=sponsors,
                    confidence="high",
                )
            )

    return events


def generic_event_page(scraper: Scraper, organization: str, url: str, category: str = "") -> list[Event]:
    soup = scraper.soup(url)
    if not soup:
        return []

    lines = clean_lines(soup)
    text = clean(" ".join(lines))

    # Title priority: h1, og:title, title tag.
    h1 = soup.find("h1")
    title = clean(h1.get_text(" ", strip=True)) if h1 else ""
    if not title:
        og = soup.find("meta", property="og:title")
        title = clean(og.get("content", "")) if og else ""
    if not title and soup.title:
        title = clean(soup.title.get_text(" ", strip=True))
    title = re.sub(r"\s+\|\s+.*$", "", title).strip()

    date_text = first_date_text(text)
    date_iso = parse_iso_date(date_text)
    if not title or not date_iso:
        return []

    # Time and location are best effort.
    time_text = ""
    m_time = re.search(r"\b\d{1,2}:\d{2}\s*(?:am|pm|CET|CEST)?\b", text, re.I)
    if m_time:
        time_text = m_time.group(0)

    city = city_from_text(text)
    sponsors = extract_sponsors_from_page(scraper, url)

    # First paragraph-like line after title for description.
    description = ""
    for line in lines:
        if len(line) > 80 and title.lower() not in line.lower():
            description = line[:500]
            break

    return [
        Event(
            organization=organization,
            title=title,
            date=date_iso,
            date_text=date_text,
            time_text=time_text,
            city=city,
            category=category or category_from_text(text),
            url=url,
            description=description,
            sponsors=sponsors,
            confidence="medium",
        )
    ]


def scrape_politico(scraper: Scraper) -> list[Event]:
    # POLITICO’s public event discovery is often event-specific rather than one clean calendar.
    # Add or remove public POLITICO event URLs here.
    pages = [
        "https://events.politico.com/event/politico-lives-competitive-europe-summit",
    ]
    events: list[Event] = []
    for url in pages:
        events.extend(generic_event_page(scraper, "POLITICO", url, "EU policy / competitiveness"))
    return events


def scrape_euronews(scraper: Scraper) -> list[Event]:
    # Euronews has an events landing page and individual campaign pages.
    # Add newly discovered Euronews event pages here when published.
    pages = [
        "https://events.euronews.com/health_summit_2026",
        "https://events.euronews.com/events",
    ]
    events: list[Event] = []
    for url in pages:
        events.extend(generic_event_page(scraper, "Euronews", url, "Euronews Events"))
    return events


def scrape_logos(scraper: Scraper) -> list[Event]:
    # logos / BBE conference properties and the logos news page.
    pages = [
        ("https://defencesecurityconference.eu/about-us/", "Defence / security"),
        ("https://spaceconference.eu/", "Space"),
        ("https://logos-pa.com/", "Public affairs"),
    ]
    events: list[Event] = []
    for url, category in pages:
        events.extend(generic_event_page(scraper, "logos", url, category))
    return events


def apply_manual_sponsors(events: list[Event]) -> None:
    if not MANUAL_SPONSORS_FILE.exists():
        return

    with MANUAL_SPONSORS_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            org = clean(row.get("organization", ""))
            title_contains = clean(row.get("title_contains", ""))
            event_date = clean(row.get("event_date", ""))
            sponsor_name = clean(row.get("sponsor", ""))
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
                event.sponsors.append(
                    Sponsor(
                        name=sponsor_name,
                        role=role,
                        source_url=source_url or event.url,
                        extraction="manual",
                    )
                )


def build_payload(events: list[Event]) -> dict:
    payload_events = []
    for event in events:
        d = asdict(event)
        d["id"] = event.event_id
        d["sponsors"] = [asdict(s) for s in event.sponsors]
        payload_events.append(d)

    return {
        "last_updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_notes": [
            "Sponsor extraction is automatic where possible and can be completed or corrected in data/manual_sponsors.csv.",
            "Some publishers use event platforms, JavaScript, or image-only partner logos; those records may need manual sponsor validation.",
            "The dashboard is refreshed by GitHub Actions every 24 hours when deployed with the included workflow.",
        ],
        "events": payload_events,
    }


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)

    scraper = Scraper()
    all_events: list[Event] = []

    scrapers = [
        scrape_euractiv,
        scrape_the_parliament,
        scrape_politico,
        scrape_euronews,
        scrape_logos,
    ]

    for fn in scrapers:
        try:
            new_events = fn(scraper)
            print(f"[ok] {fn.__name__}: {len(new_events)} events")
            all_events.extend(new_events)
        except Exception as exc:
            print(f"[warn] {fn.__name__} failed: {exc}")

    events = dedupe_events(all_events)
    apply_manual_sponsors(events)
    events = dedupe_events(events)

    payload = build_payload(events)
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[ok] wrote {len(events)} events to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
