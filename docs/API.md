# X Radar 内部 API 契约（v0.1）

服务端默认地址 `http://127.0.0.1:8787`。若设置了 `XR_TOKEN`，除 SSE/静态资源外的 API 都要求请求头 `X-Radar-Token: <token>`。

## 数据模型

TweetCard（仪表盘与插件共用的推文卡片）：

```json
{
  "id": "2099558640319373599",
  "handle": "NASA",
  "author_name": "NASA",
  "avatar": "https://pbs.twimg.com/profile_images/.../xxx_normal.jpg",
  "text": "推文全文（t.co 已展开为原文）",
  "created_at": "2026-09-15T01:59:22Z",
  "first_seen_at": "2026-09-16T00:00:00Z",
  "url": "https://x.com/NASA/status/2099558640319373599",
  "kind": "tweet | retweet | quote | reply",
  "rt_handle": "LearnWithNASA",
  "shot": "/media/tweets/2099558640319373599.png",
  "summary": "AI 中文摘要（≤120字）",
  "summary_model": "kimi-k2 | extractive",
  "status": "pending | processing | done | failed",
  "metrics": {"likes": 2051, "retweets": 226, "replies": 116},
  "pages": [
    {
      "url": "https://go.nasa.gov/4dvXZXN",
      "final_url": "https://www.nasa.gov/...",
      "title": "页面标题",
      "shot": "/media/pages/2099558640319373599_0.png",
      "summary": "网页内容摘要（≤160字）",
      "status": "done | pending | failed | skipped",
      "error": null
    }
  ]
}
```

注意：`shot` 为相对路径，前端/插件直接拼 `<server>/media/...` 引用；`status` 为推文管线的整体状态（pages 完成前推文可为 done 但 pages 有各自状态，SSE 会在 pages 完成时再推一次 `tweet_update`）。

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | `{"ok":true,"version":"0.1.0","llm":true,"accounts":2,"tweets":40,"poll_interval":60}` |
| GET | `/api/status` | `{"last_poll_at":null,"next_poll_at":null,"poll_count":0,"processed":0,"failed":0,"errors":[],"source":"syndication","headless":true}` |
| GET | `/api/accounts` | `{"accounts":[{"handle":"NASA","name":"NASA","avatar":"...","enabled":true,"added_at":"...","tweet_count":20,"last_tweet_at":"..."}]}` |
| POST | `/api/accounts` | body `{"handle":"nasa"}`（不带@，大小写不敏感）→ 201 `{"account":{...}}`；400 handle 非法；409 已存在 |
| DELETE | `/api/accounts/{handle}` | `{"ok":true}` |
| GET | `/api/feed?limit=50&handle=NASA&q=关键词&before_id=<tweet_id>` | `{"tweets":[TweetCard]}`，按 first_seen_at 倒序 |
| GET | `/api/tweets/{id}` | 单个 TweetCard |
| POST | `/api/tweets/{id}/refresh` | 重跑截图+摘要管线，`{"ok":true}` |
| POST | `/api/ingest` | 插件推送。body `{"tweets":[{"id":"...","handle":"NASA","text":"...","created_at":"ISO8601 可空","kind":"tweet|retweet|quote|reply 可空"}]}` → `{"ok":true,"accepted":1,"duplicates":0}`。服务端不校验 handle 是否在监控列表（插件采集即信任），但 handle 会规范化 |
| GET | `/api/stream` | SSE。事件名/数据：`tweet_new`/`tweet_update` → TweetCard；`account_update` → accounts 全量；`status` → status 对象；每 15s 一条 `ping` 注释 |
| GET | `/media/{path}` | 截图文件（`tweets/*.png`、`pages/*.png`） |

错误格式：`{"detail": "..."}`（FastAPI 默认）。

## 仪表盘前端（static/）

