import io
import json
import urllib.error

import pytest

from ingest import youtube_trending as yt


def video_item(vid, title="t", duration="PT2M", views=100, cat="24"):
    return {
        "id": vid,
        "snippet": {"title": title, "channelTitle": "c", "categoryId": cat,
                    "publishedAt": "2026-11-23T00:00:00Z", "tags": ["a"],
                    "defaultAudioLanguage": "hi"},
        "statistics": {"viewCount": str(views), "likeCount": "5", "commentCount": "1"},
        "contentDetails": {"duration": duration},
    }


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_opener(routes):
    """routes: list of (substring, payload or HTTPError)."""
    calls = []

    def opener(req, timeout=30):
        url = req.full_url
        assert req.get_header("X-goog-api-key")
        calls.append(url)
        for key, payload in routes:
            if key in url:
                if isinstance(payload, Exception):
                    raise payload
                return FakeResp(json.dumps(payload).encode())
        raise AssertionError(f"unexpected url {url}")

    opener.calls = calls
    return opener


@pytest.mark.parametrize("iso,secs", [("PT1M5S", 65), ("PT2H", 7200), ("PT45S", 45), ("P1DT1S", 86401), ("", 0)])
def test_parse_duration(iso, secs):
    assert yt.parse_duration(iso) == secs


def test_parse_video_marks_shorts():
    short = yt.parse_video(video_item("a", duration="PT50S"), 1, "overall")
    long = yt.parse_video(video_item("b", duration="PT12M"), 2, "overall")
    assert short.is_short and not long.is_short
    assert short.views == 100 and short.language == "hi"


def test_fetch_all_and_quota():
    opener = fake_opener([
        ("videoCategories", {"items": [
            {"id": "10", "snippet": {"title": "Music", "assignable": True}},
            {"id": "99", "snippet": {"title": "Old", "assignable": False}},
        ]}),
        ("videoCategoryId=10", {"items": [video_item("m1", cat="10")]}),
        ("chart=mostPopular", {"items": [video_item("v1"), video_item("v2")]}),
    ])
    snap = yt.fetch_all("KEY", opener=opener)
    assert snap["categories"] == {"10": "Music"}
    assert [v["video_id"] for v in snap["videos"]] == ["v1", "v2", "m1"]
    assert snap["quota_units_used"] == 3
    assert all("regionCode=IN" in u for u in opener.calls)


def test_category_without_chart_is_skipped():
    err = urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b'{"error":{"message":"not found"}}'))
    opener = fake_opener([("videoCategoryId=5", err)])
    assert yt.fetch_chart("KEY", "5", opener=opener) == []


def test_bad_key_raises_readable_error():
    err = urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b'{"error":{"message":"API key not valid"}}'))
    with pytest.raises(yt.YouTubeAPIError, match="API key not valid"):
        yt.fetch_chart("BAD", opener=fake_opener([("videos", err)]))


def test_compare_finds_new_and_climbers():
    prev = {"videos": [
        {"video_id": "a", "rank": 5, "views": 10, "chart": "overall"},
        {"video_id": "b", "rank": 1, "views": 10, "chart": "overall"},
    ]}
    cur = {"videos": [
        {"video_id": "a", "rank": 2, "views": 50, "chart": "overall"},
        {"video_id": "b", "rank": 3, "views": 20, "chart": "overall"},
        {"video_id": "c", "rank": 1, "views": 99, "chart": "overall"},
    ]}
    diff = yt.compare(prev, cur)
    assert [v["video_id"] for v in diff["new_entrants"]] == ["c"]
    assert [(v["video_id"], v["rank_change"], v["view_gain"]) for v in diff["climbers"]] == [("a", 3, 40)]


def test_snapshot_roundtrip(tmp_path):
    snap = {"fetched_at": "2026-11-23T10:00:00+00:00", "region": "IN", "videos": []}
    yt.save_snapshot(snap, tmp_path)
    assert yt.latest_snapshot(tmp_path) == snap
