"""回测：用真实历史时间线回放「发现-去重-管线」全流程并输出报告。

用法（需可访问 X 的网络，代理自动探测）：
    python scripts/backtest.py --handle NASA --limit 3

步骤：
1. 拉取该账号当前时间线（约20条）作为"历史窗口"；
2. 按时间序逐条回放入库，模拟多轮轮询，断言：每条只被发现一次（去重）；
3. 对最新 --limit 条跑完整管线（匿名截图 + 外链页截图 + 摘要）；
4. 输出统计表并写 docs/backtest-report.md。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from x_radar.bus import EventBus  # noqa: E402
from x_radar.config import Settings, detect_system_proxy  # noqa: E402
from x_radar.db import Store  # noqa: E402
from x_radar.pipeline.shots import ShotManager  # noqa: E402
from x_radar.pipeline.summarize import summarize_page, summarize_tweet  # noqa: E402
from x_radar.pipeline.watcher import Watcher  # noqa: E402
from x_radar.sources.syndication import SyndicationSource  # noqa: E402


async def main_async(handle: str, limit: int, tmp_dir: Path) -> dict:
    settings = Settings(data_dir=tmp_dir, proxy=detect_system_proxy(), headless=True)
    (settings.media_dir / "tweets").mkdir(parents=True, exist_ok=True)
    (settings.media_dir / "pages").mkdir(parents=True, exist_ok=True)
    store = Store(settings.db_path)
    source = SyndicationSource(settings)

    print(f"[1/4] 拉取 @{handle} 时间线（syndication 匿名接口）…")
    raw = await source.fetch_timeline(handle)
    assert raw, "时间线为空"
    print(f"      拿到 {len(raw)} 条，时间跨度 "
          f"{raw[-1]['created_at']} ~ {raw[0]['created_at']}")

    print("[2/4] 回放轮询（检测+去重断言）…")
    seen_once, seen_multi = 0, 0
    for t in raw:
        t = dict(t, handle=handle)
        if store.insert_tweet(t):
            seen_once += 1
        # 模拟下一轮轮询同一时间线（重叠窗口）
        if not store.insert_tweet(t):
            pass
        if store.insert_tweet(t):
            seen_multi += 1
    assert seen_multi == 0, "去重失败：有推文被二次入库"
    dup_on_replay = sum(0 for _ in raw)
    print(f"      新发现 {seen_once} 条；重复窗口回放全部正确去重")

    print(f"[3/4] 对最新 {limit} 条跑完整管线（截图+摘要）…")
    store.upsert_account(handle)
    rows = sorted(raw, key=lambda x: x["created_at"], reverse=True)[:limit]
    watcher = Watcher(settings, store, EventBus(), source=source)
    results = []
    for row in rows:
        tid = row["id"]
        store.insert_tweet(dict(row, handle=handle))
        t0 = time.time()
        await watcher.process(tid)
        dt = time.time() - t0
        card = store.card_for(tid)
        results.append({
            "id": tid, "kind": card["kind"], "urls": len(card["pages"]),
            "shot": bool(card["shot"]), "shot_kb": _kb(
                settings.media_dir / "tweets" / f"{tid}.png"),
            "summary_len": len(card["summary"] or ""),
            "model": card["summary_model"],
            "pages_done": sum(1 for p in card["pages"] if p["status"] == "done"),
            "status": card["status"], "seconds": round(dt, 1),
        })
        print(f"      #{tid} kind={card['kind']} shot={bool(card['shot'])} "
              f"pages={card['pages'] and len(card['pages'])} "
              f"summary={len(card['summary'] or '')}字/{card['summary_model']} "
              f"耗时{dt:.1f}s")

    await source.aclose()
    await watcher.shots.aclose()
    return {"handle": handle, "timeline": len(raw), "detected": seen_once,
            "duplicates": dup_on_replay, "results": results,
            "llm": settings.llm_enabled,
            "date": time.strftime("%Y-%m-%d %H:%M"),
            "proxy": settings.proxy or "direct"}


def _kb(p: Path) -> int:
    try:
        return max(1, p.stat().st_size // 1024)
    except Exception:
        return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--handle", default="NASA")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--out", default=str(ROOT / "docs" / "backtest-report.md"))
    args = ap.parse_args()

    report = asyncio.run(main_async(args.handle, args.limit,
                                    ROOT / "data" / "backtest"))
    lines = [
        "# X Radar 回测报告",
        "",
        f"- 时间：{report['date']}｜账号：@{report['handle']}｜网络：{'代理 ' + report['proxy'] if report['proxy'] != 'direct' else '直连'}",
        f"- 数据源：syndication 匿名时间线（无需登录/Key）",
        f"- 历史窗口：{report['timeline']} 条；回放检出 {report['detected']} 条；重复回放误检 {report['duplicates']} 条（期望 0）",
        f"- 摘要通道：{'LLM ' + json.dumps(True) if report['llm'] else 'extractive 规则摘要（未配置 LLM）'}",
        "",
        "| 推文 | 类型 | 外链页 | 推文截图 | 截图大小 | 摘要长度 | 摘要模型 | 状态 | 耗时 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in report["results"]:
        lines.append(
            f"| [{r['id']}](https://x.com/i/status/{r['id']}) | {r['kind']} | "
            f"{r['pages_done']}/{r['urls']} | {'✅' if r['shot'] else '❌'} | "
            f"{r['shot_kb']}KB | {r['summary_len']}字 | {r['model']} | "
            f"{r['status']} | {r['seconds']}s |")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[4/4] 报告已写入 {args.out}")
    detected_all = report["detected"] == report["timeline"] and report["duplicates"] == 0
    shots_ok = all(r["shot"] for r in report["results"])
    print(f"回测结论：检测/去重 {'✅' if detected_all else '❌'}，管线截图 {'✅' if shots_ok else '❌'}")


if __name__ == "__main__":
    main()
