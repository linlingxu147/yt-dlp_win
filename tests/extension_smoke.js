'use strict';
/*
 * background.js 冒烟测试（Node + 假 chrome API）
 *
 * 重点验证 MV3 的真实坑：Service Worker 空闲约 30 秒被回收后，模块级变量全部清零，
 * 嗅探结果必须还能从 chrome.storage.session 里捞回来。
 * 另外验证端口发现用 /api/status 的 app 标识（并对旧版服务保留 busy 字段回退）。
 *
 * 运行：node tests/extension_smoke.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const SRC = fs.readFileSync(
  path.join(__dirname, '..', 'sniffer_extension', 'background.js'), 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function makeChrome(storage, calls) {
  const listeners = {};
  const on = (name) => ({
    addListener(fn) { (listeners[name] = listeners[name] || []).push(fn); },
    removeListener() {},
  });
  const get = (obj, keys) => {
    if (keys == null) return { ...obj };
    const list = Array.isArray(keys) ? keys : [keys];
    const out = {};
    for (const k of list) if (k in obj) out[k] = obj[k];
    return out;
  };
  return {
    __listeners: listeners,
    __calls: calls,
    runtime: {
      lastError: undefined,
      onInstalled: on('installed'),
      onMessage: on('onMessage'),
    },
    webRequest: { onHeadersReceived: on('headers') },
    tabs: { onRemoved: on('tabRemoved'), onUpdated: on('tabUpdated'),
            sendMessage: (tabId, msg, cb) => { calls.push(['sendMessage', tabId, msg && msg.type]);
                                               if (cb) cb({}); return Promise.resolve(); } },
    action: { onClicked: on('actionClicked'),
              setBadgeText: (o) => calls.push(['badge', o.text]),
              setBadgeBackgroundColor: () => {} },
    contextMenus: { create: () => {}, removeAll: () => {}, onClicked: on('menuClicked') },
    cookies: { getAll: async () => [{ domain: '.x.com', name: 'sid', value: '1', path: '/' }] },
    storage: {
      local: {
        get: async (keys) => get(storage.local, keys),
        set: async (obj) => Object.assign(storage.local, obj),
        remove: async (keys) => { for (const k of [].concat(keys)) delete storage.local[k]; },
      },
      session: {
        get: async (keys) => get(storage.session, keys),
        set: async (obj) => Object.assign(storage.session, obj),
        remove: async (keys) => { for (const k of [].concat(keys)) delete storage.session[k]; },
      },
    },
  };
}

// 起一个"新的 Service Worker 实例"：模块级变量全空，但 storage 是共享的
function loadWorker(storage, calls, fetchImpl) {
  const chrome = makeChrome(storage, calls);
  const sandbox = { chrome, console, setTimeout, clearTimeout, Promise, JSON, Date, URL,
                    fetch: fetchImpl || (async () => { throw new Error('offline'); }) };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox, { filename: 'background.js' });
  return { chrome, listeners: chrome.__listeners };
}

function fire(listeners, name, ...args) {
  return (listeners[name] || []).map((fn) => fn(...args));
}

(async () => {
  const storage = { local: {}, session: {} };
  const calls = [];
  const log = (...a) => console.log('  ', ...a);

  // ---- 1) 记录一条媒体响应 ----
  let w = loadWorker(storage, calls);
  assert.ok(w.listeners.headers && w.listeners.headers.length, 'webRequest 监听器已注册');
  fire(w.listeners, 'headers', {
    tabId: 101, url: 'https://cdn.example.com/a.mp4?x=1', statusCode: 200,
    responseHeaders: [{ name: 'Content-Type', value: 'video/mp4' },
                      { name: 'Content-Length', value: '1048576' }],
  });
  await sleep(450);           // 300ms 合并落盘
  const saved = storage.session['sniff_tab_101'];
  assert.ok(saved && Object.keys(saved).length === 1, '嗅探结果已写入 storage.session');
  log('1) 写入 storage.session:', Object.keys(saved)[0]);

  // ---- 2) 模拟 Service Worker 被回收后重启：内存清空，storage 还在 ----
  w = loadWorker(storage, calls);
  let reply = null;
  fire(w.listeners, 'onMessage', { type: 'query_items', tabId: 101 }, { tab: { id: 101 }, frameId: 0 },
       (r) => { reply = r; });
  await sleep(50);
  assert.ok(reply && reply.items && reply.items.length === 1, '重启后仍能取回嗅探结果');
  log('2) SW 重启后 query_items 返回条目数:', reply.items.length);

  // ---- 3) 页面开始加载时清空 ----
  w = loadWorker(storage, calls);
  fire(w.listeners, 'tabUpdated', 101, { status: 'loading' }, {});
  await sleep(450);
  assert.ok(!storage.session['sniff_tab_101'], '刷新页面后清掉该标签页的缓存');
  log('3) 刷新后 storage.session 已清空');

  // ---- 4) 端口发现：校验 app 标识，且旧版服务（无 app 字段）仍可用 ----
  const seen = [];
  const mkFetch = (statusBody) => async (url, opt) => {
    seen.push([opt && opt.method || 'GET', url]);
    if (url.includes('/api/status')) {
      return { ok: true, json: async () => statusBody };
    }
    return { ok: true, json: async () => ({ ok: true, busy: false, url: 'x' }) };
  };
  storage.session = {};
  w = loadWorker(storage, calls, mkFetch({ app: 'yt-dlp-web', busy: false }));
  fire(w.listeners, 'onMessage', { type: 'submit', url: 'https://cdn.example.com/a.mp4',
                                   kind: 'media', title: 't', referer: 'https://p.example.com/',
                                   ua: 'UA' }, { tab: { id: 7 }, frameId: 0 }, () => {});
  await sleep(80);
  const posted = seen.find(([m, u]) => m === 'POST' && u.includes('/api/sniff'));
  assert.ok(posted, '提交时 POST 到 /api/sniff');
  assert.ok(storage.local.server_port, '端口已缓存到 storage.local');
  log('4) 端口发现 + 提交:', posted[1].replace(/^https?:\/\//, ''), '缓存端口=', storage.local.server_port);

  storage.local = {};
  seen.length = 0;
  w = loadWorker(storage, calls, mkFetch({ busy: false }));   // 旧版服务：没有 app 字段
  fire(w.listeners, 'onMessage', { type: 'submit', url: 'https://cdn.example.com/b.mp4',
                                   kind: 'page', referer: '', ua: '' },
       { tab: { id: 8 }, frameId: 0 }, () => {});
  await sleep(80);
  assert.ok(seen.some(([m, u]) => m === 'POST' && u.includes('/api/sniff')), '旧版服务也能提交');
  log('5) 旧版服务（无 app 字段）回退成功');

  console.log('\nOK  扩展冒烟全部通过（5 项）');
  process.exit(0);
})().catch((e) => { console.error('FAIL', e && e.message); process.exit(1); });
