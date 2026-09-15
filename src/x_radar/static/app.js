/*
 * X Radar 仪表盘前端（原生 JS，无构建 / 无外部依赖）
 *
 * 安全约定：
 *  - 所有动态文本一律通过 textContent 写入（el 助手的 text 属性），杜绝 XSS；
 *  - src / href 等 URL 属性赋值前必须通过 safeUrl 白名单校验（仅 ^https?://）。
 */
'use strict';

/* ================= 常量与基础工具 ================= */

// 页面由 FastAPI 同源托管；直接以文件打开时回退到默认服务地址
const SERVER = /^https?:$/.test(location.protocol) ? location.origin : 'http://127.0.0.1:8787';
const TOKEN = new URLSearchParams(location.search).get('token') || localStorage.getItem('xr_token') || '';
const FEED_LIMIT = 50;

/** URL 白名单：仅接受 http(s) 绝对地址，其余一律拒绝 */
function safeUrl(u) {
  return typeof u === 'string' && /^https?:\/\//i.test(u) ? u : null;
}

/** 服务端媒体相对路径（/media/...）→ 绝对地址后再过白名单 */
function mediaUrl(p) {
  if (!p || typeof p !== 'string') return null;
  if (/^https?:\/\//i.test(p)) return safeUrl(p);
  return safeUrl(SERVER + (p.startsWith('/') ? p : '/' + p));
}

/** 带可选 X-Radar-Token 的 fetch */
function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers);
  if (TOKEN) headers['X-Radar-Token'] = TOKEN;
  return fetch(path, Object.assign({}, opts, { headers }));
}

/**
 * DOM 构建助手：
 *  - text 属性 → textContent（防注入）；
 *  - src/href 属性 → 经 safeUrl 校验后才写入；
 *  - onXxx 函数属性 → addEventListener。
 */
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') {
      node.className = value;
    } else if (key === 'text') {
      node.textContent = value;
    } else if (key === 'dataset') {
      Object.assign(node.dataset, value);
    } else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'src' || key === 'href') {
      const u = safeUrl(String(value));
      if (u) node.setAttribute(key, u);
    } else {
      node.setAttribute(key, String(value));
    }
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/* ================= 时间工具 ================= */

function tsOf(iso) {
  const t = new Date(iso).getTime();
  return Number.isFinite(t) ? t : null;
}

/** 相对时间：x 秒/分钟/小时/天前 */
function relTime(iso) {
  const t = tsOf(iso);
  if (t === null) return '—';
  const sec = Math.floor((Date.now() - t) / 1000);
  if (sec < 10) return '刚刚';
  if (sec < 60) return sec + ' 秒前';
  const min = Math.floor(sec / 60);
  if (min < 60) return min + ' 分钟前';
  const hr = Math.floor(min / 60);
  if (hr < 24) return hr + ' 小时前';
  const day = Math.floor(hr / 24);
  if (day < 7) return day + ' 天前';
  return new Date(t).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' });
}

function fmtAbs(iso) {
  const t = tsOf(iso);
  return t === null ? '' : new Date(t).toLocaleString('zh-CN', { hour12: false });
}

/** 下次轮询倒计时 */
function fmtCountdown(iso) {
  const t = tsOf(iso);
  if (t === null) return '—';
  let s = Math.round((t - Date.now()) / 1000);
  if (s <= 0) return '即将轮询';
  const m = Math.floor(s / 60);
  s %= 60;
  return (m > 0 ? m + ' 分 ' : '') + s + ' 秒后';
}

function fmtNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString('en-US') : String(v);
}

/* ================= 全局状态与 DOM 引用 ================= */

const state = {
  health: null,
  status: null,
  accounts: [],
  tweets: [],
  filterHandle: '', // '' = 全部
  q: '',
};

const tweetEls = new Map(); // tweet.id -> 当前在 DOM 中的节点（用于原位替换）

