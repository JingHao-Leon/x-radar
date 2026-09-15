/**
 * X Radar 内容脚本：运行在 x.com / twitter.com
 * 职责：
 *  1. MutationObserver 监听推文卡片（2 秒防抖批量解析），监控名单内作者推送到 /api/ingest
 *  2. 个人主页右下角注入"🛰 加监控 / ✕ 取消监控"悬浮按钮（POST/DELETE /api/accounts）
 *  3. 所有网络请求经 chrome.runtime.sendMessage({type:'xr_fetch'}) 由 background 代理
 * 注意：x.com 为 SPA，监听 popstate + 拦截 pushState，并以 MutationObserver 兜底。
 */
(() => {
  'use strict';

  if (window.__xrContentLoaded) return;
  window.__xrContentLoaded = true;

  const FLUSH_DELAY = 2000;   // DOM 稳定 2 秒后再批量解析
  const MAX_TEXT = 2000;      // 推文文本长度上限
  const BATCH_MAX = 20;       // 单次 /api/ingest 批量上限

  // x.com 保留路径，不是用户 handle
  const RESERVED = new Set([
    'home', 'explore', 'search', 'notifications', 'messages', 'bookmarks', 'lists',
    'i', 'settings', 'compose', 'intent', 'hashtag', 'events', 'topics', 'analytics',
    'premium', 'jobs', 'communities', 'profile', 'status', 'shop', 'verified_orgs',
  ]);

  let monitored = new Set();  // 监控名单缓存（小写），来自 storage.monitoredHandles
  let scanTimer = null;
  let btnTimer = null;
  let button = null;
  let busy = false;
  const pending = new Map();  // 待推送 id -> card

  /* ---------- 经 background 代理的网络请求（统一带 token，避免 CORS） ---------- */

  function xrFetch(url, method = 'GET', body) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ type: 'xr_fetch', url, method, body }, (resp) => {
          if (chrome.runtime.lastError) {
            resolve({ ok: false, status: 0, error: chrome.runtime.lastError.message });
          } else {
            resolve(resp || { ok: false, status: 0, error: '无响应' });
          }
        });
      } catch (e) {
        // 扩展被重载等场景：上下文已失效，静默降级
        resolve({ ok: false, status: 0, error: String(e) });
      }
    });
  }

  /* ---------- 监控名单缓存 ---------- */

  function loadMonitored() {
    try {
      chrome.storage.local.get('monitoredHandles', (data) => {
        monitored = new Set(((data && data.monitoredHandles) || []).map((h) => String(h).toLowerCase()));
        refreshButtonLabel();
      });
    } catch (e) { /* ignore */ }
  }

  try {
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area === 'local' && changes.monitoredHandles) {
        monitored = new Set((changes.monitoredHandles.newValue || []).map((h) => String(h).toLowerCase()));
        refreshButtonLabel();
      }
    });
  } catch (e) { /* ignore */ }

  /* ---------- 推文解析（所有选择器带容错，解析失败返回 null 跳过） ---------- */

  function parseArticle(article) {
    try {
      // id + handle：article 内第一个 /status/ 链接（时间戳链接），作者段 + 推文 id
      const link = article.querySelector('a[href*="/status/"]');
      if (!link) return null;
      const href = link.getAttribute('href') || '';
      const m = href.match(/\/([A-Za-z0-9_]{1,15})\/status\/(\d+)/);
      if (!m) return null;

      // 文本
      const textEl = article.querySelector('[data-testid="tweetText"]');
      if (!textEl) return null; // 广告位 / 占位卡片等没有正文
      const text = (textEl.innerText || '').trim().slice(0, MAX_TEXT);
      if (!text) return null;

      // 发布时间
      const timeEl = article.querySelector('time[datetime]');
      const created_at = timeEl ? (timeEl.getAttribute('datetime') || null) : null;

      // kind 粗判：带 socialContext（"xx 转推了"）视为转推
      const kind = article.querySelector('[data-testid="socialContext"]') ? 'retweet' : 'tweet';

      // 外链：tweetText 内以 http 开头且非 x.com / twitter.com 的链接
      const links = [];
      textEl.querySelectorAll('a[href^="http"]').forEach((a) => {
        const h = a.href || '';
        if (h && !/^https?:\/\/([a-z0-9-]+\.)*(x|twitter)\.com(\/|$)/i.test(h)) links.push(h);
      });

      return {
        id: m[2],
        handle: m[1].toLowerCase(),
        text,
        created_at,
        kind,
        external_links: Array.from(new Set(links)).slice(0, 10),
      };
    } catch (e) {
      return null; // X 改版时不抛异常
    }
  }

  /* ---------- 采集与推送 ---------- */

  function scheduleScan() {
    clearTimeout(scanTimer);
    scanTimer = setTimeout(scanAndPush, FLUSH_DELAY);
  }

  function scanAndPush() {
    let nodes;
    try {
      nodes = document.querySelectorAll('article[data-testid="tweet"]');
    } catch (e) { return; }
    nodes.forEach((article) => {
      const card = parseArticle(article);
      if (!card) return;
      if (!monitored.has(card.handle)) return; // 仅监控名单内的作者才推送
      try {
        if (sessionStorage.getItem('xr_sent_' + card.id)) return; // 会话内去重
      } catch (e) { /* ignore */ }
      if (!pending.has(card.id)) pending.set(card.id, card);
    });
    if (pending.size) pushPending();
  }

  async function pushPending() {
    const batch = Array.from(pending.values()).slice(0, BATCH_MAX);
    if (!batch.length) return;
    const resp = await xrFetch('/api/ingest', 'POST', { tweets: batch });
    if (resp && resp.ok) {
      batch.forEach((t) => {
        try { sessionStorage.setItem('xr_sent_' + t.id, '1'); } catch (e) { /* ignore */ }
        pending.delete(t.id);
      });
    }
    // 失败则保留在 pending，下次扫描重试（服务端按 id 去重，幂等）
  }

  /* ---------- 个人主页悬浮按钮 ---------- */

  function profileHandle() {
    const m = location.pathname.match(/^\/(\w{1,15})\/?$/);
    if (!m) return null;
    if (RESERVED.has(m[1].toLowerCase())) return null;
    return m[1];
  }

  function removeButton() {
    if (button && button.parentNode) button.parentNode.removeChild(button);
    button = null;
  }

  function scheduleButtonCheck() {
    clearTimeout(btnTimer);
    btnTimer = setTimeout(updateButton, 400);
  }

  function updateButton() {
    const handle = profileHandle();
    if (!handle) { removeButton(); return; }
    // 页面已渲染出用户名才注入，避免路由切换途中误判
    let userName = null;
    try { userName = document.querySelector('[data-testid="UserName"]'); } catch (e) { /* ignore */ }
    if (!userName) { removeButton(); return; }
    if (button && button.dataset.xrHandle === handle) { refreshButtonLabel(); return; }
    removeButton();
    button = document.createElement('button');
    button.id = 'xr-monitor-btn';
    button.dataset.xrHandle = handle;
    // 深色胶囊样式，内联 CSS，不依赖页面样式表
    button.style.cssText = [
      'position:fixed',
      'right:20px',
      'bottom:20px',
      'z-index:2147483000',
      'padding:10px 18px',
      'border:1px solid #2f3336',
      'border-radius:999px',
      'background:#15202b',
      'color:#e7e9ea',
      'font:13px/1.2 -apple-system,"Segoe UI",Roboto,"PingFang SC",sans-serif',
      'cursor:pointer',
      'box-shadow:0 4px 14px rgba(0,0,0,.5)',
      'opacity:.92',
    ].join(';');
    button.addEventListener('click', () => toggleMonitor(handle));
    try { document.body.appendChild(button); } catch (e) { button = null; return; }
    refreshButtonLabel();
  }

  function refreshButtonLabel() {
    if (!button) return;
    const handle = String(button.dataset.xrHandle || '').toLowerCase();
    button.textContent = monitored.has(handle) ? '✕ 取消监控' : '🛰 加监控';
  }

  async function toggleMonitor(handle) {
    if (!button || busy) return;
    busy = true;
    const isMon = monitored.has(handle.toLowerCase());
    button.disabled = true;
    const resp = isMon
      ? await xrFetch('/api/accounts/' + encodeURIComponent(handle), 'DELETE')
      : await xrFetch('/api/accounts', 'POST', { handle });
    button.disabled = false;
    busy = false;
    if (resp && (resp.ok || resp.status === 409)) { // 409 已存在也视为已在监控
      // 本地缓存即时生效，并通知 background 与服务端重新对齐
      try {
        chrome.storage.local.get('monitoredHandles', (data) => {
          const next = new Set(((data && data.monitoredHandles) || []).map((h) => String(h).toLowerCase()));
          if (isMon) next.delete(handle.toLowerCase());
          else next.add(handle.toLowerCase());
          chrome.storage.local.set({ monitoredHandles: Array.from(next) });
        });
      } catch (e) { /* ignore */ }
      try {
        chrome.runtime.sendMessage({ type: 'xr_refresh_accounts' }, () => void chrome.runtime.lastError);
      } catch (e) { /* ignore */ }
      flashLabel(isMon ? '✓ 已取消' : '✓ 已加入监控');
    } else {
      flashLabel('✗ 操作失败');
    }
  }

  function flashLabel(text) {
    if (!button) return;
    const handle = button.dataset.xrHandle;
    button.textContent = text;
    setTimeout(() => {
      if (button && button.dataset.xrHandle === handle) refreshButtonLabel();
    }, 1500);
  }

  /* ---------- SPA 路由变化：popstate + pushState 拦截（observer 兜底） ---------- */

  function onRouteChange() {
    scheduleButtonCheck();
    scheduleScan();
  }

  window.addEventListener('popstate', onRouteChange);
  try {
    const rawPush = history.pushState;
    if (typeof rawPush === 'function') {
      history.pushState = function () {
        const ret = rawPush.apply(this, arguments);
        onRouteChange();
        return ret;
      };
    }
  } catch (e) { /* ignore */ }

  /* ---------- 启动 ---------- */

  const mo = new MutationObserver(() => {
    scheduleScan();
    scheduleButtonCheck();
  });
  try {
    mo.observe(document.documentElement || document, { childList: true, subtree: true });
  } catch (e) { /* ignore */ }

  loadMonitored();
  setTimeout(scheduleScan, 800);
})();
