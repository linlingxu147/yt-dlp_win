// background.js —— MV3 Service Worker
// 原理借鉴 FileCentipede 的 background.js + gloryhole：
//   1) webRequest 被动嗅探所有网络响应（content-type / URL 后缀）
//   2) 发现疑似媒体就记到对应标签页的名下，通知页面悬浮条
//   3) 用户点「下载」时带上该域 Cookie / 页面 Referer / UA，
//      POST 到本机 yt-dlp Web 服务（127.0.0.1:8765-8775 端口自动发现）
"use strict";

// 嗅探判定只有一份：sniffer_extension/sniff_rules.js（桌面内嵌浏览器共用）。
// MV3 的经典 Service Worker 用 importScripts 同步加载；Node 冒烟测试会先把它预载进沙箱。
if (typeof importScripts === "function") {
    try { importScripts("sniff_rules.js"); } catch (e) { /* 测试沙箱 / 旧缓存 */ }
}

const PORT_RANGE = [8765, 8766, 8767, 8768, 8769, 8770, 8771, 8772, 8773, 8774, 8775];
const ITEM_TTL_MS = 30 * 60 * 1000;   // 条目保留 30 分钟
const MAX_ITEMS_PER_TAB = 80;
const APP_ID = "yt-dlp-web";          // 服务端 /api/status 的 app 标识

// HLS/DASH 把整片切成几秒一个的分片（.ts/.m4s…），播放器逐个请求，webRequest 全看得见。
// 分片单独下载没有意义（多数还要 key 解密），真正的入口是 .m3u8/.mpd 清单——
// 所以「同一标签里已经有清单」时，后续分片直接不收，列表只留能下整片的那条。
function isManifest(url) { return SniffRules.isManifest(url); }
function isSegment(url) { return SniffRules.isSegment(url); }

// MV3 的 Service Worker 空闲约 30 秒就会被回收，模块级变量随之清零。
// 所以嗅探结果以 chrome.storage.session 为准，TAB_ITEMS 只是读缓存：
// 页面刷新/标签页关闭时同步清理，浏览器重启后由 Chrome 自己丢弃。
const TAB_ITEMS = new Map();          // tabId -> { url: {url, mime, size, ts} }
const HYDRATING = new Map();          // tabId -> Promise（避免并发重复读取）
const DIRTY_TABS = new Set();         // 有改动待落盘的标签页
const STORE_PREFIX = "sniff_tab_";

function storeKey(tabId) { return STORE_PREFIX + tabId; }

/** 这个标签页里是否已经嗅到能下整片的清单。 */
function hasManifest(items) {
    for (const u in items) if (isManifest(u)) return true;
    return false;
}

/** 清单到手后，把之前零散记下的分片清掉（否则列表里还挂着一堆死条目）。 */
function dropSegments(tabId, items) {
    const gone = [];
    for (const u in items) if (isSegment(u)) { delete items[u]; gone.push(u); }
    if (gone.length) {
        try {
            chrome.tabs.sendMessage(tabId, { type: "net_items_gone", urls: gone },
                                    { frameId: 0 }, () => void chrome.runtime.lastError);
        } catch (e) { /* ignore */ }
    }
    return gone.length;
}

function prune(tabId) {
    const items = TAB_ITEMS.get(tabId);
    if (!items) return;
    const now = Date.now();
    for (const u in items) {
        if (now - items[u].ts > ITEM_TTL_MS) delete items[u];
    }
    const keys = Object.keys(items);
    if (keys.length > MAX_ITEMS_PER_TAB) {
        keys.sort((a, b) => items[a].ts - items[b].ts);
        for (let i = 0; i < keys.length - MAX_ITEMS_PER_TAB; i++) delete items[keys[i]];
    }
}

function markDirty(tabId) {
    DIRTY_TABS.add(tabId);
    scheduleFlush();
}

let flushTimer = null;
function scheduleFlush() {
    if (flushTimer) return;
    // 小延迟合并高频写入；即便 SW 立刻被回收，pending 里的数据也只是最后一次丢一点，
    // 远好过整个列表消失。
    flushTimer = setTimeout(() => { flushTimer = null; flushTabs(); }, 300);
}

async function flushTabs() {
    const tabs = Array.from(DIRTY_TABS);
    DIRTY_TABS.clear();
    const patch = {};
    const empty = [];
    for (const tabId of tabs) {
        const items = TAB_ITEMS.get(tabId) || {};
        if (Object.keys(items).length) patch[storeKey(tabId)] = items;
        else empty.push(storeKey(tabId));
    }
    try {
        if (Object.keys(patch).length) await chrome.storage.session.set(patch);
        if (empty.length) await chrome.storage.session.remove(empty);
    } catch (e) {
        // 存储失败不影响嗅探本身，下次写入会再试
    }
}