const $ = (sel) => document.querySelector(sel);
const feedEl = $('#feed');
const emptyEl = $('#emptyState');
const feedCountEl = $('#feedCount');
const chipsEl = $('#chips');
const accountListEl = $('#accountList');
const accountCountEl = $('#accountCount');
const accountMsgEl = $('#accountMsg');
const addFormEl = $('#addAccountForm');
const addInputEl = $('#addAccountInput');
const searchEl = $('#searchInput');
const llmBadgeEl = $('#llmBadge');
const connEl = $('#connState');
const lastPollEl = $('#lastPoll');
const nextPollEl = $('#nextPoll');
const pollCountEl = $('#pollCount');
const processedEl = $('#processedCount');
const failedEl = $('#failedCount');
const healthMetaEl = $('#healthMeta');
const lightboxEl = $('#lightbox');
const lightboxImgEl = $('#lightboxImg');

/* ================= 图片懒加载 ================= */

const lazyObserver = new IntersectionObserver(
  (entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const img = entry.target;
      lazyObserver.unobserve(img);
      const src = img.dataset.src;
      if (src) {
        img.src = src; // data-src 生成时已经过 safeUrl 校验
        img.removeAttribute('data-src');
      }
    }
  },
  { rootMargin: '240px' }
);

function observeLazy(root) {
  if (!root) return;
  root.querySelectorAll('img[data-src]').forEach((img) => lazyObserver.observe(img));
}

/* ================= Lightbox ================= */

function openLightbox(src) {
  if (!src) return;
  lightboxImgEl.src = src;
  lightboxEl.classList.remove('hidden');
  document.body.classList.add('modal-open');
}

function closeLightbox() {
  lightboxEl.classList.add('hidden');
  lightboxImgEl.removeAttribute('src');
  document.body.classList.remove('modal-open');
}

/* ================= 通用小组件 ================= */

/** 头像：加载失败回退首字母圆块 */
function avatarNode(nameText, handle, url) {
  const wrap = el('div', { class: 'avatar' });
  const letter = (String(nameText || handle || '?').trim().charAt(0) || '?').toUpperCase();
  const fallback = () => wrap.replaceChildren(el('span', { class: 'avatar-fallback', text: letter }));
  const u = safeUrl(url);
  if (u) {
    const img = el('img', { src: u, alt: '', loading: 'lazy', referrerpolicy: 'no-referrer' });
    img.addEventListener('error', fallback, { once: true });
    wrap.append(img);
  } else {
    fallback();
  }
  return wrap;
}

/** kind 徽章：转推（含 rt_handle）/ 回复 / 引用 */
function kindBadge(t) {
  if (t.kind === 'retweet') {
    return el('span', { class: 'badge badge-kind', text: t.rt_handle ? '转推 @' + t.rt_handle : '转推' });
  }
  if (t.kind === 'reply') return el('span', { class: 'badge badge-kind', text: '回复' });
  if (t.kind === 'quote') return el('span', { class: 'badge badge-kind', text: '引用' });
  return null;
}

function svgIcon(pathD, size = 13) {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', size);
  svg.setAttribute('height', size);
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  const path = document.createElementNS(NS, 'path');
  path.setAttribute('d', pathD);
  svg.append(path);
  return svg;
}
const ICON_EXTERNAL = 'M7 17L17 7M9 7h8v8';

function spinnerNode() {
  return el('span', { class: 'spinner', 'aria-hidden': 'true' });
}

/* ================= 侧栏：健康 / 状态 / 账号 ================= */

function renderHealth() {
  const h = state.health;
  if (!h) return;
  llmBadgeEl.replaceChildren(
    el('span', {
      class: 'badge ' + (h.llm ? 'badge-ai' : 'badge-rule'),
      text: h.llm ? 'AI 摘要已启用' : '规则摘要模式',
    })
  );
  const bits = [];
  if (h.version) bits.push('v' + h.version);
  if (Number.isFinite(h.tweets)) bits.push('已收录 ' + h.tweets + ' 条推文');
  if (Number.isFinite(h.accounts)) bits.push('监控 ' + h.accounts + ' 个账号');
  healthMetaEl.textContent = bits.join(' · ');
}

