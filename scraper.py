#!/usr/bin/env python3
"""
EU media events dashboard scraper.

Official sources covered:
- POLITICO Europe events: https://www.politico.eu/events/
- Euractiv Events: https://events.euractiv.com/
- Euronews Events: https://events.euronews.com/events plus discovered event microsites
- The Parliament Magazine Events: https://events.theparliamentmagazine.eu/
- logos / BBE conference properties and logos pages

Output:
  data/events.json

Default date window:
  2026-01-01 through 2026-12-31.
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
    "Mozilla/5.0 (compatible; EUEventDashboard/3.0; +https://github.com/commsebs-rgb/eu-media-events-dashboard)",
)
REQUEST_DELAY_SECONDS = float(os.getenv("REQUEST_DELAY_SECONDS", "0.8"))
TIMEOUT_SECONDS = int(os.getenv("TIMEOUT_SECONDS", "30"))

MONTH_RE = (
    r"January|February|March|April|May|June|July|August|September|October|"
    r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
DATE_PATTERNS = [
    re.compile(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{4}\b"),
    re.compile(rf"\b(?:{MONTH_RE})\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b", re.I),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTH_RE})\s+\d{{4}}\b", re.I),
    re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b"),
    re.compile(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,?\s+" + MONTH_RE + r"\s+\d{1,2},?\s+\d{4}\b", re.I),
]

TIME_RE = re.compile(r"\b\d{1,2}[:.]\d{2}\s*(?:am|pm|AM|PM|CET|CEST|BST)?\b")

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
    "home", "program", "programme", "speakers", "partners", "partner with us", "about",
    "about us", "contact", "privacy", "terms", "legal", "log in", "register", "learn more",
    "language", "resource center", "newsletter", "subscribe", "news", "videos", "media",
    "last edition", "get in touch", "calendar", "sign up", "all events", "upcoming events",
    "past events", "become a sponsor", "sponsorship opportunities", "image", "share this event",
    "add to calendar", "google calendar", "outlook calendar", "apple calendar", "yahoo calendar",
    "ics export", "cookie policy", "copyright", "i accept", "search", "filter", "all", "next",
    "previous", "more events", "contact us",
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
]

GENERIC_IMAGE_ALTS = {
    "image", "logo", "event", "events", "parliament events", "euronews events home - calendar page",
    "star_divider", "previous", "next", "speaker", "speakers", "gallery", "slide 1 of 25",
}

TITLE_NOISE = {"events calendar", "parliament events", "euronews events", "home", "calendar"}

KNOWN_PRIVATE_ENTITIES = {
    "visa", "tiktok", "tik tok", "sanofi", "bayer", "uber", "qualcomm", "sobi", "repsol",
    "horse technologies", "fuelseurope", "avio aero", "ge aerospace", "euturbines",
    "international copper association europe", "transport & environment", "norsk hydro", "microsoft",
    "philips", "besins", "chiesi", "corteva", "automotive coalition for europe",
}


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
    return name


def title_from_image_src(src: str) -> str:
    """Best-effort sponsor name from logo filenames when image alt text is empty."""
    if not src:
        return ""
    stem = src.split("?")[0].rstrip("/").split("/")[-1]
    stem = re.sub(r"\.(svg|png|jpe?g|webp)$", "", stem, flags=re.I)
    stem = re.sub(r"(?:logo|sponsor|partner|colour|color|white|black|transparent|horizontal|vertical|new|final|copy|\d{2,})", " ", stem, flags=re.I)
    stem = re.sub(r"[-_+]+", " ", stem)
    stem = clean(stem)
    if not stem or len(stem) < 3:
        return ""
    # Keep acronyms upper-case, title case normal words.
    words = [w.upper() if w.isupper() and len(w) <= 6 else w.capitalize() for w in stem.split()]
    return normalize_company_name(" ".join(words))


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
    if not low or low in NAV_NOISE:
        return True
    if len(low) < 6 or len(low) > 220:
        return True
    if low.startswith(("image:", "http", "www.")):
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
    if low in NAV_NOISE or low in GENERIC_IMAGE_ALTS or low in TITLE_NOISE:
        return False
    if any(x in low for x in ["cookie", "privacy", "terms", "copyright", "contact us", "sponsorship opportunities"]):
        return False
    if SPONSOR_HEADING_RE.fullmatch(label):
        return False
    if len(label.split()) > 12:
        return False
    # Filter public institutions and the media hosts themselves. Keep private trade associations,
    # coalitions and industry bodies because the dashboard treats co-organisers/partners as sponsors.
    if require_private and is_public_or_media_org(label):
        return False
    company_signals = [
        "ltd", "limited", "gmbh", "sa", "ag", "nv", "inc", "corp", "company", "group",
        "technologies", "technology", "europe", "foundation", "association", "alliance", "coalition",
        "forum", "federation", "institute", "council", "union", "industries", "energy", "bank",
        "aerospace", "pharma", "mobility", "systems", "power", "fuels", "copper", "transport",
    ]
    if low in KNOWN_PRIVATE_ENTITIES:
        return True
    if any(s in low for s in company_signals):
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


def extract_sponsors_from_page(scraper: Scraper, url: str) -> list[Sponsor]:
    if not url:
        return []
    soup = scraper.soup(url)
    if not soup:
        return []
    return extract_sponsors_from_soup(soup, url)


def extract_sponsors_from_soup(soup: BeautifulSoup, source_url: str) -> list[Sponsor]:
    candidates: list[Sponsor] = []

    def add_candidate(name: str, role: str, extraction: str, confidence: str = "medium") -> None:
        name = normalize_company_name(name)
        if is_plausible_sponsor(name):
            candidates.append(Sponsor(name=name, role=role, source_url=source_url, extraction=extraction, confidence=confidence))

    # Pattern-based extraction from full text. This catches POLITICO "Presented By",
    # Euronews "supported by", Euractiv "Organised by", etc.
    full_text = clean(soup.get_text("\n"))
    for pattern, role in SPONSOR_PATTERNS:
        for m in pattern.finditer(full_text):
            raw = m.group(1)
            # Some official pages write "Organised by X: · member 1". Keep the organiser name.
            raw = re.split(r"\s*(?:Media Partner|Media Partners|Sponsors?|Partners?)\s*:?", raw, 1, flags=re.I)[0]
            add_candidate(raw, role, "auto-labelled-text", "medium")

    # Line-based extraction for headings followed by company names.
    lines = clean_lines(soup)
    for i, line in enumerate(lines):
        if SPONSOR_HEADING_RE.search(line):
            for w in lines[i + 1 : min(i + 10, len(lines))]:
                if w.lower() in NAV_NOISE or re.search(r"^(programme|speakers|event details|related events|contact)$", w, re.I):
                    break
                # Avoid grabbing long descriptions.
                if len(w) <= 90:
                    add_candidate(w, "Sponsor / partner", "auto-heading-nearby", "medium")

    # Sponsor-related sections and nearby headings; capture link text, img alt/title and logo filenames.
    for node in soup.find_all(string=SPONSOR_HEADING_RE):
        parent = node.parent if isinstance(node.parent, Tag) else None
        if not parent:
            continue
        blocks: list[Tag] = [parent]
        sib = parent.find_next_sibling()
        for _ in range(10):
            if not isinstance(sib, Tag):
                break
            blocks.append(sib)
            if sib.name in {"h1", "h2"} and not SPONSOR_HEADING_RE.search(clean(sib.get_text(" "))):
                break
            sib = sib.find_next_sibling()
        for block in blocks:
            for h in block.find_all(["h2", "h3", "h4", "strong", "b", "a", "span"]):
                label = normalize_company_name(h.get_text(" ", strip=True))
                add_candidate(label, "Sponsor / partner", "auto-section-text", "medium")
            for img in block.find_all("img"):
                for value in [img.get("alt", ""), img.get("title", ""), title_from_image_src(img.get("src", ""))]:
                    add_candidate(value, "Sponsor / partner", "auto-logo", "medium")

    # Generic image alts / filenames. Include only strong-looking names.
    for img in soup.find_all("img"):
        values = [img.get("alt", ""), img.get("title", ""), title_from_image_src(img.get("src", ""))]
        for value in values:
            name = normalize_company_name(value)
            if not is_plausible_sponsor(name):
                continue
            low = name.lower()
            if low in KNOWN_PRIVATE_ENTITIES or any(signal in low for signal in [
                "qualcomm", "sanofi", "bayer", "sobi", "philips", "microsoft", "uber", "fuelseurope",
                "chiesi", "corteva", "norsk hydro", "besins", "visa", "tiktok", "tik tok", "repsol",
                "horse", "automotive coalition", "avio", "copper association", "euturbines"
            ]):
                add_candidate(name, "Sponsor / partner", "auto-logo", "medium")

    # Parliament/other programmes sometimes explicitly say "our Platinum partner" and then
    # list that partner's representative. Extract the affiliation from the immediate speaker block.
    for i, line in enumerate(lines):
        if re.search(r"\b(platinum|gold|silver|strategic|commercial|content) partner\b", line, re.I):
            window = lines[i : min(i + 12, len(lines))]
            for w in window:
                m = re.search(r"\b(?:Director|Head|Vice President|SVP|CEO|Chief|Manager|President|Secretary General|Executive Director)[^,]*\s+([A-Z][A-Za-z0-9&.'’\- ]{2,80})$", w)
                if m:
                    add_candidate(m.group(1), "Inferred partner", "auto-programme-inference", "low")

    # If there is a Sponsors section with image-only logos, infer likely sponsors/co-organisers from
    # repeated private-company speaker affiliations. This is deliberately low confidence.
    if any(line.lower() == "sponsors" for line in lines) and not candidates:
        for i, line in enumerate(lines):
            # Speaker cards often appear as Name / Job title / Organisation on consecutive lines.
            if i + 2 < len(lines):
                org = lines[i + 2]
                if is_plausible_sponsor(org) and not is_public_or_media_org(org):
                    add_candidate(org, "Possible sponsor / partner", "auto-affiliation-inference", "low")

    # Dedupe and keep the most confident role per sponsor.
    out: list[Sponsor] = []
    seen: dict[str, int] = {}
    rank = {"high": 3, "medium": 2, "low": 1}
    for s in candidates:
        name = normalize_company_name(s.name)
        key = re.sub(r"\W+", "", name).lower()
        if not key:
            continue
        if key in seen:
            existing = out[seen[key]]
            if rank.get(s.confidence, 0) > rank.get(existing.confidence, 0):
                out[seen[key]] = s
            continue
        s.name = name
        seen[key] = len(out)
        out.append(s)
    return out[:24]


def extract_event_from_detail(scraper: Scraper, organization: str, url: str, default_category: str = "", default_title: str = "") -> Optional[Event]:
    soup = scraper.soup(url)
    if not soup:
        return None
    lines = clean_lines(soup)
    full_text = clean(" ".join(lines))

    title = ""
    title_candidates: list[str] = []
    for tag in soup.find_all(["h1", "h2"], limit=8):
        txt = clean(tag.get_text(" ", strip=True))
        if txt:
            title_candidates.append(txt)
    og = soup.find("meta", property="og:title")
    if og:
        title_candidates.append(clean(og.get("content", "")))
    if soup.title:
        title_candidates.append(clean(soup.title.get_text(" ", strip=True)))
    if default_title:
        title_candidates.insert(0, default_title)
    for candidate in title_candidates:
        candidate = re.sub(r"\s+[–|-]\s+(?:Parliament Events|Events Calendar|POLITICO.*|Euronews.*)$", "", candidate).strip()
        if candidate.lower() not in TITLE_NOISE and not is_bad_title(candidate):
            title = candidate
            break
    if not title:
        return None

    date_text = next_line_after(lines, ["Start Date", "When", "Date", "Date & Time"])
    if not first_date_text(date_text):
        date_text = first_date_text(full_text)
    else:
        date_text = first_date_text(date_text) or date_text
    date_iso = parse_iso_date(date_text)
    if not date_iso or not in_range(date_iso):
        return None

    end_text = next_line_after(lines, ["End Date"])
    end_iso = parse_iso_date(first_date_text(end_text)) if end_text else ""
    time_text = ""
    time_source = full_text[:1500]
    m_time = TIME_RE.search(time_source)
    if m_time:
        time_text = m_time.group(0)

    city = next_line_after(lines, ["Location", "Where"])
    if city and len(city) > 60:
        city = city_from_text(city)
    if not city:
        city = city_from_text(full_text)
    venue = next_line_after(lines, ["Venue"])
    if len(venue) > 120:
        venue = ""

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
        city=city_from_text(city) or city,
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

    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        if "/event/info/" not in href:
            continue
        title = clean(a.get_text(" ", strip=True))
        if is_bad_title(title):
            continue
        context = surrounding_text(a)
        # The page is often a compact table; if surrounding text does not include date, search nearby page text.
        if not first_date_text(context):
            page_text = clean(soup.get_text(" "))
            idx = page_text.find(title)
            if idx >= 0:
                context = page_text[idx : idx + 500]
        date_text = first_date_text(context)
        date_iso = parse_iso_date(date_text)
        if not date_iso or not in_range(date_iso):
            continue
        detail = extract_event_from_detail(scraper, "Euractiv", href, category_from_text(context), default_title=title)
        if detail:
            # Preserve useful category/city from table if detail page is sparse.
            detail.category = detail.category or category_from_text(context)
            detail.city = detail.city or city_from_text(context)
            events.append(detail)
        else:
            events.append(Event(
                organization="Euractiv",
                title=title,
                date=date_iso,
                date_text=date_text,
                city=city_from_text(context),
                category=category_from_text(context),
                url=href,
                sponsors=extract_sponsors_from_page(scraper, href),
                confidence="high",
            ))
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


def scrape_politico(scraper: Scraper) -> list[Event]:
    events: list[Event] = []
    urls = ["https://www.politico.eu/events/"] + [f"https://www.politico.eu/events/page/{i}/" for i in range(2, 12)]
    seen_detail: set[str] = set()

    for page_url in urls:
        soup = scraper.soup(page_url)
        if not soup:
            continue
        for a in soup.find_all("a", href=True):
            href = urljoin(page_url, a["href"]).split("#")[0]
            parsed = urlparse(href)
            if parsed.netloc not in {"www.politico.eu", "politico.eu"}:
                continue
            if "/event/" not in parsed.path:
                continue
            title = clean(a.get_text(" ", strip=True))
            if is_bad_title(title):
                continue
            if href in seen_detail:
                continue
            seen_detail.add(href)

            context = surrounding_text(a)
            date_text = first_date_text(context)
            sponsor_candidates = extract_sponsors_from_soup(BeautifulSoup(f"<div>{context}</div>", "lxml"), href)
            # Fetch detail page for exact date/sponsor block; if blocked or no details, use listing.
            detail = extract_event_from_detail(scraper, "POLITICO", href, category_from_text(context), default_title=title)
            if detail:
                # Add listing-level "Presented By" if detail page did not expose a sponsor block.
                detail.sponsors.extend(sponsor_candidates)
                events.append(detail)
            else:
                date_iso = parse_iso_date(date_text)
                if date_iso and in_range(date_iso):
                    events.append(Event(
                        organization="POLITICO",
                        title=title,
                        date=date_iso,
                        date_text=date_text,
                        time_text=(TIME_RE.search(context).group(0) if TIME_RE.search(context) else ""),
                        city=city_from_text(context),
                        category=category_from_text(context),
                        url=href,
                        description=context[:700],
                        sponsors=sponsor_candidates,
                        confidence="medium",
                    ))
    return events


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


def scrape_logos(scraper: Scraper) -> list[Event]:
    # logos does not publish one clean public event calendar. These are official logos/BBE-owned or
    # legacy conference properties that the scraper can verify directly.
    seeds = [
        ("https://logos-pa.com/", "logos"),
        ("https://logos-pa.com/news/", "logos"),
        ("https://defencesecurityconference.eu/", "logos"),
        ("https://defencesecurityconference.eu/about-us/", "logos"),
        ("https://spaceconference.eu/", "logos"),
    ]
    events: list[Event] = []
    seen = set()
    for url, org in seeds:
        soup = scraper.soup(url)
        if not soup:
            continue
        # JSON-LD first.
        for ev in extract_events_from_jsonld(soup, org, url):
            if ev.url not in seen:
                ev.sponsors = extract_sponsors_from_soup(soup, url)
                events.append(ev)
                seen.add(ev.url)
        # Generic detail-like pages.
        ev = extract_event_from_detail(scraper, org, url)
        if ev and ev.event_id not in seen:
            events.append(ev)
            seen.add(ev.event_id)
        # Also crawl obvious internal conference/news links.
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"]).split("#")[0]
            if href in seen:
                continue
            parsed = urlparse(href)
            if parsed.netloc not in {"logos-pa.com", "www.logos-pa.com", "defencesecurityconference.eu", "spaceconference.eu", "www.spaceconference.eu"}:
                continue
            label = clean(a.get_text(" ", strip=True))
            context = surrounding_text(a)
            if not (first_date_text(context) or "2026" in context or "conference" in label.lower()):
                continue
            ev2 = extract_event_from_detail(scraper, org, href)
            if ev2:
                events.append(ev2)
                seen.add(href)
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
            "https://events.euractiv.com/",
            "https://events.euronews.com/events",
            "https://events.theparliamentmagazine.eu/",
            "https://logos-pa.com/",
            "https://defencesecurityconference.eu/",
            "https://spaceconference.eu/",
        ],
        "source_notes": [
            "Only public official source pages and their linked event detail pages are scraped.",
            "Events are filtered to the configured date window, defaulting to all of 2026.",
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
    events = dedupe_events(all_events)
    apply_manual_sponsors(events)
    events = dedupe_events(events)
    payload = build_payload(events)
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[ok] wrote {len(events)} events to {OUTPUT_FILE}")
    with_sponsors = sum(1 for e in events if e.sponsors)
    print(f"[ok] sponsor coverage: {with_sponsors}/{len(events)} events")


if __name__ == "__main__":
    main()
