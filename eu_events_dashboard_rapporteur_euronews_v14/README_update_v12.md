# Update v12 — POLITICO 24 September date cleanup

This update removes the broad POLITICO listing-card date fallback that caused multiple events to inherit the same unrelated date, especially 24 September 2026 / 8:15 am.

Replace in GitHub:
- index.html
- scraper.py
- requirements.txt

Then run Actions > Update event data > Run workflow.

You do not need to edit update-events.yml if it is already working. The Node.js warning is a warning only.
