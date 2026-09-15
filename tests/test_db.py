"""存储层：去重、feed 排序、pages 卡片组装。"""

import time

from x_radar.db import Store


def make_store(tmp_path):
    return Store(tmp_path / "t.db")


def sample_tweet(tid, handle="NASA", **kw):
    t = {"id": tid, "handle": handle, "author_name": "NASA", "avatar": "",
         "text": f"tweet {tid}", "created_at": "2026-09-14T00:00:00+00:00",
         "url": f"https://x.com/{handle}/status/{tid}", "kind": "tweet",
         "metrics": {"likes": 1}, "external_urls": [], "source": "poll"}
    t.update(kw)
    return t


def test_insert_dedup(tmp_path):
    s = make_store(tmp_path)
    assert s.insert_tweet(sample_tweet("1")) is True
    assert s.insert_tweet(sample_tweet("1")) is False
    assert s.counts()["tweets"] == 1


def test_feed_order_and_filters(tmp_path):
    s = make_store(tmp_path)
    for tid in ("a", "b", "c"):
        s.insert_tweet(sample_tweet(tid))
        time.sleep(1.1)  # first_seen_at 秒级精度，保证顺序
    s.insert_tweet(sample_tweet("d", handle="Other"))
    feed = s.list_feed(limit=10)
    assert [t["id"] for t in feed] == ["d", "c", "b", "a"]
    assert [t["id"] for t in s.list_feed(handle="NASA")] == ["c", "b", "a"]
    assert [t["id"] for t in s.list_feed(q="tweet c")] == ["c"]


def test_card_with_pages(tmp_path):
    s = make_store(tmp_path)
    s.insert_tweet(sample_tweet("x", external_urls=["https://a.com"]))
    s.update_tweet("x", status="done", summary="摘要", shot="/media/tweets/x.png")
    s.upsert_page("x", 0, url="https://a.com", title="A 页", summary="页摘要",
                  shot="/media/pages/x_0.png", status="done")
    card = s.card_for("x")
    assert card["status"] == "done" and card["summary"] == "摘要"
    assert card["pages"][0]["title"] == "A 页"
    assert card["external_urls"] == ["https://a.com"]


def test_accounts(tmp_path):
    s = make_store(tmp_path)
    s.upsert_account("NASA", "NASA", "http://a/1.jpg")
    assert s.get_account("nasa")["handle"] == "NASA"  # NOCASE 主键
    s.delete_account("nasa")
    assert s.get_account("NASA") is None