function renderStatus() {
  const s = state.status;
  if (!s) return;
  lastPollEl.textContent = s.last_poll_at ? relTime(s.last_poll_at) : '尚未轮询';
  lastPollEl.title = fmtAbs(s.last_poll_at);
  pollCountEl.textContent = fmtNum(s.poll_count);
  processedEl.textContent = fmtNum(s.processed);
  failedEl.textContent = fmtNum(s.failed);
  tickStatus();
}

/** 每秒刷新：下次轮询倒计时 + 上次轮询相对时间 */
function tickStatus() {
  const s = state.status;
  nextPollEl.textContent = s && s.next_poll_at ? fmtCountdown(s.next_poll_at) : '—';
  if (s && s.last_poll_at) {
    lastPollEl.textContent = relTime(s.last_poll_at);
    lastPollEl.title = fmtAbs(s.last_poll_at);
  }
}

function setConn(ok) {
  connEl.className = 'conn ' + (ok ? 'conn-ok' : 'conn-err');
  connEl.replaceChildren(
    el('i', { class: 'dot' }),
    el('span', { text: ok ? '实时连接正常' : '连接中断，自动重连中…' })
  );
}

function renderAccounts() {
  accountCountEl.textContent = state.accounts.length ? String(state.accounts.length) : '';
  if (!state.accounts.length) {
    accountListEl.replaceChildren(el('li', { class: 'account-empty', text: '暂无监控账号，先添加一个吧' }));
    return;
  }
  accountListEl.replaceChildren(...state.accounts.map(accountItem));
}

function accountItem(acc) {
  const handle = acc.handle || '';
  const name = acc.name || handle;
  const meta = ['@' + handle];
  if (Number.isFinite(acc.tweet_count)) meta.push(acc.tweet_count + ' 条');
  return el(
    'li',
    { class: 'account-item' },
    avatarNode(name, handle, acc.avatar),
    el(
      'div',
      { class: 'account-meta' },
      el('span', { class: 'account-name', text: name }),
      el('span', { class: 'account-handle', text: meta.join(' · ') })
    ),
    el('button', {
      class: 'icon-btn',
      type: 'button',
      title: '取消监控 @' + handle,
      'aria-label': '取消监控 @' + handle,
      onclick: () => deleteAccount(handle),
    }, '✕')
  );
}

let msgTimer = null;
function showAccountMsg(text, isError) {
  accountMsgEl.textContent = text || '';
  accountMsgEl.className = 'form-msg ' + (isError ? 'err' : 'ok');
  clearTimeout(msgTimer);
  if (text) msgTimer = setTimeout(() => { accountMsgEl.textContent = ''; }, 4000);
}

async function refreshAccounts() {
  try {
    const r = await api('/api/accounts');
    if (!r.ok) return;
    const d = await r.json();
    state.accounts = Array.isArray(d.accounts) ? d.accounts : [];
    renderAccounts();
    renderChips();
  } catch { /* 服务不可达时保持现状 */ }
}

async function addAccount(raw) {
  try {
    const res = await api('/api/accounts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ handle: raw }),
    });
    if (res.status === 201) {
      addInputEl.value = '';
      showAccountMsg('已添加 @' + raw, false);
      await refreshAccounts();
    } else if (res.status === 409) {
      showAccountMsg('@' + raw + ' 已在监控列表中', true);
    } else if (res.status === 400) {
      showAccountMsg('handle 非法（1-15 位字母、数字、下划线）', true);
    } else {
      let detail = '';
      try { detail = (await res.json()).detail || ''; } catch { /* ignore */ }
      showAccountMsg('添加失败' + (detail ? '：' + detail : '（HTTP ' + res.status + '）'), true);
    }
  } catch {
    showAccountMsg('网络错误，无法连接服务', true);
  }
}