function ensureTab(tabId) {
    if (TAB_ITEMS.has(tabId)) return Promise.resolve(TAB_ITEMS.get(tabId));
    let pending = HYDRATING.get(tabId);
    if (pending) return pending;
    pending = (async () => {
        let items = {};
        try {
            const key = storeKey(tabId);
            const got = await chrome.storage.session.get(key);
            items = got[key] || {};
        } catch (e) {
            items = {};
        }
        TAB_ITEMS.set(tabId, items);
        prune(tabId);
        return items;
    })();
    HYDRATING.set(tabId, pending);
    return pending;
}

async function dropTab(tabId) {
    TAB_ITEMS.delete(tabId);
    HYDRATING.delete(tabId);
    DIRTY_TABS.delete(tabId);
    try {
        await chrome.storage.session.remove(storeKey(tabId));
    } catch (e) { /* ignore */ }
}

async function addItem(tabId, url, mime, size) {
    url = SniffRules.normalizeUrl(url);
    if (!/^https?:\/\//i.test(url)) return;
    const items = await ensureTab(tabId);
    const seg = isSegment(url);
    if (seg && hasManifest(items)) return;      // 有清单就只要清单
    const manifest = isManifest(url);
    if (manifest) dropSegments(tabId, items);   // 清单来得晚：把先到的分片清掉
    const existed = !!items[url];
    items[url] = { url: url, mime: mime || "", size: size || 0, ts: Date.now() };
    if (seg) items[url].seg = true;
    prune(tabId);
    markDirty(tabId);
    if (!existed) {
        try {
            chrome.tabs.sendMessage(tabId, { type: "net_items", items: [items[url]] },
                                    { frameId: 0 }, () => void chrome.runtime.lastError);
        } catch (e) { /* 页面可能还没注入 content script */ }
    }
}

chrome.webRequest.onHeadersReceived.addListener((details) => {
    if (details.tabId < 0) return;
    const { url, statusCode } = details;
    if (statusCode < 200 || statusCode >= 400) return;
    let mime = "", size = 0;
    for (const h of (details.responseHeaders || [])) {
        const n = (h.name || "").toLowerCase();
        if (n === "content-type") mime = h.value || "";
        else if (n === "content-length") size = parseInt(h.value, 10) || 0;
    }
    if (!SniffRules.accept(url, mime, size)) return;
    addItem(details.tabId, url, mime, size).catch(() => void chrome.runtime.lastError);
}, { urls: ["<all_urls>"] });

chrome.tabs.onRemoved.addListener((tabId) => { dropTab(tabId); });

// 切换/刷新页面时清空该标签的条目与角标
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
    if (changeInfo.status !== "loading") return;
    dropTab(tabId);
    chrome.action.setBadgeText({ tabId: tabId, text: "" }, () => void chrome.runtime.lastError);
});

// ---------------- 本机服务端口发现 ----------------

// 最近一次「端口有响应但把我们拒了」的状态码：用来区分「没起服务」和「服务不认这个扩展」
let lastReject = 0;

async function findPort() {
    lastReject = 0;
    try {
        const st = await chrome.storage.local.get("server_port");
        if (st.server_port && await ping(st.server_port)) return st.server_port;
    } catch (e) { /* ignore */ }
    for (const p of PORT_RANGE) {
        if (await ping(p)) {
            chrome.storage.local.set({ server_port: p });
            return p;
        }
    }
    return null;
}

async function ping(port) {
    try {
        const r = await fetch(`http://127.0.0.1:${port}/api/status`, { method: "GET" });
        if (!r.ok) {
            // 403 = 服务在跑，但 Origin 里的扩展 ID 不在服务端白名单里
            lastReject = r.status;
            return false;
        }
        const data = await r.json();
        // 新版服务返回 app 标识；老版本只有 busy，仍然认它（升级过渡期不至于失联）
        if (data.app !== undefined) return data.app === APP_ID;
        return Object.prototype.hasOwnProperty.call(data, "busy");
    } catch (e) {
        return false;
    }
}

