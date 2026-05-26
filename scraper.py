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
    "previous", "more events", "contact us", "register here", "locatee", "[email protected]", "email protected",
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
    "philips", "besins", "chiesi", "corteva", "automotive coalition for europe", "adpa", "airc", "ame", "egea", "figiefa", "insurance europe", "repsol technology lab", "horse technologies", "horse powertrain",
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
    if not low or low in NAV_NOISE or low in TITLE_NOISE:
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
    city_noise = {c.lower() for c in LOCATION_HINTS} | {"brussels", "online", "nicosia", "prague", "renaissance hotel"}
    hard_noise = NAV_NOISE | GENERIC_IMAGE_ALTS | TITLE_NOISE | {"register here", "locatee", "image", "source", "open", "event details", "start date", "end date"}
    if low in hard_noise or low in city_noise:
        return False
    if "@" in label or "email protected" in low or "[email" in low:
        return False
    if any(x in low for x in ["cookie", "privacy", "terms", "copyright", "contact us", "sponsorship opportunities", "google maps", "add to calendar"]):
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
        "forum", "federation", "institute", "council", "union", "industries", "energy", "bank",
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
    mapping = {
        "tiktok": "TikTok", "tik": "TikTok", "sobi": "Sobi", "repsol": "Repsol",
        "horse": "Horse Technologies", "horsepowertrain": "Horse Powertrain", "visa": "Visa",
        "sanofi": "Sanofi", "bayer": "Bayer", "uber": "Uber", "fuelseurope": "FuelsEurope",
        "qualcomm": "Qualcomm", "microsoft": "Microsoft", "philips": "Philips",
    }
    return mapping.get(label, normalize_company_name(label.replace("-", " ").title()))


def add_sponsor_candidate(candidates: list[Sponsor], name: str, role: str, source_url: str, extraction: str, confidence: str = "medium") -> None:
    name = normalize_company_name(name)
    if is_plausible_sponsor(name):
        candidates.append(Sponsor(name=name, role=role, source_url=source_url, extraction=extraction, confidence=confidence))


def dedupe_sponsors(candidates: list[Sponsor]) -> list[Sponsor]:
    out: list[Sponsor] = []
    seen: dict[str, int] = {}
    rank = {"high": 3, "medium": 2, "low": 1}
    for s in candidates:
        name = normalize_company_name(s.name)
        key = re.sub(r"\W+", "", name).lower()
        if not key or not is_plausible_sponsor(name):
            continue
        s.name = name
        if key in seen:
            idx = seen[key]
            if rank.get(s.confidence, 0) > rank.get(out[idx].confidence, 0):
                out[idx] = s
            continue
        seen[key] = len(out)
        out.append(s)
    return out[:24]


def collect_logo_and_link_names(block: Tag, source_url: str, role: str, confidence: str = "medium") -> list[Sponsor]:
    """Collect only names that appear as links or logo metadata inside a verified sponsor/partner section."""
    candidates: list[Sponsor] = []
    host = urlparse(source_url).netloc.lower().replace("www.", "")

    for a in block.find_all("a", href=True):
        href = urljoin(source_url, a.get("href", ""))
        text = normalize_company_name(a.get_text(" ", strip=True))
        if text and text.lower() not in NAV_NOISE and text.lower() not in {"image", "register", "open", "source"}:
            add_sponsor_candidate(candidates, text, role, source_url, "auto-section-link", confidence)
        # If the link text is just an image, infer from external domain.
        parsed = urlparse(href)
        link_host = parsed.netloc.lower().replace("www.", "")
        if link_host and host not in link_host:
            add_sponsor_candidate(candidates, domain_label_from_url(href), role, source_url, "auto-section-link-domain", confidence)

    for img in block.find_all("img"):
        values = [img.get("alt", ""), img.get("title", ""), img.get("aria-label", ""), title_from_image_src(img.get("src", ""))]
        parent_link = img.find_parent("a", href=True)
        if parent_link:
            values.append(domain_label_from_url(urljoin(source_url, parent_link.get("href", ""))))
        for value in values:
            add_sponsor_candidate(candidates, value, role, source_url, "auto-section-logo", confidence)
    return candidates


