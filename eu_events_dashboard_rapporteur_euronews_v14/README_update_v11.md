# EU Events Dashboard update v11 — strict event-date extraction

This update fixes repeated wrong dates such as multiple POLITICO events being assigned September 24.

Changes:
- POLITICO date extraction no longer infers a year from the URL/title for month/day snippets found elsewhere on the page.
- POLITICO pages no longer use broad top-of-page fallbacks, because those can pull related-card or agenda dates.
- Structured dates are accepted only when they match the current event page/title/URL.
- Known official safeguards remain for POLITICO Health Care Summit 2026 (1–2 December 2026) and Energy & Climate Forum (1 June 2026).
- Non-POLITICO pages keep the broader fallback so Euractiv/The Parliament/Euronews pages still work when they publish simpler HTML.

Replace in GitHub:
- index.html
- scraper.py
- requirements.txt

Then run Actions → Update event data → Run workflow.