function serviceMissingHint() {
    if (lastReject === 403) {
        return "本机服务在跑，但拒绝了本扩展（HTTP 403）：扩展 ID 与服务端白名单不一致。"
            + "请到扩展页确认加载的是本目录的 sniffer_extension，并点「重新加载」，然后刷新页面。";
    }
    return "找不到 yt-dlp 服务：请先开着桌面版（YtDlpGUI\\启动.bat，右上角应显示 127.0.0.1:8765），"
        + "或运行 YtDlpWeb\\启动.bat。";
}

async function serverPost(path, body) {
    const port = await findPort();
    if (!port) return { ok: false, error: serviceMissingHint() };
    try {
        const r = await fetch(`http://127.0.0.1:${port}${path}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) {
            // 透传 busy/running_kind，前端据此显示「停止任务」入口
            return {
                ok: false,
                error: data.error || `HTTP ${r.status}`,
                busy: !!data.busy,
                running_kind: data.running_kind || "",
            };
        }
        return data;
    } catch (e) {
        // 端口记录可能过期，清掉让下次重新发现
        chrome.storage.local.remove("server_port");
        return { ok: false, error: "无法连接 yt-dlp 服务：" + (e && e.message) };
    }
}

async function serverGet(path) {
    const port = await findPort();
    if (!port) return null;
    try {
        const r = await fetch(`http://127.0.0.1:${port}${path}`);
        return await r.json();
    } catch (e) {
        return null;
    }
}


// ---------------- 预览：缩略图 / 短片段 / 直播判断 ----------------
// 页面是 https 时，<img src="http://127.0.0.1/..."> 会被当成混合内容拦掉；
// content script 自己 fetch 又受页面 CORS 限制。所以统一由这里取回字节再转
// data URL 交给页面——扩展后台有 host_permissions，不受这两条限制。

async function serverBase() {
    const port = await findPort();
    return port ? `http://127.0.0.1:${port}` : null;
}

function toBase64(bytes) {
    // 分块拼接：一次 apply 几十万参数会爆调用栈
    let bin = "";
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return btoa(bin);
}

async function serverFetchDataUrl(path) {
    const base = await serverBase();
    if (!base) return "";
    try {
        const r = await fetch(base + path);
        if (!r.ok) return "";
        const ctype = (r.headers.get("Content-Type") || "application/octet-stream").split(";")[0];
        const buf = new Uint8Array(await r.arrayBuffer());
        if (!buf.length) return "";
        return "data:" + ctype + ";base64," + toBase64(buf);
    } catch (e) {
        return "";
    }
}

// 把当前标签页的嗅探结果报给本机服务：网页版那边才看得见，服务端也才允许预览
async function reportItems(tabId, pageUrl, title) {
    const items = await ensureTab(tabId);
    const list = Object.values(items).slice(0, 200).map((it) => ({
        url: it.url,
        kind: it.mime || "",
        mime: it.mime || "",
        cat: SniffRules.classify(it.url, it.mime),
        entry: "extension",
        size: it.size || 0,
        source: "webRequest",
        page_url: pageUrl || "",
        referer: pageUrl || "",
        ua: navigator.userAgent,
        title: title || "",
    }));
    if (!list.length) return { ok: true, count: 0 };
    return serverPost("/api/sniff/items", { items: list, page_url: pageUrl || "" });
}

async function previewFor(url) {
    const info = await serverPost("/api/sniff/preview", { url: url });
    if (!info || !info.ok) {
        return { ok: false, error: (info && info.error) || "服务没有响应" };
    }
    const out = {
        ok: true, url: url,
        native: !!info.native,
        duration: info.duration || 0,
        thumb: "",
        clip: "",
    };
    if (info.thumb) {
        out.thumb = await serverFetchDataUrl("/api/sniff/media?id=" + encodeURIComponent(info.thumb));
    }
    if (info.clip) {
        out.clip = await serverFetchDataUrl("/api/sniff/media?id=" + encodeURIComponent(info.clip));
    }
    if (!out.thumb && !out.clip && !out.native) {
        return { ok: false, error: "抓不到画面：可能需要登录，或是加密流（DRM）" };
    }
    return out;
}

// ---------------- 提交下载 ----------------

async function cookieJarFor(url) {
    // 送完整字段，服务端会写成 Netscape cookies.txt（比请求头更规范）
    try {
        const list = await chrome.cookies.getAll({ url: url });
        return list.map((c) => ({
            domain: c.domain, name: c.name, value: c.value,
            path: c.path, secure: !!c.secure,
            expirationDate: c.expirationDate,
        }));
    } catch (e) {
        return [];
    }
}

// 提交成功后的面板文案：服务端排队时回 queue_position（1 起；0 = 立即开始），
// 旧版服务没有该字段，沿用原提示。task_id 原样透传备用。
function submitStatusMessage(resp) {
    const pos = resp && resp.queue_position;
    return pos ? `已加入队列，第 ${pos} 位` : "已交给 yt-dlp，开始下载…";
}

async function submit(msg) {
    const url = String(msg.url || "").trim();
    const kind = msg.kind === "page" ? "page" : "media";
    const body = {
        url: url,
        kind: kind,
        title: String(msg.title || "").slice(0, 200),
        referer: msg.referer || (kind === "page" ? url : ""),
        ua: msg.ua || navigator.userAgent,
        cookie_jar: [],
    };
    // 直链和「下载本页」都带当前页 cookies：抖音这类站点 page 提取也需要 ttwid/msToken。
    body.cookie_jar = await cookieJarFor(url);
    if (kind === "media" && !body.referer && msg.page_url) body.referer = msg.page_url;
    return serverPost("/api/sniff", body);
}

// ---------------- 消息入口 ----------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || typeof msg !== "object") return;
    if (msg.type === "submit") {
        submit(msg).then((resp) => {
            if (resp && resp.ok) resp.message = submitStatusMessage(resp);
            sendResponse(resp);
        });
        return true;                                  // 异步回复
    }
    if (msg.type === "sniff_log") {
        serverGet("/api/sniff/log").then((data) => sendResponse(data || { ok: false }));
        return true;
    }
    if (msg.type === "stop_task") {
        serverPost("/api/stop", {}).then(sendResponse);
        return true;
    }
    if (msg.type === "report_items") {
        const tid = sender.tab && sender.tab.id;
        if (tid == null) { sendResponse({ ok: false }); return false; }
        reportItems(tid, msg.page_url || "", msg.title || "").then(sendResponse);
        return true;
    }
    if (msg.type === "preview") {
        previewFor(String(msg.url || "")).then(sendResponse);
        return true;
    }
    if (msg.type === "query_items") {
        const tabId = sender.tab && sender.tab.id;
        if (tabId == null) {
            sendResponse({ items: [] });
            return false;
        }
        // iframe 里的元素发现也汇总给顶层框架
        if (msg.items && msg.items.length) {
            try {
                chrome.tabs.sendMessage(tabId, { type: "frame_items", items: msg.items },
                                        { frameId: 0 }, () => void chrome.runtime.lastError);
            } catch (e) { /* ignore */ }
        }
        ensureTab(tabId).then(() => {
            sendResponse({ items: Object.values(TAB_ITEMS.get(tabId) || {}) });
        });
        return true;                                  // 等 storage.session 读完再回复
    }
    if (msg.type === "sync_count") {
        const tabId = sender.tab && sender.tab.id;
        if (tabId != null) {
            try {
                chrome.action.setBadgeBackgroundColor({ tabId: tabId, color: "#2da44e" });
                chrome.action.setBadgeText({ tabId: tabId, text: msg.n ? String(msg.n) : "" },
                                           () => void chrome.runtime.lastError);
            } catch (e) { /* ignore */ }
        }
        return false;
    }
    return false;
});