async function deleteAccount(handle) {
  if (!window.confirm('取消监控 @' + handle + '？')) return;
  try {
    const r = await api('/api/accounts/' + encodeURIComponent(handle), { method: 'DELETE' });
    if (r.ok) {
      showAccountMsg('已取消监控 @' + handle, false);
      await refreshAccounts();
    } else {
      showAccountMsg('删除失败（HTTP ' + r.status + '）', true);
    }
  } catch {
    showAccountMsg('网络错误，无法连接服务', true);
  }
}

/* ================= 筛选 chips / 搜索 ================= */

function renderChips() {
  const mk = (handle, label) =>
    el('button', {
      class: 'chip' + (state.filterHandle === handle ? ' chip-active' : ''),
      type: 'button',
      role: 'tab',
      'aria-selected': state.filterHandle === handle ? 'true' : 'false',
      onclick: () => applyFilter(handle),
    }, label);
  const chips = [mk('', '全部')];
  for (const a of state.accounts) chips.push(mk(a.handle, '@' + a.handle));
  chipsEl.replaceChildren(...chips);
}

function applyFilter(handle) {
  if (state.filterHandle === handle) return;
  state.filterHandle = handle;
  renderChips();
  renderStream();   // 先在已加载集合上本地过滤
  refetchFeed();    // 再按 handle 重新拉取服务端数据
}

async function refetchFeed() {
  const params = new URLSearchParams({ limit: String(FEED_LIMIT) });
  if (state.filterHandle) params.set('handle', state.filterHandle);
  try {
    const r = await api('/api/feed?' + params.toString());
    if (!r.ok) return;
    const d = await r.json();
    state.tweets = Array.isArray(d.tweets) ? d.tweets : [];
    renderStream();
  } catch { /* 网络失败时保留本地集合 */ }
}

/** 单条推文是否满足当前筛选 + 搜索条件 */
function tweetVisible(t) {
  if (state.filterHandle && String(t.handle || '').toLowerCase() !== state.filterHandle.toLowerCase()) {
    return false;
  }
  const q = state.q.toLowerCase();
  if (!q) return true;
  return [t.text, t.author_name, t.handle, t.summary, t.rt_handle]
    .filter(Boolean)
    .join('\n')
    .toLowerCase()
    .includes(q);
}

function visibleTweets() {
  return state.tweets.filter(tweetVisible);
}

/* ================= 推文卡片流 ================= */

function renderStream() {
  tweetEls.clear();
  const list = visibleTweets();
  feedEl.replaceChildren(...list.map(renderTweet));
  for (const node of tweetEls.values()) observeLazy(node);
  updateEmpty(list.length);
}

function updateEmpty(visibleCount) {
  const n = typeof visibleCount === 'number' ? visibleCount : visibleTweets().length;
  feedCountEl.textContent = state.tweets.length ? '共 ' + state.tweets.length + ' 条' : '';
  const hasAny = state.tweets.length > 0;
  emptyEl.classList.toggle('hidden', n > 0);
  if (n === 0) {
    emptyEl.textContent = hasAny
      ? '没有匹配的推文，试试调整搜索或筛选条件'
      : '暂无推文 —— 添加监控账号，或等待下一轮轮询';
  }
}

function handleTweetNew(t) {
  if (!t || !t.id) return;
  if (state.tweets.some((x) => x.id === t.id)) {
    handleTweetUpdate(t);
    return;
  }
  state.tweets.unshift(t);
  if (tweetVisible(t)) {
    const node = renderTweet(t);
    node.classList.add('flash');
    node.addEventListener('animationend', () => node.classList.remove('flash'), { once: true });
    feedEl.prepend(node);
    observeLazy(node);
  }
  updateEmpty();
}

function handleTweetUpdate(t) {
  if (!t || !t.id) return;
  const idx = state.tweets.findIndex((x) => x.id === t.id);
  if (idx >= 0) state.tweets[idx] = t;
  else state.tweets.unshift(t);
  replaceTweetEl(t);
  updateEmpty();
}