def section_blocks_after_heading(soup: BeautifulSoup, heading_regex: re.Pattern[str], max_siblings: int = 8) -> list[Tag]:
    """Return blocks immediately after a verified sponsor/partner heading.

    This deliberately stops at Related Events, Event Details, Programme, Speakers, Location, etc.
    so that the dashboard does not confuse cities, CTAs or speaker affiliations for sponsors.
    """
    blocks: list[Tag] = []
    stop_re = re.compile(r"\b(related events|event details|programme|program|speakers|schedule|location|venue|contact|share this event|add to calendar|on the same topic)\b", re.I)

    # Heading tags with exact or near-exact sponsor/partner labels.
    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "strong", "b", "p", "div"]):
        label = clean(h.get_text(" ", strip=True))
        if not label or not heading_regex.search(label):
            continue
        # Avoid matching an event title such as "Media Partnership: ..."; we need section headings.
        if len(label) > 80 and not re.fullmatch(r".*(sponsors?|partners?|supporters?).*", label, flags=re.I):
            continue
        parent = h.parent if isinstance(h.parent, Tag) else h
        blocks.append(parent)
        sib = parent.find_next_sibling()
        for _ in range(max_siblings):
            if not isinstance(sib, Tag):
                break
            text = clean(sib.get_text(" ", strip=True))
            if stop_re.search(text):
                break
            blocks.append(sib)
            sib = sib.find_next_sibling()
    return blocks


def extract_labelled_text_sponsors(soup: BeautifulSoup, source_url: str) -> list[Sponsor]:
    """Extract sponsors only from explicit 'sponsored by / supported by / presented by' statements."""
    candidates: list[Sponsor] = []
    full_text = clean(soup.get_text("\n"))
    for pattern, role in SPONSOR_PATTERNS:
        for m in pattern.finditer(full_text):
            raw = m.group(1)
            raw = re.split(r"\s*(?:Media Partner|Media Partners|Sponsors?|Partners?|Location|Panellists|Schedule|Contact)\s*:?", raw, 1, flags=re.I)[0]
            add_sponsor_candidate(candidates, raw, role, source_url, "auto-labelled-text", "medium")
    return candidates


def extract_euractiv_sponsors(soup: BeautifulSoup, source_url: str) -> list[Sponsor]:
    candidates: list[Sponsor] = []
    candidates.extend(extract_labelled_text_sponsors(soup, source_url))
    lines = clean_lines(soup)
    stop_re = re.compile(r"^(media partner|media partners|location|panellists|panelists|schedule|contact|subscribe|on the same topic|events)$", re.I)

    for i, line in enumerate(lines):
        # Euractiv often writes: "Organised by the Automotive Coalition for Europe:" followed by member organisations.
        m = re.match(r"^(?:Organised|Organized|Co-organised|Co-organized|Sponsored|Supported)\s+by\s+(?:the\s+)?(.+?)(?:\s*:)?$", line, re.I)
        if m:
            role = "Organised by" if "organ" in line.lower() else "Sponsored / supported by"
            lead = m.group(1).strip(" :")
            add_sponsor_candidate(candidates, lead, role, source_url, "auto-euractiv-organised-by", "high")
            for w in lines[i + 1 : min(i + 12, len(lines))]:
                if stop_re.search(w) or SPONSOR_HEADING_RE.search(w) and not re.match(r"^[·•-]", w):
                    break
                # Keep bullet-list organisations; remove bullet characters.
                cleaned = re.sub(r"^[·•\-*]\s*", "", w).strip()
                add_sponsor_candidate(candidates, cleaned, "Co-organiser / member", source_url, "auto-euractiv-organiser-list", "high")

        if re.match(r"^(sponsored by|sponsors|partners|partner|with the support of)$", line, re.I):
            for w in lines[i + 1 : min(i + 10, len(lines))]:
                if stop_re.search(w):
                    break
                add_sponsor_candidate(candidates, re.sub(r"^[·•\-*]\s*", "", w), "Sponsor / partner", source_url, "auto-euractiv-heading-lines", "medium")

    # Image/logo sections around sponsor/partner headings.
    heading_re = re.compile(r"\b(sponsored by|sponsors?|partners?|organised by|organized by|supported by)\b", re.I)
    for block in section_blocks_after_heading(soup, heading_re):
        candidates.extend(collect_logo_and_link_names(block, source_url, "Sponsor / partner", "medium"))
    return dedupe_sponsors(candidates)


def extract_parliament_sponsors(soup: BeautifulSoup, source_url: str) -> list[Sponsor]:
    candidates: list[Sponsor] = []
    # The Parliament places sponsor/partner logos in a dedicated "Sponsors" or "Partners" section.
    # Do not scan programme/speaker affiliations because those are not necessarily sponsors.
    heading_re = re.compile(r"^(sponsors?|partners?|supporters?|event partners?|commercial partners?)$", re.I)
    for block in section_blocks_after_heading(soup, heading_re, max_siblings=12):
        candidates.extend(collect_logo_and_link_names(block, source_url, "Sponsor / partner", "medium"))
    candidates.extend(extract_labelled_text_sponsors(soup, source_url))
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
    if "politico.eu" in host:
        return extract_politico_sponsors(soup, source_url)
    if "euronews.com" in host:
        return extract_partner_section_sponsors(soup, source_url)
    if any(x in host for x in ["logos-pa.com", "defencesecurityconference.eu", "spaceconference.eu"]):
        return extract_partner_section_sponsors(soup, source_url)
    return extract_partner_section_sponsors(soup, source_url)