// ---------------- 右键菜单 ----------------

chrome.runtime.onInstalled.addListener(() => {
    chrome.contextMenus.removeAll(() => {
        chrome.contextMenus.create({
            id: "ytdlp-link",
            title: "用 yt-dlp 下载此链接",
            contexts: ["link"],
        });
        chrome.contextMenus.create({
            id: "ytdlp-page",
            title: "用 yt-dlp 下载本页视频",
            contexts: ["page", "video"],
        });
    });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
    if (!tab || !tab.id) return;
    if (info.menuItemId === "ytdlp-link") {
        submit({
            url: info.linkUrl,
            kind: "media",
            title: tab.title || "",
            page_url: tab.url || "",
            referer: tab.url || "",
            ua: navigator.userAgent,
        }).then(() => {});
    } else if (info.menuItemId === "ytdlp-page") {
        submit({
            url: tab.url,
            kind: "page",
            title: tab.title || "",
        }).then(() => {});
    }
});

// ---------------- 工具栏图标：唤出/收起页面上的嗅探面板 ----------------

chrome.action.onClicked.addListener((tab) => {
    if (!tab || !tab.id) return;
    try {
        chrome.tabs.sendMessage(tab.id, { type: "toggle_panel" }, { frameId: 0 },
                                () => void chrome.runtime.lastError);
    } catch (e) { /* ignore */ }
});