function replaceTweetEl(t) {
  const old = tweetEls.get(t.id);
  if (!old) return; // 当前被筛选隐藏，等下次 renderStream 自然出现
  const fresh = renderTweet(t);
  old.replaceWith(fresh);
  observeLazy(fresh);
}

/** 渲染单条 TweetCard（字段严格对应 docs/API.md 契约） */
function renderTweet(t) {
  const card = el('article', { class: 'tweet', dataset: { id: t.id } });

  /* 头部：头像 / 作者名 @handle / kind 徽章 / 相对时间 / 原文链接 */
  const created = t.created_at || t.first_seen_at || '';
  card.append(
    el(
      'header',
      { class: 'tweet-head' },
      avatarNode(t.author_name, t.handle, t.avatar),
      el(
        'div',
        { class: 'tweet-author' },
        el(
          'div',
          { class: 'tweet-names' },
          el('span', { class: 'tweet-name', text: t.author_name || t.handle || '未知用户' }),
          el('span', { class: 'tweet-handle', text: '@' + (t.handle || '') }),
          kindBadge(t)
        )
      ),
      el(
        'div',
        { class: 'tweet-side' },
        el('time', {
          class: 'tweet-time',
          datetime: created,
          dataset: { ts: created },
          title: fmtAbs(created),
          text: relTime(created),
        }),
        t.url
          ? el('a', { class: 'btn-link', href: t.url, target: '_blank', rel: 'noopener noreferrer' }, '原文', svgIcon(ICON_EXTERNAL))
          : null
      )
    )
  );

  /* 正文：textContent 保留换行 */
  if (t.text) card.append(el('div', { class: 'tweet-text', text: t.text }));

  /* ✨ 摘要块：extractive 标注"规则摘要"，其余显示模型名 */
  if (t.summary) {
    const badge =
      t.summary_model === 'extractive'
        ? el('span', { class: 'badge badge-rule', text: '规则摘要' })
        : el('span', { class: 'badge badge-ai', text: t.summary_model || 'AI 摘要' });
    card.append(
      el(
        'div',
        { class: 'summary' },
        el('div', { class: 'summary-head' }, el('span', { text: '✨' }), el('span', { text: '摘要' }), badge),
        el('p', { class: 'summary-text', text: t.summary })
      )
    );
  }

  /* 截图 / 处理管线状态（pending|processing 骨架屏，failed 红字+重试） */
  card.append(pipelineNode(t));

  /* 引用网页列表 */
  const pages = pagesNode(t);
  if (pages) card.append(pages);

  /* 互动数据 */
  const metrics = metricsNode(t);
  if (metrics) card.append(metrics);

  tweetEls.set(t.id, card);
  return card;
}

/** 推文管线状态区：pending/processing → 骨架屏；failed → 重试；done → 懒加载截图 */
function pipelineNode(t) {
  if (t.status === 'pending' || t.status === 'processing') {
    return el(
      'div',
      { class: 'shot-skeleton' },
      el('div', { class: 'skeleton sk-shot', 'aria-hidden': 'true' }),
      el(
        'div',
        { class: 'pipeline-note' },
        spinnerNode(),
        el('span', { text: t.status === 'processing' ? '截图与摘要生成中…' : '已加入处理队列，等待中…' })
      )
    );
  }
  if (t.status === 'failed') {
    return el(
      'div',
      { class: 'pipeline-failed' },
      el('span', { class: 'err-text', text: '✕ 截图 / 摘要处理失败' }),
      el('button', {
        class: 'btn btn-retry',
        type: 'button',
        onclick: (e) => retryTweet(t.id, e.currentTarget),
      }, '↻ 重试')
    );
  }
  const shot = mediaUrl(t.shot);
  if (shot) {
    const img = el('img', {
      class: 'tweet-shot',
      dataset: { src: shot },
      alt: '推文截图',
      loading: 'lazy',
    });
    img.addEventListener('load', () => img.classList.add('shot-loaded'), { once: true });
    img.addEventListener('click', () => openLightbox(img.dataset.src || img.src));
    return el('div', { class: 'shot-wrap' }, img);
  }
  return null;
}

