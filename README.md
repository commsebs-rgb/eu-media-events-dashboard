# EU Media Events Dashboard

A lightweight dashboard to map events from:

- POLITICO
- Euractiv
- The Parliament
- Euronews
- logos

It has two main tabs:

1. **Calendar** — events grouped week by week.
2. **Sponsors by event** — sponsor/partner names assigned to each event.

The included GitHub Actions workflow refreshes the data every 24 hours and republishes the JSON file used by the dashboard.

---

## Folder structure

```text
.
├── index.html
├── scraper.py
├── requirements.txt
├── data/
│   ├── events.json
│   └── manual_sponsors.csv
└── .github/
    └── workflows/
        └── update-events.yml
```

---

## How to run locally

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python scraper.py
python -m http.server 8000
```

Open:

```text
http://localhost:8000
```

---

## How the 24-hour update works

The workflow in `.github/workflows/update-events.yml` runs once per day. It:

1. Installs Python dependencies.
2. Runs `python scraper.py`.
3. Updates `data/events.json`.
4. Commits the updated JSON back to the repository.
5. GitHub Pages serves the updated dashboard.

You can also launch it manually from GitHub: **Actions → Update event data → Run workflow**.

---

## How to make it always available with GitHub Pages

1. Create a new GitHub repository, for example `eu-media-events-dashboard`.
2. Upload all files in this folder.
3. Go to **Settings → Pages**.
4. Under **Build and deployment**, choose:
   - Source: **Deploy from a branch**
   - Branch: **main**
   - Folder: **/root**
5. Save.
6. Your dashboard will be available at:

```text
https://YOUR-USERNAME.github.io/eu-media-events-dashboard/
```

This is the cheapest and simplest option because GitHub hosts the static dashboard and GitHub Actions refreshes the data.

---

## Sponsor mapping

Automatic sponsor extraction is difficult because many event sites show partner logos as images, use JavaScript tabs, or hide details behind event platforms.

To guarantee sponsor assignments, edit:

```text
data/manual_sponsors.csv
```

Example:

```csv
organization,title_contains,event_date,sponsor,role,source_url
Euractiv,Tech Policy Conference,2026-06-23,Example Sponsor,Main sponsor,https://example.com
The Parliament,European Industry Forum,2026-06-03,Example Partner,Partner,https://example.com
```

Rules:

- `organization` can be left blank if the title is unique.
- `title_contains` matches part of the event title.
- `event_date` is optional but useful when the same event repeats every year.
- `sponsor` is the sponsor/partner name to show in the dashboard.
- `role` can be "Main sponsor", "Partner", "Media partner", etc.

---

## Important notes

- The scraper reads public pages only.
- Some websites may change layout, which can require adjusting `scraper.py`.
- POLITICO and Euronews often publish individual event pages rather than a single complete public event calendar, so add new public event URLs in `scraper.py` when needed.
- Check each website’s terms of use before relying on automated scraping for commercial use.
