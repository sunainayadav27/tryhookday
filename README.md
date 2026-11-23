# Pulse Calendar (working name)

AI content calendar for Indian brands. This repo starts with the signal crawlers.

## YouTube trending fetcher

`ingest/youtube_trending.py` pulls India's most-popular chart (overall + every
video category), marks Shorts, saves a snapshot to `data/youtube/`, and reports
new entrants and climbers versus the previous snapshot. A full run costs about
20 of the 10,000 free daily quota units.

### Key

Either:
- **Cloud environment (recommended):** add a network secret for host
  `www.googleapis.com`, header `X-Goog-Api-Key`, value = your key. The proxy
  adds it to every request; nothing is stored in code or `.env`.
- **Local machine:** copy `.env.example` to `.env` and set `YOUTUBE_API_KEY`.

### Run

```bash
python3 -m ingest.youtube_trending check   # prints top 10 trending in India
python3 -m ingest.youtube_trending fetch   # full snapshot + trend diff
python3 -m pytest -q                       # tests (no network needed)
```
