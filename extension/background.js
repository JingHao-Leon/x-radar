/**
 * X Radar background service worker（MV3）
 * 职责：
 *  1. 代理所有对 X Radar 服务端的 HTTP 请求（xr_fetch 消息），统一带上 X-Radar-Token
 *  2. chrome.alarms 每 0.5 分钟轮询 /api/feed，对新推文弹系统通知 + 设置角标未读数
 *  3. 定期（每 30 分钟 + 每次轮询后）刷新监控账号列表缓存到 storage.monitoredHandles
 * 注意：service_worker 环境无 DOM/sessionStorage，一律使用 chrome.storage.local
 */

const POLL_ALARM = 'xr-poll';          // 每 0.5 分钟
const ACCOUNTS_ALARM = 'xr-accounts';  // 每 30 分钟
const DEFAULT_SERVER = 'http://127.0.0.1:8787';
const MAX_UNREAD = 10;                 // 未读列表容量
const MAX_NOTIFY_PER_POLL = 3;         // 单轮最多弹的通知数，避免刷屏

/* ---------------- storage 小工具 ---------------- */

function storageGet(keys) {
  return new Promise((resolve) => chrome.storage.local.get(keys, (d) => resolve(d || {})));
}
function storageSet(obj) {
  return new Promise((resolve) => chrome.storage.local.set(obj, () => resolve()));
}

/* ---------------- 服务端请求 ---------------- */

/** 读取 server/token 后请求服务端，返回 {ok, status, data|error} */
async function apiFetch(url, method = 'GET', body) {
  try {
    const { server = '', token = '' } = await storageGet(['server', 'token']);
    let target = url || '';
    if (!/^https?:\/\//i.test(target)) {
      const base = (server || DEFAULT_SERVER).replace(/\/+$/, '');
      target = base + (target.startsWith('/') ? target : '/' + target);
    }
    const headers = {};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (token) headers['X-Radar-Token'] = token;
    const resp = await fetch(target, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const text = await resp.text();
    let data;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
    return { ok: resp.ok, status: resp.status, data };
  } catch (e) {
    return { ok: false, status: 0, error: String((e && e.message) || e) };
  }
}

/** 处理来自 content script / popup 的 xr_fetch 消息 */
async function handleXrFetch(msg) {
  return apiFetch(msg.url, msg.method || 'GET', msg.body);
}

/* ---------------- 消息总线 ---------------- */

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'xr_fetch') {
    handleXrFetch(msg).then(sendResponse);
    return true; // 异步应答
  }
  if (msg.type === 'xr_refresh_accounts') {
    refreshAccounts()
      .then(() => sendResponse({ ok: true }))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (msg.type === 'xr_mark_read') {
    markRead(msg.id);
    sendResponse({ ok: true });
  }
});

/* ---------------- 定时任务 ---------------- */

function ensureAlarms() {
  chrome.alarms.get(POLL_ALARM, (a) => {
    if (!a) chrome.alarms.create(POLL_ALARM, { periodInMinutes: 0.5 });
  });
  chrome.alarms.get(ACCOUNTS_ALARM, (a) => {
    if (!a) chrome.alarms.create(ACCOUNTS_ALARM, { periodInMinutes: 30 });
  });
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === POLL_ALARM) {
    pollFeed();
  } else if (alarm.name === ACCOUNTS_ALARM) {
    refreshAccounts();
  }
});

chrome.runtime.onInstalled.addListener(ensureAlarms);
chrome.runtime.onStartup.addListener(() => {
  ensureAlarms();
  refreshAccounts();
});
ensureAlarms();

/* ---------------- 监控账号列表缓存 ---------------- */

async function refreshAccounts() {
  const r = await apiFetch('/api/accounts');
  if (!r.ok || !r.data || !Array.isArray(r.data.accounts)) return;
  const handles = r.data.accounts
    .filter((a) => a && a.enabled !== false && a.handle)
    .map((a) => String(a.handle).toLowerCase());
  await storageSet({ monitoredHandles: handles, monitoredUpdatedAt: Date.now() });
}

/* ---------------- 轮询 feed：新推文通知 + 角标 ---------------- */

function tsOf(t) {
  const d = new Date((t && t.first_seen_at) || 0);
  return isNaN(d.getTime()) ? 0 : d.getTime();
}

async function pollFeed() {
  const r = await apiFetch('/api/feed?limit=10');
  if (!r.ok || !r.data || !Array.isArray(r.data.tweets)) return;

  const tweets = r.data.tweets.slice().sort((a, b) => tsOf(a) - tsOf(b));
  const { lastSeenAt = null, unreadItems = [] } = await storageGet(['lastSeenAt', 'unreadItems']);

  // 推进水位线
  if (tweets.length) {
    const newest = tweets[tweets.length - 1];
    if (!lastSeenAt || tsOf(newest) > new Date(lastSeenAt).getTime()) {
      await storageSet({ lastSeenAt: newest.first_seen_at });
    }
  }
  // 首次运行只建立水位，不弹历史通知
  if (!lastSeenAt) return;

  const cutoff = new Date(lastSeenAt).getTime();
  const fresh = tweets.filter((t) => t && t.id && tsOf(t) > cutoff);
  if (!fresh.length) return;

  const items = Array.isArray(unreadItems) ? unreadItems.slice() : [];
  const known = new Set(items.map((i) => i.id));
  let notified = 0;
  for (const t of fresh) {
    if (known.has(t.id)) continue;
    items.unshift({
      id: t.id,
      url: t.url || '',
      handle: t.handle || '',
      text: (t.summary || t.text || '').replace(/\s+/g, ' ').slice(0, 80),
      first_seen_at: t.first_seen_at || '',
    });
    if (notified < MAX_NOTIFY_PER_POLL) {
      notified += 1;
      notifyTweet(t);
    }
  }

  items.sort((a, b) => new Date(b.first_seen_at || 0) - new Date(a.first_seen_at || 0));
  const trimmed = items.slice(0, MAX_UNREAD);
  await storageSet({ unreadItems: trimmed });
  updateBadge(trimmed.length);
  refreshAccounts(); // 每次告警后同步监控名单缓存
}

function notifyTweet(t) {
  const author = t.author_name || (t.handle ? '@' + t.handle : 'X Radar');
  const snippet = (t.summary || t.text || '').replace(/\s+/g, ' ').slice(0, 80) || '点击查看详情';
  try {
    // 以推文 id 作为通知 id，同一条推文不会重复弹
    chrome.notifications.create(String(t.id), {
      type: 'basic',
      title: 'X Radar 新推文',
      message: author + ': ' + snippet,
    });
  } catch (e) { /* 通知创建失败时静默跳过 */ }
}

/* ---------------- 通知点击 / 未读管理 ---------------- */

chrome.notifications.onClicked.addListener((notifId) => {
  storageGet('unreadItems').then(({ unreadItems = [] }) => {
    const item = (Array.isArray(unreadItems) ? unreadItems : []).find((u) => u.id === notifId);
    if (item && item.url) chrome.tabs.create({ url: item.url });
    markRead(notifId);
  });
});

function markRead(id) {
  storageGet('unreadItems').then(({ unreadItems = [] }) => {
    const next = (Array.isArray(unreadItems) ? unreadItems : []).filter((u) => u.id !== id);
    storageSet({ unreadItems: next }).then(() => updateBadge(next.length));
  });
}

function updateBadge(count) {
  try {
    chrome.action.setBadgeBackgroundColor({ color: '#f4212e' });
    chrome.action.setBadgeText({ text: count > 0 ? String(Math.min(count, 99)) : '' });
  } catch (e) { /* ignore */ }
}
