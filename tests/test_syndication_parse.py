"""syndication __NEXT_DATA__ 解析（离线夹具）。"""

from x_radar.sources.syndication import (
    classify,
    expand_text,
    external_urls_of,
    parse_next_data,
    parse_twitter_time,
    valid_handle,
)

import json
import re


def _next_data_json() -> str:
    raw = {"props": {"pageProps": load_page_props()}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(raw)}</script></html>'


def load_page_props() -> dict:
    from conftest import load_fixture

    return load_fixture("syndication_nasa.json")


def test_parse_next_data_entries():
    tweets = parse_next_data(_next_data_json())
    assert len(tweets) == 3
    t0 = tweets[0]
    assert t0["id"].isdigit() and len(t0["id"]) >= 15
    assert t0["handle"] == "NASA"
    assert t0["url"].startswith("https://x.com/NASA/status/")
    assert t0["created_at"].endswith("+00:00")
    assert set(t0["metrics"]) == {"likes", "retweets", "replies"}


def test_retweet_classification():
    tweets = parse_next_data(_next_data_json())
    rt = [t for t in tweets if t["kind"] == "retweet"]
    assert rt, "夹具中应有转推"
    assert rt[0]["rt_handle"]
    assert not rt[0]["text"].startswith("RT @")


def test_expand_text_replaces_tco():
    text = expand_text("look https://t.co/abc end",
                       {"urls": [{"url": "https://t.co/abc",
                                  "expanded_url": "https://example.com/x"}]})
    assert text == "look https://example.com/x end"


def test_external_urls_filters_self_hosts():
    urls = external_urls_of({
        "entities": {"urls": [
            {"expanded_url": "https://go.nasa.gov/abc"},
            {"expanded_url": "https://x.com/NASA/status/1"},
            {"expanded_url": "https://pic.twitter.com/img"},
        ]}})
    assert urls == ["https://go.nasa.gov/abc"]


def test_classify_quote():
    kind, _ = classify({"full_text": "hello", "quoted_tweet": {"id_str": "1"}})
    assert kind == "quote"


def test_twitter_time():
    iso = parse_twitter_time("Mon Sep 14 17:59:22 +0000 2026")
    assert iso == "2026-09-14T17:59:22+00:00"


def test_valid_handle():
    assert valid_handle("NASA")
    assert valid_handle("elonmusk")
    assert not valid_handle("has space")
    assert not valid_handle("a" * 16)
    assert not valid_handle("")


def test_no_execute_pattern_in_source():
    import pathlib

    src = pathlib.Path(__file__).parent.parent / "src" / "x_radar"
    for py in src.rglob("*.py"):
        assert not re.search(r"\.execute\(", py.read_text(encoding="utf-8")), py
