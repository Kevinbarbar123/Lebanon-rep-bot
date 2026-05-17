# Lebanon-rep-bot

Telegram bot for finding likely owner-direct apartment rentals in Metn, Lebanon.

## What it does

- Checks OLX/Dubizzle, OpenSooq, and Mourjan every 12 hours.
- Filters likely owner listings vs. agencies using English and Arabic terms.
- Sends Telegram alerts for likely owner listings.
- Saves listing history in SQLite.
- Generates an offline static dashboard in `offline_site/`.
- Creates one folder per detected area, for example `offline_site/areas/fanar/index.html`.

## Dashboard

When running on Railway as a web process, the same `offline_site/` dashboard is served from the service URL.

For local/offline use, run:

```bash
python bot.py
```

Then open:

```text
offline_site/index.html
```

The dashboard includes listing history, client/contact history, area folders, and average USD price per sqm when both price and surface area are available.
