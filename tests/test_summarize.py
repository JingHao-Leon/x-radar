"""规则摘要降级逻辑（离线、无网络）。"""

from x_radar.pipeline.summarize import extractive


def test_chinese_summary_short():
    text = "阿尔忒弥斯协定新增吉布提，已有72个国家签署。该协定规范月球与火星资源利用。NASA 局长表示这是和平探索的关键一步。"
    s = extractive(text)
    assert "吉布提" in s
    assert len(s) <= 160


def test_english_summary_and_numbers():
    text = "KV cache memory dropped by 96.7% using our method. Benchmark ran at 14.7x speedup. Second sentence here."
    s = extractive(text)
    assert s
    assert "96.7%" in s or "14.7x" in s


def test_empty():
    assert extractive("") == ""


def test_long_text_truncated():
    s = extractive("长" * 5000)
    assert len(s) <= 200