/** POST /api/tweets/{id}/refresh 重跑管线；成功后本卡片先切回骨架屏等 SSE 更新 */
async function retryTweet(id, btn) {
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = '重试中…';
  try {
    const r = await api('/api/tweets/' + encodeURIComponent(id) + '/refresh', { method: 'POST' });
    if (r.ok) {
      const t = state.tweets.find((x) => x.id === id);
      if (t) {
        t.status = 'processing';
        replaceTweetEl(t);
      }
    } else {
      btn.disabled = false;
      btn.textContent = '↻ 重试';
    }
  } catch {
    btn.disabled = false;
    btn.textContent = '↻ 重试';
  }
}

/** 引用网页列表：favicon 占位 / 标题 / 状态徽章 / 缩略图 / 摘要 */
function pagesNode(t) {
  if (!Array.isArray(t.pages) || t.pages.length === 0) return null;
  return el(
    'section',
    { class: 'pages' },
    el('h3', { class: 'pages-title', text: '引用网页 · ' + t.pages.length }),
    el('ul', { class: 'page-list' }, t.pages.map((p, i) => pageItem(p, i)))
  );
}

function pageItem(p, idx) {
  const map = { done: 'ok', pending: 'wait', failed: 'err', skipped: 'skip' };
  const labels = { ok: '完成', wait: '处理中', err: '失败', skip: '跳过' };
  const cls = map[p.status] || 'skip';
  const label = labels[cls];

  const href = safeUrl(p.final_url || p.url);
  const title = p.title || (href ? href : '网页 ' + (idx + 1));

  const thumb = el('div', { class: 'page-thumb' });
  const mu = mediaUrl(p.shot);
  if (p.status === 'pending' || p.status === 'processing') {
    thumb.classList.add('skeleton');
  } else if (mu) {
    const img = el('img', { dataset: { src: mu }, alt: '网页截图', loading: 'lazy' });
    img.addEventListener('load', () => img.classList.add('shot-loaded'), { once: true });
    img.addEventListener('click', () => openLightbox(img.dataset.src || img.src));
    thumb.append(img);
  } else {
    thumb.append(el('span', { class: 'page-thumb-ph', text: '📄' }));
  }

  return el(
    'li',
    { class: 'page-item' },
    thumb,
    el(
      'div',
      { class: 'page-body' },
      el(
        'div',
        { class: 'page-head' },
        el('span', { class: 'page-fav', text: '🌐' }),
        href
          ? el('a', { class: 'page-title', href: href, target: '_blank', rel: 'noopener noreferrer', text: title })
          : el('span', { class: 'page-title', text: title }),
        el('span', { class: 'badge badge-' + cls, text: label })
      ),
      p.summary ? el('p', { class: 'page-summary', text: p.summary }) : null,
      p.error ? el('p', { class: 'page-error', text: '错误：' + p.error }) : null
    )
  );
}

function metricsNode(t) {
  const m = t.metrics;
  if (!m || typeof m !== 'object') return null;
  const bits = [];
  if (m.likes != null) bits.push(['♥', m.likes]);
  if (m.retweets != null) bits.push(['🔁', m.retweets]);
  if (m.replies != null) bits.push(['💬', m.replies]);
  if (!bits.length) return null;
  return el(
    'div',
    { class: 'tweet-metrics' },
    bits.map(([icon, v]) => el('span', { class: 'metric' }, el('span', { text: icon }), el('span', { text: fmtNum(v) })))
  );
}

/** 首次加载前的骨架屏占位 */
function renderSkeletons() {
  const card = () =>
    el(
      'div',
      { class: 'tweet skeleton-card', 'aria-hidden': 'true' },
      el(
        'div',
        { class: 'sk-row' },
        el('div', { class: 'skeleton sk-avatar' }),
        el(
          'div',
          { class: 'sk-lines' },
          el('div', { class: 'skeleton sk-line w40' }),
          el('div', { class: 'skeleton sk-line w60' })
        )
      ),
      el('div', { class: 'skeleton sk-line w90' }),
      el('div', { class: 'skeleton sk-line w70' }),
      el('div', { class: 'skeleton sk-shot' })
    );
  feedEl.replaceChildren(card(), card(), card());
}