def title_from_url_slug(url: str) -> str:
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    if not slug or slug in {"event", "events", "info"}:
        return ""
    title = re.sub(r"[-_]+", " ", slug).strip()
    return title[:1].upper() + title[1:] if title else ""


def candidate_title_from_lines(lines: list[str]) -> str:
    """Find a title line immediately before a date; useful for Euractiv pages where H1 is 'Events Calendar'."""
    for i, line in enumerate(lines):
        if first_date_text(line):
            for j in range(max(0, i - 4), i):
                cand = lines[j]
                if not is_bad_title(cand) and cand.lower() not in TITLE_NOISE:
                    return cand
    return ""


def extract_event_from_detail(scraper: Scraper, organization: str, url: str, default_category: str = "", default_title: str = "") -> Optional[Event]:
    soup = scraper.soup(url)
    if not soup:
        return None
    lines = clean_lines(soup)
    full_text = clean(" ".join(lines))

    title = ""
    title_candidates: list[str] = []

    # For Euractiv and some event-platform pages, the page H1 can be the site title
    # ("Events Calendar") and the actual event title is the H2 above the date.
    line_title = candidate_title_from_lines(lines)
    if line_title:
        title_candidates.append(line_title)

    heading_order = ["h2", "h3", "h1"] if organization.lower() == "euractiv" else ["h1", "h2", "h3"]
    for tag in soup.find_all(heading_order, limit=16):
        txt = clean(tag.get_text(" ", strip=True))
        if txt:
            title_candidates.append(txt)

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

    date_text = next_line_after(lines, ["Start Date", "When", "Date", "Date & Time"])
    if not first_date_text(date_text):
        # Prefer dates that appear close to the selected title.
        joined = "\n".join(lines)
        idx = joined.lower().find(title.lower())
        local = joined[idx : idx + 1000] if idx >= 0 else full_text[:2000]
        date_text = first_date_text(local) or first_date_text(full_text)
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



def is_actual_event(event: Event) -> bool:
    title_low = event.title.lower()
    # Exclude logos/news/insights/editorial pages that have dates but are not event pages.
    if any(x in title_low for x in ["insight", "insights", "news", "blog", "article", "press release", "vacancy"]):
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
    # logos does not publish one clean public event calendar. To avoid non-event "insights" pages,
    # this scraper only accepts verified conference/event microsites and event-like pages.
    seeds = [
        "https://defencesecurityconference.eu/",
        "https://defencesecurityconference.eu/about-us/",
        "https://spaceconference.eu/",
    ]
    events: list[Event] = []
    seen = set()
    for url in seeds:
        soup = scraper.soup(url)
        if not soup:
            continue
        partner_links: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"]).split("#")[0]
            if re.search(r"partners?|sponsors?", href, re.I) or re.search(r"partners?|sponsors?", clean(a.get_text(" ", strip=True)), re.I):
                partner_links.add(href)

        # JSON-LD event records are safest.
        for ev in extract_events_from_jsonld(soup, "logos", url):
            if not is_actual_event(ev):
                continue
            ev.sponsors = extract_sponsors_from_soup(soup, url)
            merge_partner_page_sponsors(scraper, ev, partner_links)
            if ev.event_id not in seen:
                events.append(ev)
                seen.add(ev.event_id)

        # Some conference sites do not expose JSON-LD but have an event-like title/date page.
        ev = extract_event_from_detail(scraper, "logos", url)
        if ev and is_actual_event(ev) and ev.event_id not in seen:
            merge_partner_page_sponsors(scraper, ev, partner_links)
            events.append(ev)
            seen.add(ev.event_id)

        # Crawl only internal links that themselves look like conference/event pages, not news or insights.
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"]).split("#")[0]
            if href in seen:
                continue
            parsed = urlparse(href)
            if parsed.netloc not in {"defencesecurityconference.eu", "spaceconference.eu", "www.spaceconference.eu"}:
                continue
            label = clean(a.get_text(" ", strip=True))
            if not re.search(r"conference|summit|forum|event|agenda|programme|partners?|sponsors?", label + " " + href, re.I):
                continue
            ev2 = extract_event_from_detail(scraper, "logos", href)
            if ev2 and is_actual_event(ev2):
                ev2.sponsors.extend(extract_sponsors_from_soup(soup, url))
                merge_partner_page_sponsors(scraper, ev2, partner_links | {href})
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
