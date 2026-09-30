# Update v13 — Euronews calendar discovery fix

This update restores Euronews events and improves automatic discovery from:

- https://events.euronews.com/events
- https://events.euronews.com/
- Euronews event microsites such as https://events.euronews.com/health_summit_2026
- Euronews sitemap URLs, when available

The scraper now checks normal links, JavaScript/JSON-embedded links, and sitemap URLs so new official Euronews event microsites should be added automatically when they are published on the official Euronews events page.

It also keeps a safety-net entry for the current official Euronews Health Summit 2026 page so it appears in the 2026 dataset even if the JavaScript calendar temporarily hides the link.

Replace in GitHub:

- index.html
- scraper.py
- requirements.txt

Workflow replacement is optional if your workflow is already green and uses START_DATE=2026-01-01 and END_DATE=2026-12-31.