- 仅用原生 HTML/CSS/JS，禁止构建步骤与外部 CDN 依赖（图标用内联 SVG/emoji）。
- 首次加载并行拉 `/api/health`、`/api/status`、`/api/accounts`、`/api/feed?limit=50`，随后 `EventSource('/api/stream')` 增量更新。
- 布局：左侧栏（logo、运行状态卡：上次/下次轮询、LLM 徽章、账号列表+添加输入框+删除按钮）+ 主区（搜索框 + 账号筛选 chips + 推文卡片流）。
- 推文卡片：头像（加载失败回退首字母圆块）、作者名 @handle、kind 徽章（转推显示 rt_handle）、相对时间、原文外链按钮；推文文本（保留换行，t.co 已由服务端展开）；✨AI 摘要块（`summary_model=extractive` 时标注"规则摘要"）；推文截图（懒加载缩略，点击弹出 lightbox 大图）；引用网页列表（每条：favicon 占位、标题、状态、缩略图、摘要）；status=processing/pending 显示骨架屏/旋转指示；failed 显示错误与"重试"按钮（调 refresh）。
- SSE `tweet_new` 插入列表顶部并高亮；`tweet_update` 原位替换；搜索/筛选在前端做（已加载集合内过滤），变更时重新拉 feed。
- 深色主题，CSS 变量定义色板；响应式：≤900px 侧栏折叠为顶部条。

## 浏览器插件（extension/，Chrome MV3）

manifest.json：name "X Radar 监控助手"，permissions `storage,alarms,notifications`，host_permissions `http://*/*,https://*/*`，content_scripts 匹配 `https://x.com/*` 与 `https://twitter.com/*`（run_at document_idle），background service_worker，action popup。

- content/x.js（采集器）：
  - MutationObserver 观察 `article[data-testid="tweet"]`，2s 防抖批量解析：id（`a[href*="/status/"]` 中 `/status/<id>`）、handle（tweetText 所在 article 内第一个 status 链接的作者段）、文本（`[data-testid="tweetText"]` innerText）、时间（`time[datetime]`）、外链（tweetText 内 `a[href^="http"]` 且非 x.com/twitter.com）。
  - 仅当作者 handle ∈ 监控列表（启动时和每次 alarm 后从 `/api/accounts` 拉取，存 storage）才推送；`POST /api/ingest`（带 X-Radar-Token）批量 ≤20 条。
  - sessionStorage 去重 key `xr_sent_<id>`；`text` 长度上限 2000。
  - 个人主页（URL 匹配 `/^\/(\w{1,15})$/` 且页面有 `[data-testid="UserName"]`）注入悬浮按钮"🛰 加监控/✕ 取消监控"，调 POST/DELETE `/api/accounts`。
  - 所有服务端调用通过 `chrome.runtime.sendMessage({type:'xr_fetch',...})` 走 background（统一带 token、避免 CORS）。
- background.js：
  - 消息 `xr_fetch` 代理 fetch；`chrome.alarms` 每 0.5 分钟轮询 `/api/feed?limit=10`，与 storage 中 `lastSeenId`（按 first_seen_at 比较）比对出新推 → `chrome.notifications.create`（标题"X Radar 新推文"，message=`作者: 摘要前80字`）+ badge 未读数；点击通知打开 tweet url。
  - 每 30 分钟与告警后刷新监控列表缓存。
- popup.html/js：
  - 设置区：服务器地址（默认 http://127.0.0.1:8787）、Token、保存按钮（保存后立即测 /api/health 显示连通状态点）。
  - 状态区：health 数据（监控数/推文数/LLM 徽章）、未读列表（最近 10 条，点击打开原文并清 badge）。
  - "打开监控面板"链接。
- options 不单做，合并进 popup。

## 摘要服务（后端）

- LLM：OpenAI 兼容 `POST {XR_LLM_BASE_URL}/chat/completions`，模型 `XR_LLM_MODEL`。推文提示词：中文、≤120字、保留关键数字/结论/链接域名、转推保留原作者；网页提示词：中文、≤160字、提取要点与数据。超时 20s，失败自动降级 extractive。
- extractive 降级：取前 2 句（中文按。！？，英文按 .!?）+ 提取所有数字百分比，`summary_model="extractive"`。
