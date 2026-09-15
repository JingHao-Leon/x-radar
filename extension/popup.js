/**
 * X Radar popup 逻辑
 * 所有网络请求经 chrome.runtime.sendMessage({type:'xr_fetch'}) 走 background 代理
 * （统一带 X-Radar-Token，避免 CORS 问题）
 */
(() => {
  'use strict';

  const DEFAULT_SERVER = 'http://127.0.0.1:8787';
  const $ = (id) => document.getElementById(id);

  /* ---------- 经 background 代理的请求 ---------- */

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
        resolve({ ok: false, status: 0, error: String(e) });
      }
    });
  }

  function baseOf(server) {
    return (server || DEFAULT_SERVER).replace(/\/+$/, '');
  }

  /* ---------- 设置与健康检查 ---------- */

  async function loadSettings() {
    const { server = '', token = '' } = await chrome.storage.local.get(['server', 'token']);
    $('server').value = server || DEFAULT_SERVER;
    $('token').value = token || '';
  }

  async function checkHealth() {
    const dot = $('dot');
    const txt = $('statusText');
    const health = $('health');
    dot.className = 'dot dot-wait';
    txt.textContent = '检测中…';
    health.classList.add('hidden');

    const resp = await xrFetch('/api/health');
    if (resp.ok && resp.data && resp.data.ok) {
      dot.className = 'dot dot-ok';
      txt.textContent = '已连接';
      $('hAccounts').textContent = resp.data.accounts != null ? resp.data.accounts : '-';
      $('hTweets').textContent = resp.data.tweets != null ? resp.data.tweets : '-';
      $('hLlm').textContent = resp.data.llm ? '已启用' : '规则摘要';
      $('hLlm').className = 'badge ' + (resp.data.llm ? 'badge-on' : 'badge-off');
      health.classList.remove('hidden');
    } else {
      dot.className = 'dot dot-err';
      txt.textContent = resp.status ? 'HTTP ' + resp.status : '无法连接';
      health.classList.add('hidden');
    }
  }

  $('save').addEventListener('click', async () => {
    const server = baseOf($('server').value.trim() || DEFAULT_SERVER);
    const token = $('token').value.trim();
    await chrome.storage.local.set({ server, token });
    $('server').value = server; // 回写规范化后的地址
    await checkHealth();
  });

  /* ---------- 未读列表 ---------- */

  function formatTime(iso) {
    const d = new Date(iso || 0);
    if (!d.getTime()) return '';
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    return Math.floor(diff / 86400) + ' 天前';
  }

  async function renderUnread() {
    const { unreadItems = [] } = await chrome.storage.local.get('unreadItems');
    const list = $('unreadList');
    const empty = $('empty');
    list.textContent = '';
    const items = (Array.isArray(unreadItems) ? unreadItems : []).slice(0, 10);
    if (!items.length) {
      empty.style.display = 'block';
      return;
    }
    empty.style.display = 'none';
    items.forEach((item) => {
      const li = document.createElement('li');
      li.className = 'unread-item';

      const head = document.createElement('div');
      head.className = 'u-head';
      const name = document.createElement('span');
      name.className = 'u-handle';
      name.textContent = item.handle ? '@' + item.handle : '未知作者';
      const time = document.createElement('span');
      time.className = 'u-time';
      time.textContent = formatTime(item.first_seen_at);
      head.append(name, time);

      const body = document.createElement('div');
      body.className = 'u-text';
      body.textContent = item.text || '';
      li.append(head, body);

      li.addEventListener('click', () => openTweet(item));
      list.appendChild(li);
    });
  }

  async function openTweet(item) {
    if (item.url) {
      try { chrome.tabs.create({ url: item.url }); } catch (e) { /* ignore */ }
    }
    // 通知 background 移除该条并重算角标
    try {
      chrome.runtime.sendMessage({ type: 'xr_mark_read', id: item.id }, () => void chrome.runtime.lastError);
    } catch (e) { /* ignore */ }
    await renderUnread();
  }

  /* ---------- 监控面板入口 ---------- */

  $('openPanel').addEventListener('click', async (e) => {
    e.preventDefault();
    const { server = '' } = await chrome.storage.local.get('server');
    try { chrome.tabs.create({ url: baseOf(server) + '/' }); } catch (err) { /* ignore */ }
    window.close();
  });

  /* ---------- 启动 ---------- */

  loadSettings().then(() => {
    checkHealth();
    renderUnread();
  });
})();
