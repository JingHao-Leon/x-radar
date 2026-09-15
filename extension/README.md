# X Radar 监控助手（Chrome 扩展）

X Radar 的 Chrome MV3 浏览器插件。在浏览 x.com 时自动采集监控名单内作者的新推文并推送到 X Radar 服务端，同时后台轮询 feed，对新增推文弹出桌面通知并在图标上显示未读角标。纯原生 JavaScript，无构建步骤、无 npm 依赖、无外部 CDN。

## 功能

- **推文采集**：MutationObserver 监听 x.com 时间线中的推文卡片（2 秒防抖批量解析），提取推文 id / 作者 handle / 正文 / 发布时间 / 外链，仅当作者在监控名单内时批量（≤20 条/次）`POST /api/ingest` 推送，sessionStorage 会话内去重。
- **快捷加监控**：打开任意用户个人主页，右下角出现"🛰 加监控 / ✕ 取消监控"悬浮按钮，一键调用服务端接口添加 / 移除监控。
- **新推文提醒**：后台每 0.5 分钟轮询 `/api/feed?limit=10`，发现新推文弹出系统通知，扩展图标显示未读角标；点击通知直达推文原文。
- **弹窗面板**：配置服务器地址与 Token、查看服务端健康状态（监控数 / 推文数 / LLM 徽章）、浏览最近 10 条未读、一键打开监控面板仪表盘。

## 安装（开发者模式加载）

1. 打开 Chrome，地址栏输入 `chrome://extensions` 回车；
2. 打开右上角 **开发者模式** 开关；
3. 点击左上角 **加载已解压的扩展程序**，选择本目录（`extension/`）；
4. 安装后建议刷新已打开的 x.com 标签页，使内容脚本生效。

> 基于 Chromium 的浏览器（Edge / Brave 等）同样适用，入口一般为 `edge://extensions` 等。

## 配置步骤

1. 先启动 X Radar 服务端（默认监听 `http://127.0.0.1:8787`；若设置了 `XR_TOKEN` 环境变量，则所有 API 需要 Token）；
2. 点击浏览器工具栏中的 X Radar 图标打开弹窗；
3. 填写 **服务器地址**（默认 `http://127.0.0.1:8787`）与 **Token**（服务端未设置 `XR_TOKEN` 时留空），点击 **保存**；
4. 保存后立即检测 `/api/health`：绿点表示连通，并展示监控数 / 推文数 / LLM 状态；
5. 回到 x.com 刷新页面，即可开始采集；监控名单可由仪表盘或个人主页悬浮按钮维护。

## 权限用途声明

| 权限 | 用途 |
|---|---|
| `storage` | 本地保存服务器地址、Token、监控名单缓存、未读列表与轮询水位 |
| `alarms` | 每 0.5 分钟轮询新推文、每 30 分钟刷新监控账号列表缓存 |
| `notifications` | 检测到新推文时弹出系统桌面通知 |
| `host_permissions: http://*/*, https://*/*` | 允许扩展后台向自建的 X Radar 服务器（含局域网地址）发起请求 |
| 内容脚本匹配 `https://x.com/*`、`https://twitter.com/*` | 仅在 X 页面上解析推文卡片并注入悬浮按钮 |

本插件不收集、不上传任何数据到除你自己部署的 X Radar 服务器之外的任何第三方；所有网络请求都发往你在弹窗中配置的服务器地址。

## 目录结构

```
extension/
├── manifest.json     # MV3 清单
├── background.js     # service worker：请求代理 / 轮询 / 通知 / 角标 / 监控名单缓存
├── content/x.js      # x.com 页面采集器 + 个人主页加监控按钮
├── popup.html/js/css # 弹窗：设置、健康状态、未读列表、面板入口
└── README.md
```

## 说明

- 所有服务端调用统一经 background 代理（自动附加 `X-Radar-Token`，规避 CORS）；
- x.com 是 SPA，插件通过 `popstate` / `pushState` 拦截 + MutationObserver 兜底处理路由切换；
- DOM 选择器均带容错，X 前端改版时静默跳过、不抛异常；
- API 契约见仓库 `docs/API.md`。
