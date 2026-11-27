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
const RULES_SRC = fs.readFileSync(
  path.join(__dirname, '..', 'sniffer_extension', 'sniff_rules.js'), 'utf8');
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
                    navigator: { userAgent: 'NodeSmoke' },
                    fetch: fetchImpl || (async () => { throw new Error('offline'); }) };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(RULES_SRC, sandbox, { filename: 'sniff_rules.js' });
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
  const bodies = [];
  const mkFetch = (statusBody) => async (url, opt) => {
    seen.push([opt && opt.method || 'GET', url]);
    if (opt && opt.body) {
      try { bodies.push(JSON.parse(opt.body)); } catch (e) { /* ignore */ }
    }
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
  const mediaBody = bodies.find((b) => b && b.kind === 'media');
  assert.ok(mediaBody && mediaBody.cookie_jar.length === 1, '直链提交带 cookies');
  log('4) 端口发现 + 提交:', posted[1].replace(/^https?:\/\//, ''), '缓存端口=', storage.local.server_port);

  storage.local = {};
  seen.length = 0;
  w = loadWorker(storage, calls, mkFetch({ busy: false }));   // 旧版服务：没有 app 字段
  fire(w.listeners, 'onMessage', { type: 'submit', url: 'https://cdn.example.com/b.mp4',
                                   kind: 'page', referer: '', ua: '' },
       { tab: { id: 8 }, frameId: 0 }, () => {});
  await sleep(80);
  assert.ok(seen.some(([m, u]) => m === 'POST' && u.includes('/api/sniff')), '旧版服务也能提交');
  const pageBody = bodies.filter((b) => b && b.kind === 'page').pop();
  assert.ok(pageBody, '下载本页提交体存在');
  assert.ok(pageBody.cookie_jar.length === 1, '下载本页也带 cookies');
  assert.strictEqual(pageBody.referer, 'https://cdn.example.com/b.mp4',
                     'page 没填 Referer 时默认用页面地址');
  log('5) 旧版服务（无 app 字段）回退成功；page 提交带 cookies');

  // ---- 6) 音频流识别（content 脚本公共 helper） ----
  const audioSrc = fs.readFileSync(
    path.join(__dirname, '..', 'sniffer_extension', 'audio_stream.js'), 'utf8');
  const audioBox = {};
  vm.createContext(audioBox);
  vm.runInContext(RULES_SRC, audioBox, { filename: 'sniff_rules.js' });
  vm.runInContext(audioSrc, audioBox, { filename: 'audio_stream.js' });
  assert.ok(audioBox.isAudioStream('https://x/a-1-30280.m4s'), 'B 站 30280 是音频');
  assert.ok(audioBox.isAudioStream('https://x/a.mp3?x=1'), 'mp3 后缀是音频');
  assert.ok(!audioBox.isAudioStream('https://x/a-1-100026.m4s'), 'B 站视频 m4s 不是音频');
  assert.ok(!audioBox.isAudioStream('https://data.bilibili.com/log/web?x=.m4s'),
            '只在查询参数里出现 .m4s 的日志地址不算');
  log('6) 音频流识别通过（302xx / 音频后缀）');

  // ---- 7) 共享规则（与桌面 sniff_rules.py 读同一份 JSON 表） ----
  const rulesBox = {};
  vm.createContext(rulesBox);
  vm.runInContext(RULES_SRC, rulesBox, { filename: 'sniff_rules.js' });
  const SR = rulesBox.SniffRules;
  assert.ok(SR.accept('https://x/a.m3u8', 'application/vnd.apple.mpegurl', 0), 'm3u8 清单可收');
  assert.ok(SR.accept('https://x/a.mp4', '', 0), 'mp4 后缀可收');
  assert.ok(!SR.accept('https://x/page', 'text/html', 999999), 'html 不收');
  assert.ok(!SR.accept('https://x/sound', 'audio/mpeg', 1000), '纯 MIME 小文件不收');
  assert.ok(SR.accept('https://x/sound', 'audio/mpeg', 300000), '纯 MIME 大文件可收');
  assert.ok(!SR.accept('ftp://x/a.mp4', 'video/mp4', 0), '非 http(s) 不收');
  // 日志/埋点把媒体地址塞进查询串：不是媒体，别按 2B 的假视频收
  assert.ok(!SR.accept('https://data.bilibili.com/log/web?x=1&url=https://x/a.mp4',
                       'text/plain; charset=utf-8', 2), '日志查询串里的 .mp4 不算媒体');
  assert.ok(SR.accept('https://x/a.mp4?from=log', 'text/plain; charset=utf-8', 999999),
            '路径本身是媒体后缀才收');
  assert.strictEqual(SR.classify('https://x/a.m3u8', ''), 'stream');
  assert.strictEqual(SR.classify('https://x/a-1-30280.m4s', ''), 'audio');
  assert.strictEqual(SR.classify('https://x/a.mp4', 'video/mp4'), 'video');
  assert.strictEqual(SR.normalizeUrl('  https://x/a.mp4?x=1&amp;y=2  '),
                     'https://x/a.mp4?x=1&y=2');
  log('7) 共享嗅探规则通过');

  // ---- 8) 排队位置提示：服务端新字段 queue_position / task_id 转成面板文案 ----
  const sniffFetch = (sniffBody) => async (url) => {
    if (url.includes('/api/status')) {
      return { ok: true, json: async () => ({ app: 'yt-dlp-web', busy: false }) };
    }
    return { ok: true, json: async () => sniffBody };
  };
  storage.local = {};
  storage.session = {};
  let qresp = null;
  w = loadWorker(storage, calls,
                 sniffFetch({ ok: true, busy: false, url: 'x', task_id: 12, queue_position: 3 }));
  fire(w.listeners, 'onMessage', { type: 'submit', url: 'https://cdn.example.com/q.mp4',
                                   kind: 'media', referer: '', ua: 'UA' },
       { tab: { id: 9 }, frameId: 0 }, (r) => { qresp = r; });
  await sleep(80);
  assert.ok(qresp && qresp.ok, '排队提交仍返回成功');
  assert.strictEqual(qresp.task_id, 12, 'task_id 原样透传');
  assert.ok(/队列/.test(qresp.message || ''), '排队时提示文案含「队列」');
  assert.ok(/3/.test(qresp.message || ''), '提示文案带排队位置');

  // queue_position 为 0（立即开始）或字段缺失（旧版服务）：沿用原提示
  storage.local = {};
  let nresp = null;
  w = loadWorker(storage, calls,
                 sniffFetch({ ok: true, busy: false, url: 'x', task_id: 13, queue_position: 0 }));
  fire(w.listeners, 'onMessage', { type: 'submit', url: 'https://cdn.example.com/r.mp4',
                                   kind: 'media', referer: '', ua: 'UA' },
       { tab: { id: 10 }, frameId: 0 }, (r) => { nresp = r; });
  await sleep(80);
  assert.ok(nresp && nresp.ok && !/队列/.test(nresp.message || '')
            && /开始下载/.test(nresp.message || ''), '未排队时沿用原提示');

  // 旧版服务：响应里根本没有 queue_position，也沿用原提示
  let oresp = null;
  w = loadWorker(storage, calls, sniffFetch({ ok: true, busy: false, url: 'x' }));
  fire(w.listeners, 'onMessage', { type: 'submit', url: 'https://cdn.example.com/s.mp4',
                                   kind: 'media', referer: '', ua: 'UA' },
       { tab: { id: 11 }, frameId: 0 }, (r) => { oresp = r; });
  await sleep(80);
  assert.ok(oresp && oresp.ok && !/队列/.test(oresp.message || '')
            && /开始下载/.test(oresp.message || ''), '旧版服务（无 queue_position）沿用原提示');

  // queue_position 按数值判定：字符串 "0" 不能因为非空字符串而被当成排队
  storage.local = {};
  let zresp = null;
  w = loadWorker(storage, calls,
                 sniffFetch({ ok: true, busy: false, url: 'x', task_id: 14, queue_position: '0' }));
  fire(w.listeners, 'onMessage', { type: 'submit', url: 'https://cdn.example.com/t.mp4',
                                   kind: 'media', referer: '', ua: 'UA' },
       { tab: { id: 12 }, frameId: 0 }, (r) => { zresp = r; });
  await sleep(80);
  assert.ok(zresp && zresp.ok && !/队列/.test(zresp.message || '') && /开始下载/.test(zresp.message || ''),
            '字符串 "0" 不算排队');

  // 字符串数字仍按数值处理（服务端字段类型变化时不丢提示）
  storage.local = {};
  let sresp = null;
  w = loadWorker(storage, calls,
                 sniffFetch({ ok: true, busy: false, url: 'x', task_id: 15, queue_position: '2' }));
  fire(w.listeners, 'onMessage', { type: 'submit', url: 'https://cdn.example.com/u.mp4',
                                   kind: 'media', referer: '', ua: 'UA' },
       { tab: { id: 13 }, frameId: 0 }, (r) => { sresp = r; });
  await sleep(80);
  assert.ok(sresp && sresp.ok && /队列/.test(sresp.message || '') && /2/.test(sresp.message || ''),
            '字符串 "2" 仍提示排队第 2 位');
  log('8) 排队提示 / 未排队回退:', qresp.message, '|', nresp.message, '|', oresp.message,
      '|', zresp.message, '|', sresp.message);

  // ---- 9) DOM 补扫（sniff_rules.js + dom_scan.js，扩展与桌面共用） ----
  const domSrc = fs.readFileSync(
    path.join(__dirname, '..', 'sniffer_extension', 'dom_scan.js'), 'utf8');
  const domBox = { console };
  domBox.globalThis = domBox;
  vm.createContext(domBox);
  vm.runInContext(RULES_SRC, domBox, { filename: 'sniff_rules.js' });
  vm.runInContext(domSrc, domBox, { filename: 'dom_scan.js' });

  // 假元素：只需要扫描器用到的字段
  const el = (tag, props) => Object.assign({
    tagName: tag.toUpperCase(),
    currentSrc: '', src: '', srcset: '',
    naturalWidth: 0, naturalHeight: 0, width: 0, height: 0,
    parentElement: null,
    getAttribute: (n) => (props && props.attrs && n in props.attrs ? props.attrs[n] : null),
  }, props || {});
  const mediaEls = [
    el('video', { currentSrc: 'https://cdn.example.com/v.mp4' }),
    el('audio', { src: 'https://cdn.example.com/a.mp3' }),
    el('source', { src: 'https://cdn.example.com/v2.webm' }),
  ];
  // <picture> 里的 <source>：两个选择器都会选中，必须归到图片而不是视频
  const picSource = el('source', {
    naturalWidth: 900, naturalHeight: 600,
    parentElement: el('picture', {
      attrs: { 'data-lazy-src': 'https://cdn.example.com/pic-lazy.webp' } }),
  });
  mediaEls.push(picSource);
  const imgEls = [
    el('img', { currentSrc: 'https://cdn.example.com/big.jpg',
                naturalWidth: 800, naturalHeight: 600, attrs: { alt: '封面' } }),
    el('img', { src: 'https://cdn.example.com/small.png',
                naturalWidth: 100, naturalHeight: 100 }),
    el('img', { src: 'https://cdn.example.com/set.jpg', naturalWidth: 1200, naturalHeight: 900,
                srcset: 'https://cdn.example.com/set-200.jpg 200w, ' +
                        'https://cdn.example.com/set-1200.jpg 1200w, ' +
                        'https://cdn.example.com/set-800.jpg 800w' }),
    el('img', { naturalWidth: 640, naturalHeight: 480,
                attrs: { 'data-src': 'https://cdn.example.com/lazy.jpg' } }),
    picSource,
    el('source', { srcset: 'https://cdn.example.com/icon.png 64w',
                   naturalWidth: 64, naturalHeight: 64,
                   parentElement: el('picture', {}) }),
    el('img', { src: 'blob:https://cdn.example.com/xyz',
                naturalWidth: 500, naturalHeight: 500 }),
    el('img', { src: 'data:image/png;base64,AAAA', naturalWidth: 500, naturalHeight: 500 }),
    el('img', { currentSrc: 'https://cdn.example.com/big.jpg',
                naturalWidth: 800, naturalHeight: 600 }),          // 同一地址，去重
  ];
  const fakeDoc = {
    querySelectorAll(sel) {
      if (sel === 'video, audio, source') return mediaEls;
      if (sel === 'img, picture source') return imgEls;
      return [];
    },
  };
  const dom = domBox.SniffDom.scan(fakeDoc);
  const byUrl = new Map(dom.map((it) => [it.url, it]));
  const must = (url) => {
    assert.ok(byUrl.has(url), '应包含 ' + url);
    return byUrl.get(url);
  };
  assert.strictEqual(must('https://cdn.example.com/v.mp4').kind, 'video');
  assert.strictEqual(must('https://cdn.example.com/a.mp3').kind, 'audio');
  assert.strictEqual(must('https://cdn.example.com/v2.webm').kind, 'video');
  const big = must('https://cdn.example.com/big.jpg');
  assert.strictEqual(big.kind, 'image');
  assert.strictEqual(big.w, 800);
  assert.strictEqual(big.h, 600);
  assert.strictEqual(big.title, '封面');
  assert.ok(!byUrl.has('https://cdn.example.com/small.png'), '小图（100x100）不收');
  must('https://cdn.example.com/set-1200.jpg');                 // srcset 取最大描述符
  assert.ok(!byUrl.has('https://cdn.example.com/set-200.jpg'), 'srcset 小图不单独收');
  assert.ok(!byUrl.has('https://cdn.example.com/set-800.jpg'), 'srcset 只取最大一项');
  must('https://cdn.example.com/lazy.jpg');                     // data-src 兜底
  assert.strictEqual(must('https://cdn.example.com/pic-lazy.webp').kind, 'image',
                     '<picture> 里的 <source> 归图片');        // picture 上的懒加载
  assert.ok(!byUrl.has('https://cdn.example.com/icon.png'), '<picture> 小图标不收');
  assert.ok(!byUrl.has('blob:https://cdn.example.com/xyz'), 'blob: 不收');
  assert.ok(!dom.some((it) => it.url.startsWith('data:')), 'data: 不收');
  assert.strictEqual(dom.filter((it) => it.url === 'https://cdn.example.com/big.jpg').length,
                     1, '同一次扫描内按 URL 去重');
  assert.strictEqual(dom.length, 7);
  // 上限 300 条
  const many = {
    querySelectorAll: (sel) => (sel === 'video, audio, source'
      ? Array.from({ length: 350 }, (_, i) => el('video', { src: 'https://cdn.example.com/m' + i + '.mp4' }))
      : []),
  };
  assert.strictEqual(domBox.SniffDom.scan(many).length, 300, '单次最多 300 条');
  log('9) DOM 补扫:', dom.map((it) => it.kind).join(','), '| 上限与去重通过');

  // ---- 10) 无后缀签名直链：octet-stream + Content-Range 靠体积/分片兜底 ----
  assert.ok(SR.accept('https://x/videoplayback?id=1', 'application/octet-stream', 5 << 20,
                      { range: false }), '大体积 octet-stream 可收');
  assert.ok(!SR.accept('https://x/videoplayback?id=1', 'application/octet-stream', 1024,
                       { range: false }), '小体积 octet-stream 不收');
  assert.ok(SR.accept('https://x/videoplayback?id=1', 'application/octet-stream', 1024,
                      { range: true }), '带 Content-Range 可收');
  assert.ok(!SR.accept('https://x/videoplayback?id=1', 'text/plain', 5 << 20,
                       { range: false }), 'text/plain 不走兜底');
  assert.ok(!SR.accept('https://x/videoplayback?id=1', 'application/octet-stream', 5 << 20),
            '不传 opts 时维持旧行为');
  assert.ok(!SR.accept('https://s1.hdslb.com/bfs/static/jinkela/long/fo.js',
                       'application/octet-stream', 5 << 20, { range: false }),
            'octet-stream 的静态 JS 不算媒体');

  const storage2 = { local: {}, session: {} };
  w = loadWorker(storage2, calls);
  fire(w.listeners, 'headers', {
    tabId: 201, url: 'https://cdn.example.com/videoplayback?id=1', statusCode: 206,
    responseHeaders: [{ name: 'Content-Type', value: 'application/octet-stream' },
                      { name: 'Content-Range', value: 'bytes 0-999/1234567' }],
  });
  fire(w.listeners, 'headers', {
    tabId: 202, url: 'https://cdn.example.com/installer.zip', statusCode: 200,
    responseHeaders: [{ name: 'Content-Type', value: 'application/octet-stream' },
                      { name: 'Content-Length', value: '2048' }],
  });
  await sleep(450);
  assert.ok(storage2.session['sniff_tab_201'], 'range 命中的直链已落盘');
  assert.ok(!storage2.session['sniff_tab_202'], '没有 range 的小二进制不落盘');
  log('10) 签名直链兜底通过（octet-stream + Content-Range）');

  // ---- 11) 页面钩子补强：responseType=json / WebSocket / 资源时序表 ----
  const injectedSrc = fs.readFileSync(
    path.join(__dirname, '..', 'sniffer_extension', 'injected.js'), 'utf8');

  function loadInjected(entries) {
    const sent = [];
    const winListeners = {};
    class FakeXHR {
      constructor() {
        this.responseType = '';
        this.response = null;
        this.responseText = '';
        this.__listeners = {};
      }
      open(method, url) { this.__url = String(url); }
      addEventListener(type, fn) {
        (this.__listeners[type] = this.__listeners[type] || []).push(fn);
      }
      getResponseHeader() { return ''; }
      fire(type) {
        for (const fn of (this.__listeners[type] || [])) fn({});
      }
    }
    class FakeWS {
      constructor(url) { this.url = String(url); this.__l = {}; }
      addEventListener(type, fn) { (this.__l[type] = this.__l[type] || []).push(fn); }
      fire(type, ev) { for (const fn of (this.__l[type] || [])) fn(ev); }
    }
    const doc = {
      readyState: 'complete',
      addEventListener() {},
    };
    const perf = { getEntriesByType: () => entries || [] };
    const win = {
      dispatchEvent(e) { sent.push(e && e.detail); return true; },
      addEventListener(type, fn) { (winListeners[type] = winListeners[type] || []).push(fn); },
      WebSocket: FakeWS,
      performance: perf,
    };
    const box = {
      window: win, self: null,
      document: doc,
      XMLHttpRequest: FakeXHR,
      CustomEvent: class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } },
      performance: perf,
      crypto: { getRandomValues: (buf) => { for (let i = 0; i < buf.length; i++) buf[i] = i; } },
      setTimeout: () => 0,
      clearTimeout: () => {},
      console, Object, String, Number, Array, Uint8Array, Date, Math, JSON, RegExp, Error,
    };
    box.globalThis = box;
    box.self = box;
    vm.createContext(box);
    vm.runInContext(injectedSrc, box, { filename: 'injected.js' });
    return { box, sent, FakeXHR, FakeWS };
  }

  const inj = loadInjected([
    'https://cdn.example.com/pre.m3u8?t=1',
    'https://cdn.example.com/seg-1.ts',
    'https://cdn.example.com/v.mp4',
  ]);
  const hits = (url) => inj.sent.filter((d) => d && d.url === url);
  assert.ok(hits('https://cdn.example.com/pre.m3u8?t=1').length,
            '时序表里注入前的清单被补扫到');
  assert.ok(hits('https://cdn.example.com/v.mp4').length, '时序表里的整片被补扫到');
  assert.ok(!hits('https://cdn.example.com/seg-1.ts').length,
            '时序表补扫不刷分片');
  assert.strictEqual(hits('https://cdn.example.com/pre.m3u8?t=1')[0].kind, 'm3u8');

  // responseType="json" 的 XHR：以前整条跳过，现在要遍历已解析的对象
  const xhr = new inj.FakeXHR();
  xhr.open('GET', 'https://api.example.com/play?id=1');
  xhr.responseType = 'json';
  xhr.response = { data: { url: 'https://cdn.example.com/json.m3u8' } };
  xhr.fire('load');
  assert.strictEqual(hits('https://cdn.example.com/json.m3u8').length, 1,
                     'responseType=json 能挖到清单');
  // 接口体挖出来的地址沿用原来的 kind="json"，种类由使用方按后缀再判
  assert.strictEqual(hits('https://cdn.example.com/json.m3u8')[0].kind, 'json');

  // WebSocket 下发的清单（JSON 里的转义斜杠要还原）
  const ws = new inj.FakeWS('wss://api.example.com/live');
  ws.addEventListener('message', () => {});
  ws.fire('message', { data: '{"play":"https:\\/\\/cdn.example.com\\/ws.m3u8?token=2"}' });
  assert.strictEqual(hits('https://cdn.example.com/ws.m3u8?token=2').length, 1,
                     'WebSocket 下发的清单能认出来');
  log('11) 页面钩子补强通过（json 响应 / WebSocket / 时序表）');

  console.log('\nOK  扩展冒烟全部通过（11 项）');
  process.exit(0);
})().catch((e) => { console.error('FAIL', e && e.message); process.exit(1); });
