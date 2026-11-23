"""YouTube trending fetcher for India.

Pulls the "mostPopular" chart for region IN (overall and per video category),
saves a timestamped snapshot, and compares it with the previous snapshot to
find new entrants and fast climbers -- the trend signal the pipeline uses.

Quota: each videos.list / videoCategories.list call costs 1 unit. A full run
(overall + ~15 categories, up to 50 videos each) costs roughly 20 units, so the
default 10,000 units/day allows a run every few minutes; hourly is plenty.

Usage:
    export YOUTUBE_API_KEY=...        # or put it in .env
    python -m ingest.youtube_trending check
    python -m ingest.youtube_trending fetch
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://www.googleapis.com/youtube/v3"
REGION = "IN"
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "youtube"
SHORTS_MAX_SECONDS = 60  # API has no Shorts flag; <=60s is a reliable "likely Short" signal (3-min trailers are not)


class YouTubeAPIError(RuntimeError):
    pass


@dataclass
class Video:
    video_id: str
    title: str
    channel: str
    category_id: str
    published_at: str
    duration_seconds: int
    is_short: bool
    views: int
    likes: int
    comments: int
    tags: list[str] = field(default_factory=list)
    language: str | None = None
    rank: int = 0
    chart: str = "overall"  # "overall" or a category id

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


# ---------------------------------------------------------------- HTTP


def _get(endpoint: str, params: dict, api_key: str, opener=urllib.request.urlopen) -> dict:
    # Key goes in the X-Goog-Api-Key header (not the URL) so it never lands in logs.
    url = f"{API_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"X-Goog-Api-Key": api_key})
    try:
        with opener(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            reason = json.loads(body)["error"]["message"]
        except Exception:
            reason = body[:300]
        raise YouTubeAPIError(f"{endpoint} failed ({e.code}): {reason}") from None


# ---------------------------------------------------------------- parsing

_DURATION = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def parse_duration(iso: str) -> int:
    """ISO 8601 duration (e.g. PT1M5S) -> seconds."""
    m = _DURATION.fullmatch(iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def parse_video(item: dict, rank: int, chart: str) -> Video:
    sn = item.get("snippet", {})
    st = item.get("statistics", {})
    secs = parse_duration(item.get("contentDetails", {}).get("duration", ""))
    return Video(
        video_id=item["id"],
        title=sn.get("title", ""),
        channel=sn.get("channelTitle", ""),
        category_id=sn.get("categoryId", ""),
        published_at=sn.get("publishedAt", ""),
        duration_seconds=secs,
        is_short=0 < secs <= SHORTS_MAX_SECONDS,
        views=int(st.get("viewCount", 0)),
        likes=int(st.get("likeCount", 0)),
        comments=int(st.get("commentCount", 0)),
        tags=sn.get("tags", [])[:20],
        language=sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
        rank=rank,
        chart=chart,
    )


# ---------------------------------------------------------------- API calls


def list_categories(api_key: str, opener=urllib.request.urlopen) -> dict[str, str]:
    """Assignable video categories for India: {id: title}."""
    data = _get("videoCategories", {"part": "snippet", "regionCode": REGION, "hl": "en_IN"}, api_key, opener)
    return {
        c["id"]: c["snippet"]["title"]
        for c in data.get("items", [])
        if c.get("snippet", {}).get("assignable")
    }


def fetch_chart(api_key: str, category_id: str | None = None, max_results: int = 50,
                opener=urllib.request.urlopen) -> list[Video]:
    params = {
        "part": "snippet,statistics,contentDetails",
        "chart": "mostPopular",
        "regionCode": REGION,
        "maxResults": min(max_results, 50),
    }
    if category_id:
        params["videoCategoryId"] = category_id
    try:
        data = _get("videos", params, api_key, opener)
    except YouTubeAPIError as e:
        # Some categories have no chart for a region; skip them quietly.
        if category_id and "404" in str(e):
            return []
        raise
    chart = category_id or "overall"
    return [parse_video(it, i + 1, chart) for i, it in enumerate(data.get("items", []))]


def fetch_all(api_key: str, opener=urllib.request.urlopen) -> dict:
    categories = list_categories(api_key, opener)
    videos = fetch_chart(api_key, opener=opener)
    calls = 2
    for cid in categories:
        videos.extend(fetch_chart(api_key, cid, opener=opener))
        calls += 1
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "region": REGION,
        "categories": categories,
        "quota_units_used": calls,
        "videos": [asdict(v) for v in videos],
    }


# ---------------------------------------------------------------- trend signal


def compare(previous: dict | None, current: dict) -> dict:
    """New entrants and climbers on the overall chart vs the previous snapshot."""
    def overall(snap):
        return {v["video_id"]: v for v in snap["videos"] if v["chart"] == "overall"}

    now = overall(current)
    if not previous:
        return {"new_entrants": list(now.values()), "climbers": []}
    before = overall(previous)
    new = [v for vid, v in now.items() if vid not in before]
    climbers = sorted(
        (
            {**v, "rank_change": before[vid]["rank"] - v["rank"],
             "view_gain": v["views"] - before[vid]["views"]}
            for vid, v in now.items() if vid in before
        ),
        key=lambda v: v["rank_change"], reverse=True,
    )
    return {"new_entrants": new, "climbers": [c for c in climbers if c["rank_change"] > 0]}


# ---------------------------------------------------------------- storage


def save_snapshot(snapshot: dict, data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    stamp = snapshot["fetched_at"].replace(":", "").replace("-", "")
    path = data_dir / f"trending_{snapshot['region']}_{stamp}.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def latest_snapshot(data_dir: Path) -> dict | None:
    files = sorted(data_dir.glob("trending_*.json"))
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


# ---------------------------------------------------------------- CLI


def _load_key() -> str:
    key = os.environ.get("YOUTUBE_API_KEY")
    env = Path(__file__).resolve().parent.parent / ".env"
    if not key and env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("YOUTUBE_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"')
    if not key:
        # In the cloud environment the key is stored as a network secret and the
        # proxy fills in the X-Goog-Api-Key header, so a placeholder is enough.
        print("YOUTUBE_API_KEY not set; relying on the environment's network secret.", file=sys.stderr)
        key = "injected-by-proxy"
    return key


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="YouTube trending fetcher (India)")
    p.add_argument("command", choices=["check", "fetch"])
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = p.parse_args(argv)
    key = _load_key()

    if args.command == "check":
        videos = fetch_chart(key, max_results=10)
        print(f"API key works. Top {len(videos)} trending in India now:")
        for v in videos:
            kind = "Short" if v.is_short else "Video"
            print(f"{v.rank:>2}. [{kind}] {v.title} - {v.channel} ({v.views:,} views)")
        return 0

    previous = latest_snapshot(args.data_dir)
    snap = fetch_all(key)
    path = save_snapshot(snap, args.data_dir)
    diff = compare(previous, snap)
    print(f"Saved {len(snap['videos'])} videos from {len(snap['categories']) + 1} charts "
          f"to {path} (quota used: {snap['quota_units_used']} units)")
    print(f"New on the overall chart: {len(diff['new_entrants'])}; climbers: {len(diff['climbers'])}")
    for v in diff["new_entrants"][:5]:
        print(f"  NEW #{v['rank']}: {v['title']}")
    for v in diff["climbers"][:5]:
        print(f"  UP {v['rank_change']}: #{v['rank']} {v['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