/* ================= SSE 实时更新 ================= */

function sseData(e) {
  try {
    return JSON.parse(e.data);
  } catch {
    return null;
  }
}

let eventSource = null;

function connectStream() {
  try {
    eventSource = new EventSource('/api/stream');
  } catch {
    setConn(false);
    return;
  }
  eventSource.onopen = () => setConn(true);
  eventSource.onerror = () => setConn(false);
  eventSource.addEventListener('tweet_new', (e) => {
    const t = sseData(e);
    if (t) handleTweetNew(t);
  });
  eventSource.addEventListener('tweet_update', (e) => {
    const t = sseData(e);
    if (t) handleTweetUpdate(t);
  });
  eventSource.addEventListener('account_update', (e) => {
    const d = sseData(e);
    if (!d) return;
    state.accounts = Array.isArray(d) ? d : Array.isArray(d.accounts) ? d.accounts : [];
    renderAccounts();
    renderChips();
  });
  eventSource.addEventListener('status', (e) => {
    const d = sseData(e);
    if (!d) return;
    state.status = d;
    renderStatus();
  });
  eventSource.addEventListener('ping', () => setConn(true));
}

/* ================= 事件绑定与初始化 ================= */

function bindStaticEvents() {
  // 添加账号：@ 可省略；409 → 已存在提示
  addFormEl.addEventListener('submit', (e) => {
    e.preventDefault();
    const raw = addInputEl.value.trim().replace(/^@+/, '');
    if (!raw) {
      showAccountMsg('请输入账号 handle', true);
      return;
    }
    if (!/^[\w]{1,15}$/.test(raw)) {
      showAccountMsg('handle 非法（1-15 位字母、数字、下划线）', true);
      return;
    }
    addAccount(raw);
  });

  // 搜索：在已加载集合上本地过滤（防抖）
  let searchTimer = null;
  searchEl.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.q = searchEl.value.trim();
      renderStream();
    }, 200);
  });

  // Lightbox：点击遮罩 / 关闭按钮 / ESC 关闭
  lightboxEl.addEventListener('click', (e) => {
    if (e.target === lightboxEl || e.target.closest('.lightbox-close')) closeLightbox();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !lightboxEl.classList.contains('hidden')) closeLightbox();
  });
}

async function init() {
  bindStaticEvents();
  renderSkeletons();
  renderChips();
  renderAccounts();

  // 首次加载：并行拉取健康 / 状态 / 账号 / 信息流
  const [h, s, a, f] = await Promise.allSettled([
    api('/api/health').then((r) => (r.ok ? r.json() : null)),
    api('/api/status').then((r) => (r.ok ? r.json() : null)),
    api('/api/accounts').then((r) => (r.ok ? r.json() : null)),
    api('/api/feed?limit=' + FEED_LIMIT).then((r) => (r.ok ? r.json() : null)),
  ]).then((rs) => rs.map((x) => (x.status === 'fulfilled' ? x.value : null)));

  if (h) state.health = h;
  if (s) state.status = s;
  if (a) state.accounts = Array.isArray(a.accounts) ? a.accounts : [];
  if (f) state.tweets = Array.isArray(f.tweets) ? f.tweets : [];

  renderHealth();
  renderStatus();
  renderAccounts();
  renderChips();
  renderStream();

  connectStream();
}

/* 周期刷新：倒计时每秒；卡片相对时间每 30 秒 */
setInterval(tickStatus, 1000);
setInterval(() => {
  document.querySelectorAll('time[data-ts]').forEach((n) => {
    if (n.dataset.ts) n.textContent = relTime(n.dataset.ts);
  });
}, 30000);

init();
