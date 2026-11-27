// content.js —— 运行在隔离世界（world: ISOLATED）
// 职责：
//   1) 接收页面世界钩子抛出的 CustomEvent（injected.js）
//   2) 接收后台 webRequest 嗅探到的网络条目（net_items）
//   3) 扫描页面 <video>/<audio>/<img> 元素（扫描器在 dom_scan.js，与桌面共用）
//   4) 顶层框架渲染悬浮条（Shadow DOM 隔离，防站点 CSS 干扰），
//      点「下载」把地址交给后台 → 本机扩展服务
"use strict";

(function () {
    // 只跳过本机服务端点，其余本地页面照常显示
    function isOwnWebUI() {
        var h = location.hostname;
        var loop = /^(localhost|127(\.\d+){3}|\[::1\])$/i.test(h);
        if (!loop) return false;
        var p = parseInt(location.port, 10) || 80;
        return p >= 8765 && p <= 8775;
    }
    if (isOwnWebUI()) return;

    const MAX_ITEMS = 60;
    const MAX_IMAGE_ITEMS = 24;
    const items = {};          // url -> {url, kind, size, source}
    const HOST_ID = "ytdlp-sniff-host";
    let host = null, root = null, panel = null, listEl = null, statusEl = null, badge = null, polling = null;
    let showAudio = false, audioCb = null, audioText = null;
    let showImage = false, imageCb = null, imageText = null;
    let playerEl = null, playerTitle = null;
    const previewCache = {};     // url -> {done, ok, native, duration, thumb, clip, error}
    const thumbQueue = [];
    let thumbBusy = false, reportTimer = null;

    function isTop() { try { return window.top === window; } catch (e) { return false; } }
    function isHttpUrl(u) { return typeof u === "string" && /^https?:\/\//i.test(u); }

    function shortUrl(u) {
        try {
            const p = new URL(u);
            let name = decodeURIComponent(p.pathname.split("/").filter(Boolean).pop() || p.hostname);
            if (name.length > 44) name = name.slice(0, 41) + "…";
            return p.hostname + " / " + name;
        } catch (e) {
            return u.length > 50 ? u.slice(0, 47) + "…" : u;
        }
    }

    function fmtSize(n) {
        n = Number(n) || 0;
        if (n <= 0) return "";
        if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
        if (n < 1024 * 1024 * 1024) return (n / 1048576).toFixed(1) + " MB";
        return (n / 1073741824).toFixed(2) + " GB";
    }

    function guessKind(url) {
        return SniffRules.guessKind(url);
    }

    function addItem(url, kind, size, source) {
        url = SniffRules.normalizeUrl(url);
        if (!isHttpUrl(url)) return;
        if (items[url]) {
            if (size && !items[url].size) items[url].size = size;
            return;
        }
        const k = kind || guessKind(url);
        if (k === "image") {
            const n = Object.keys(items).filter((x) => items[x].image).length;
            if (n >= MAX_IMAGE_ITEMS) return;   // 图片另设小上限，别把视频条目挤掉
        }
        items[url] = {
            url: url,
            kind: k,
            size: size || 0,
            source: source || "",
            audio: isAudioStream(url),
            image: k === "image",
            ts: Date.now(),
        };
        const keys = Object.keys(items);
        if (keys.length > MAX_ITEMS) {
            keys.sort((a, b) => items[a].ts - items[b].ts);
            let excess = keys.length - MAX_ITEMS;
            // 先淘汰隐藏的图片，再按时间淘汰其余
            for (const k of keys) {
                if (excess <= 0) break;
                if (items[k].image && !showImage) { delete items[k]; excess--; }
            }
            for (const k of keys) {
                if (excess <= 0) break;
                if (items[k]) { delete items[k]; excess--; }
            }
        }
        if (isTop()) markDirty();
    }

    // 面板收起时不做整表重建：只更新角标并记脏，展开时再渲染。
    // 页面动辄几十条嗅探结果，而面板大多数时间是关着的。
    let dirty = false;
    let lastCount = -1;

    function isPanelOpen() { return !!(panel && panel.style.display === "block"); }

    function markDirty() {
        dirty = true;
        updateBadge();
        if (isPanelOpen()) render();
        scheduleReport();
    }

    function updateBadge() {
        const n = Object.keys(items).length;
        if (badge) badge.textContent = String(n);
        syncCount(false);
    }

    // ---------------- 来源 1：页面世界钩子（injected.js） ----------------
    // MAIN world 与页面共用同一个 JS 环境，页面脚本也能 dispatch 同名事件往这里
    // 塞任意 URL。所以只收同时满足两条的条目：nonce 对得上（injected.js 每次加载
    // 随机生成），且这个 URL 后台真的在网络层看到过（chrome.webRequest 是页面
    // 碰不到的通道）。伪造者两条都过不了。
    let sniffToken = "";
    const netSeen = new Set();      // 后台网络嗅探实锤见过的 URL
    let pendingMain = [];           // nonce 验证通过、但网络侧还没到的条目

    window.addEventListener("ytdlp-sniff-token", (e) => {
        const t = e && e.detail && e.detail.token;
        if (typeof t === "string" && t) sniffToken = t;
    });

    function flushPendingMain() {
        const keep = [];
        for (const it of pendingMain) {
            if (netSeen.has(it.url)) addItem(it.url, it.kind, it.size, "页面请求");
            else if (Date.now() - it.ts < 4000) keep.push(it);
        }
        pendingMain = keep;
        if (pendingMain.length) setTimeout(flushPendingMain, 600);
    }

    window.addEventListener("ytdlp-sniff", (e) => {
        const d = e.detail || {};
        if (!d || !d.url) return;
        if (!sniffToken || d.token !== sniffToken) return;   // 页面伪造的：没有 nonce
        const url = SniffRules.normalizeUrl(d.url);
        if (!isHttpUrl(url)) return;
        if (netSeen.has(url)) { addItem(url, d.kind, d.size, "页面请求"); return; }
        // 钩子可能比后台的 webRequest 回传更早，给网络侧 4 秒追上来的机会
        if (pendingMain.length < 200) pendingMain.push({ url: url, kind: d.kind, size: d.size, ts: Date.now() });
        if (pendingMain.length === 1) setTimeout(flushPendingMain, 600);
    });

    // ---------------- 来源 2：后台网络嗅探 ----------------
    chrome.runtime.onMessage.addListener((msg) => {
        if (!msg || typeof msg !== "object") return;
        if (msg.type === "net_items" || msg.type === "frame_items") {
            for (const it of (msg.items || [])) {
                const url = SniffRules.normalizeUrl(it.url);
                if (isHttpUrl(url)) {
                    netSeen.add(url);
                    addItem(url, it.kind || guessKind(url), it.size || 0,
                            msg.type === "net_items" ? "网络响应" : "iframe");
                }
            }
        } else if (msg.type === "net_items_gone") {
            let hit = 0;
            for (const u of (msg.urls || [])) {
                if (items[u]) { delete items[u]; hit++; }
            }
            if (hit) render();
        } else if (msg.type === "toggle_panel") {
            togglePanel(true);
        }
    });

    // ---------------- 来源 3：页面媒体元素 ----------------
    function scanElements() {
        const dom = window.SniffDom;
        if (dom && typeof dom.scan === "function") {
            for (const it of dom.scan(document)) addItem(it.url, it.kind, 0, "页面元素");
            return;
        }
        // 旧路径：没有共享扫描器（dom_scan.js 未加载）时只扫 video/audio
        const nodes = document.querySelectorAll("video, audio");
        for (const el of nodes) {
            const src = el.currentSrc || el.src;
            if (src && isHttpUrl(src)) addItem(src, el.tagName === "AUDIO" ? "audio" : "video", 0, "页面元素");
            const sources = el.querySelectorAll("source");
            for (const s of sources) {
                if (s.src && isHttpUrl(s.src)) addItem(s.src, el.tagName === "AUDIO" ? "audio" : "video", 0, "页面元素");
            }
        }
    }

    let scanTimer = null;
    function scheduleScan() {
        if (scanTimer) return;
        scanTimer = setTimeout(() => {
            scanTimer = null;
            ensureMounted();     // 页面若 document.write 重写过，这里自愈重新挂载
            scanElements();
        }, 800);
    }

    // ---------------- 悬浮条（仅顶层框架，Shadow DOM 隔离） ----------------
    const CSS_TEXT = `
        :host { all: initial; }
        * { box-sizing: border-box; font: 12px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
        #wrap { position: relative; font: 12px/1.5 system-ui, sans-serif; }
        #pill {
            display: inline-flex; align-items: center; gap: 6px;
            background: #1f2328; color: #fff; padding: 6px 12px;
            border-radius: 18px; cursor: pointer; user-select: none;
            box-shadow: 0 2px 10px rgba(0,0,0,.35); white-space: nowrap;
        }
        #badge { background: #2da44e; color: #fff; border-radius: 9px; padding: 0 7px;
                 font-size: 11px; min-width: 16px; text-align: center; }
        #panel {
            display: none; position: absolute; right: 0; bottom: 40px;
            width: 420px; max-height: 320px; overflow: auto;
            background: #fff; color: #1f2328; border: 1px solid #d0d7de;
            border-radius: 10px; box-shadow: 0 4px 18px rgba(0,0,0,.25);
            padding: 8px;
        }
        #head { display: flex; flex-wrap: wrap; gap: 4px; justify-content: space-between; align-items: center; margin: 2px 4px 6px; }
        #head b { font-size: 12px; font-weight: 600; }
        #audioToggle, #imageToggle { display: flex; align-items: center; gap: 3px; color: #57606a;
                       font-size: 11px; cursor: pointer; white-space: nowrap; }
        #pageBtn { border: 1px solid #d0d7de; background: #f6f8fa; border-radius: 6px;
                   padding: 3px 10px; cursor: pointer; font-size: 12px; color: #1f2328; }
        #stopBtn { display: none; border: 1px solid #cf222e; background: #ffebe9; color: #cf222e;
                   border-radius: 6px; padding: 3px 10px; cursor: pointer; font-size: 12px; }
        .row { display: flex; align-items: center; gap: 8px; padding: 4px; border-bottom: 1px solid #f0f0f0; }
        .tag { flex: none; background: #ddf4ff; color: #0969da; border-radius: 4px;
               padding: 0 6px; font-size: 11px; min-width: 34px; text-align: center; }
        .tag.audio { background: #fff8c5; color: #9a6700; }
        .tag.image { background: #ffeff7; color: #bf3989; }
        .label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #1f2328; }
        .dl { flex: none; border: 1px solid #1f883d; background: #2da44e; color: #fff;
              border-radius: 6px; padding: 2px 10px; cursor: pointer; font-size: 12px; }
        #empty { color: #57606a; padding: 10px; text-align: center; }
        #status { margin: 6px 4px 2px; color: #57606a; font-size: 11px; min-height: 15px; }
        .thumb { flex: none; width: 42px; height: 24px; border-radius: 4px; background: #eaeef2;
                 color: #8b949e; font-size: 9px; display: flex; align-items: center;
                 justify-content: center; background-size: cover; background-position: center; }
        .meta { flex: none; color: #57606a; font-size: 11px; min-width: 32px; text-align: right; }
        .pv { flex: none; border: 1px solid #d0d7de; background: #f6f8fa; color: #0969da;
              border-radius: 6px; padding: 2px 7px; cursor: pointer; font-size: 11px; }
        .pv[disabled] { opacity: .6; cursor: default; }
        #player { display: none; margin: 4px; padding: 6px; background: #0d1117; border-radius: 8px; }
        #playerHead { display: flex; justify-content: space-between; align-items: center;
                      gap: 6px; margin-bottom: 4px; }
        #playerTitle { color: #c9d1d9; font-size: 11px; overflow: hidden;
                       text-overflow: ellipsis; white-space: nowrap; }
        #playerClose { flex: none; border: 0; background: transparent; color: #8b949e;
                       cursor: pointer; font-size: 12px; }
        #player video { width: 100%; max-height: 220px; background: #000; border-radius: 6px;
                        display: block; }
    `;

    function buildUI() {
        host = document.createElement("div");
        host.id = HOST_ID;
        // all:initial 之后再声明定位，确保不被站点样式覆盖
        host.setAttribute("style",
            "all:initial;position:fixed;right:16px;bottom:16px;z-index:2147483647;display:block;");
        root = host.attachShadow({ mode: "open" });

        const style = document.createElement("style");
        style.textContent = CSS_TEXT;
        root.appendChild(style);

        const wrap = document.createElement("div");
        wrap.id = "wrap";

        const pill = document.createElement("div");
        pill.id = "pill";
        pill.textContent = "▶ 嗅探";
        badge = document.createElement("span");
        badge.id = "badge";
        badge.textContent = "0";
        pill.appendChild(badge);
        pill.addEventListener("click", () => togglePanel());
        wrap.appendChild(pill);

        panel = document.createElement("div");
        panel.id = "panel";

        const head = document.createElement("div");
        head.id = "head";
        const titleEl = document.createElement("b");
        titleEl.textContent = "本页嗅探到的媒体";
        const verEl = document.createElement("span");
        try {
            verEl.textContent = " v" + chrome.runtime.getManifest().version;
        } catch (e) {
            verEl.textContent = "";
        }
        verEl.setAttribute("style", "color:#8b949e;font-weight:400;font-size:11px");
        titleEl.appendChild(verEl);
        head.appendChild(titleEl);
        const audioWrap = document.createElement("label");
        audioWrap.id = "audioToggle";
        audioWrap.title = "默认隐藏音频单轨（如 B 站 30280 m4s）；勾选后显示";
        audioCb = document.createElement("input");
        audioCb.type = "checkbox";
        audioCb.checked = showAudio;
        audioCb.addEventListener("change", () => {
            showAudio = audioCb.checked;
            try { chrome.storage.local.set({ sniff_show_audio: showAudio }); } catch (e) { /* ignore */ }
            render();
        });
        audioText = document.createElement("span");
        audioText.textContent = " 显示音频 (0)";
        audioWrap.appendChild(audioCb);
        audioWrap.appendChild(audioText);
        head.appendChild(audioWrap);
        const imageWrap = document.createElement("label");
        imageWrap.id = "imageToggle";
        imageWrap.title = "默认隐藏页面图片（只收宽高都 ≥ 200 的图）；勾选后显示";
        imageCb = document.createElement("input");
        imageCb.type = "checkbox";
        imageCb.checked = showImage;
        imageCb.addEventListener("change", () => {
            showImage = imageCb.checked;
            try { chrome.storage.local.set({ sniff_show_image: showImage }); } catch (e) { /* ignore */ }
            render();
        });
        imageText = document.createElement("span");
        imageText.textContent = " 显示图片 (0)";
        imageWrap.appendChild(imageCb);
        imageWrap.appendChild(imageText);
        head.appendChild(imageWrap);
        const pageBtn = document.createElement("button");
        pageBtn.id = "pageBtn";
        pageBtn.textContent = "下载本页（yt-dlp）";
        pageBtn.addEventListener("click", () => doSubmit(location.href, "page"));
        head.appendChild(pageBtn);
        const stopBtn = document.createElement("button");
        stopBtn.id = "stopBtn";
        stopBtn.textContent = "停止任务";
        stopBtn.addEventListener("click", () => {
            stopBtn.disabled = true;
            userStopped = true;
            setStatus("已请求停止，等待任务结束…", "#9a6700");
            chrome.runtime.sendMessage({ type: "stop_task" }, (r) => {
                stopBtn.disabled = false;
                if (chrome.runtime.lastError || !r) {
                    setStatus("停止请求发送失败", "#cf222e");
                }
            });
        });
        head.appendChild(stopBtn);
        panel.appendChild(head);

        playerEl = document.createElement("div");
        playerEl.id = "player";
        const playerHead = document.createElement("div");
        playerHead.id = "playerHead";
        playerTitle = document.createElement("span");
        playerTitle.id = "playerTitle";
        playerHead.appendChild(playerTitle);
        const closeBtn = document.createElement("button");
        closeBtn.id = "playerClose";
        closeBtn.textContent = "✕ 关闭";
        closeBtn.addEventListener("click", () => {
            const v = playerEl.querySelector("video");
            try { v.pause(); } catch (e) { /* ignore */ }
            v.removeAttribute("src");
            try { v.load(); } catch (e) { /* ignore */ }
            playerEl.style.display = "none";
        });
        playerHead.appendChild(closeBtn);
        playerEl.appendChild(playerHead);
        const videoEl = document.createElement("video");
        videoEl.setAttribute("controls", "");
        videoEl.setAttribute("playsinline", "");
        videoEl.setAttribute("preload", "metadata");
        playerEl.appendChild(videoEl);
        panel.appendChild(playerEl);

        listEl = document.createElement("div");
        listEl.id = "list";
        panel.appendChild(listEl);

        statusEl = document.createElement("div");
        statusEl.id = "status";
        panel.appendChild(statusEl);

        wrap.appendChild(panel);
        root.appendChild(wrap);
        try {
            chrome.storage.local.get({ sniff_show_audio: false, sniff_show_image: false }, (r) => {
                showAudio = !!(r && r.sniff_show_audio);
                if (audioCb) audioCb.checked = showAudio;
                showImage = !!(r && r.sniff_show_image);
                if (imageCb) imageCb.checked = showImage;
                render();
            });
        } catch (e) { /* ignore */ }
    }

    // 挂载 + 自愈：document.write / SPA 换掉 <html> 时重新挂回去
    function ensureMounted() {
        if (!isTop()) return;
        if (!host) buildUI();
        if (!host.isConnected) {
            // 优先挂到 <html>：避开 body 上 transform/filter 导致 position:fixed 失效、被站点浮层盖住的坑
            const parent = document.documentElement || document.body;
            if (parent) parent.appendChild(host);
        }
    }

    function togglePanel(forceOpen) {
        ensureMounted();
        if (!panel) return;
        const open = forceOpen === true ? true : panel.style.display !== "block";
        panel.style.display = open ? "block" : "none";
        if (open) {
            if (dirty || !listEl.childElementCount) render();
            else updateBadge();
        }
    }

    // 把条数同步给后台，让工具栏图标角标与面板数量一致（Service Worker 首条消息可能丢，靠心跳纠正）
    function syncCount(force) {
        const n = Object.keys(items).length;
        if (!force && n === lastCount) return;   // 数量没变就不打扰后台
        lastCount = n;
        try {
            chrome.runtime.sendMessage({ type: "sync_count", n: n },
                                       () => void chrome.runtime.lastError);
        } catch (e) { /* ignore */ }
    }

    // ---------------- 缩略图 / 试播 / 时长 ----------------
    function fmtDuration(sec) {
        const s = Math.max(0, Math.round(Number(sec) || 0));
        if (!s) return "";
        const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
        const pad = (n) => (n < 10 ? "0" + n : "" + n);
        return h ? h + ":" + pad(m) + ":" + pad(ss) : m + ":" + pad(ss);
    }

    function setThumb(el, dataUrl) {
        el.style.backgroundImage = 'url("' + dataUrl + '")';
        el.textContent = "";
    }

    function refreshMeta(url, dur) {
        if (!listEl) return;
        const rows = listEl.querySelectorAll(".row");
        for (const r of rows) {
            if (r.dataset.url !== url) continue;
            const m = r.querySelector(".meta");
            if (m && !m.textContent) m.textContent = fmtDuration(dur);
        }
    }

    function requestPreview(url) {
        const c = previewCache[url];
        if (c && c.pending) return c.pending;
        if (c && c.done) return Promise.resolve(c);
        const p = new Promise((resolve) => {
            chrome.runtime.sendMessage({ type: "preview", url: url }, (r) => {
                if (chrome.runtime.lastError || !r) {
                    Object.assign(previewCache[url] || (previewCache[url] = {}),
                                  { done: true, ok: false, error: "扩展后台无响应" });
                    resolve(previewCache[url]);
                    return;
                }
                const rec = previewCache[url] || (previewCache[url] = {});
                Object.assign(rec, r, { done: true });
                if (rec.ok && rec.duration) {
                    const it = items[url];
                    if (it) it.dur = rec.duration;
                    refreshMeta(url, rec.duration);
                }
                resolve(rec);
            });
        });
        (previewCache[url] || (previewCache[url] = {})).pending = p;
        return p;
    }

    function observeThumb(el, it) {
        const c = previewCache[it.url];
        if (c && c.thumb) { setThumb(el, c.thumb); return; }
        if (c && c.done && !c.thumb) { el.textContent = "无图"; return; }
        if (!("IntersectionObserver" in window)) { queueThumb(el, it); return; }
        const io = new IntersectionObserver((ents) => {
            for (const e of ents) {
                if (!e.isIntersecting) continue;
                io.disconnect();
                queueThumb(el, it);
            }
        }, { threshold: 0.15 });
        io.observe(el);
    }

    function queueThumb(el, it) {
        const c = previewCache[it.url];
        if (c && c.thumb) { setThumb(el, c.thumb); return; }
        if (c && c.done && !c.thumb) { el.textContent = "无图"; return; }
        thumbQueue.push({ el: el, it: it });
        pumpThumbQueue();
    }

    // 服务端抓流是串行的（一次一个），这里也就一次只发一个，别自己把队列堆爆
    function pumpThumbQueue() {
        if (thumbBusy) return;
        const job = thumbQueue.shift();
        if (!job) return;
        thumbBusy = true;
        requestPreview(job.it.url).then((r) => {
            thumbBusy = false;
            if (r && r.thumb) setThumb(job.el, r.thumb);
            else {
                job.el.textContent = "无图";
                job.el.title = (r && r.error) || "";
            }
            pumpThumbQueue();
        });
    }

    function previewItem(it, btn) {
        btn.disabled = true;
        btn.textContent = "取画面…";
        setStatus("正在问本机要画面（首次约 3-5 秒）…", "#0969da");
        requestPreview(it.url).then((r) => {
            btn.disabled = false;
            btn.textContent = "试播";
            if (!r || !r.ok) {
                setStatus((r && r.error) || "取不到画面", "#cf222e");
                return;
            }
            playUrl(it.url, r);
        });
    }

    function playUrl(url, info) {
        if (!playerEl) return;
        // 浏览器能直接解的容器就走原地址（零等待）；m3u8 之类用刚抓好的一小段
        const src = info.native ? url : (info.clip || "");
        if (!src) {
            setStatus("这个流取不到片段，只能「下载」后用播放器看", "#9a6700");
            return;
        }
        playerTitle.textContent = shortUrl(url) + (info.duration ? "  ·  " + fmtDuration(info.duration) : "");
        playerEl.style.display = "block";
        const v = playerEl.querySelector("video");
        v.onerror = () => {
            setStatus("播放被站点拒绝（防盗链）或格式不支持，可点「下载」交给 yt-dlp", "#9a6700");
        };
        v.src = src;
        try { v.play(); } catch (e) { /* 用户手势之外的自动播放可能被拦，控件仍在 */ }
        if (panel) panel.scrollTop = 0;
    }

    // 嗅探结果同步给本机服务：网页版才能看见，服务端也才允许为这些地址取画面
    function scheduleReport() {
        if (reportTimer) return;
        reportTimer = setTimeout(() => {
            reportTimer = null;
            chrome.runtime.sendMessage({
                type: "report_items",
                page_url: location.href,
                title: document.title || "",
            }, () => void chrome.runtime.lastError);
        }, 2000);
    }

    function render() {
        if (!panel) return;
        dirty = false;
        const keys = Object.keys(items).sort((a, b) => (items[b].ts || 0) - (items[a].ts || 0));
        if (badge) badge.textContent = String(keys.length);
        lastCount = keys.length;
        syncCount(true);
        if (audioText) {
            const n = keys.filter((k) => items[k].audio).length;
            audioText.textContent = " 显示音频 (" + n + ")";
        }
        if (imageText) {
            const n = keys.filter((k) => items[k].image).length;
            imageText.textContent = " 显示图片 (" + n + ")";
        }
        listEl.textContent = "";
        const visible = keys.filter((k) => (showAudio || !items[k].audio)
                                         && (showImage || !items[k].image));
        for (const k of visible) {
            const it = items[k];
            const row = document.createElement("div");
            row.className = "row";
            row.dataset.url = it.url;

            // 缩略图：直链没有封面元数据，只能让本机 ffmpeg 截一帧（服务端缓存，
            // 同一条第二次几乎瞬时）。滚动到可见才取，免得一开面板就排队几十个。
            const thumb = document.createElement("div");
            thumb.className = "thumb";
            if (it.image) {
                thumb.textContent = "图";
                thumb.title = "图片条目";
            } else {
                thumb.textContent = "…";
            }
            row.appendChild(thumb);

            const kindTag = document.createElement("span");
            kindTag.className = "tag";
            if (it.audio) kindTag.classList.add("audio");
            if (it.image) kindTag.classList.add("image");
            kindTag.textContent = it.image
                ? "图片"
                : (it.audio ? "音频" : String(it.kind || "media").toUpperCase().slice(0, 5));
            row.appendChild(kindTag);

            const label = document.createElement("span");
            label.className = "label";
            label.textContent = shortUrl(it.url) + (it.size ? "  (" + fmtSize(it.size) + ")" : "");
            label.title = it.url + (it.source ? "\n来源：" + it.source : "");
            row.appendChild(label);

            const meta = document.createElement("span");
            meta.className = "meta";
            meta.textContent = it.dur ? fmtDuration(it.dur) : "";
            row.appendChild(meta);

            if (!it.image) {
                const pv = document.createElement("button");
                pv.className = "pv";
                pv.textContent = "试播";
                pv.addEventListener("click", () => previewItem(it, pv));
                row.appendChild(pv);
            }

            const btn = document.createElement("button");
            btn.className = "dl";
            btn.textContent = "下载";
            btn.addEventListener("click", () => doSubmit(it.url, "media", it));
            row.appendChild(btn);

            listEl.appendChild(row);
            if (!it.image) observeThumb(thumb, it);
        }
        if (!visible.length) {
            const empty = document.createElement("div");
            empty.id = "empty";
            const hiddenAudio = keys.filter((k) => items[k].audio && !showAudio).length;
            const hiddenImage = keys.filter((k) => items[k].image && !showImage).length;
            if (hiddenAudio && hiddenImage) {
                empty.textContent = "只发现音频单轨和图片（都已隐藏）；勾选「显示音频」或「显示图片」可查看";
            } else if (hiddenAudio) {
                empty.textContent = "只发现音频单轨（已隐藏）；勾选「显示音频」可查看";
            } else if (hiddenImage) {
                empty.textContent = "只发现图片（已隐藏）；勾选「显示图片」可查看";
            } else {
                empty.textContent = "暂未发现媒体——播放一下视频试试";
            }
            listEl.appendChild(empty);
        }
    }

    // ---------------- 提交与进度轮询 ----------------
    let lastKind = "media";
    let userStopped = false;
    let ownTask = false;      // 服务端当前任务是否由本次点击发起

    function sniffMeta(it) {
        // 交给服务端的源类型线索：build_download_cmd 据此决定直链要不要上 aria2c
        const k = String((it && it.kind) || "");
        let cat = k;
        if (k === "m3u8" || k === "mpd") cat = "stream";
        if (it && it.image) cat = "image";
        else if (it && it.audio) cat = "audio";
        return {
            cat: cat,
            seg: !!(it && SniffRules.isSegment(it.url)),
            manifest: k === "m3u8" || k === "mpd",
            size: Number(it && it.size) || 0,
        };
    }

    function doSubmit(url, kind, item) {
        lastKind = kind;
        userStopped = false;
        ownTask = false;
        setStatus("提交中…", "#0969da");
        const meta = kind === "media" ? sniffMeta(item) : {};
        chrome.runtime.sendMessage({
            type: "submit",
            url: url,
            kind: kind,
            title: document.title || "",
            referer: location.href,
            ua: navigator.userAgent,
            page_url: location.href,
            cat: meta.cat || "",
            seg: !!meta.seg,
            manifest: !!meta.manifest,
            size: meta.size || 0,
        }, (resp) => {
            if (chrome.runtime.lastError || !resp) {
                setStatus("发送失败：扩展后台无响应", "#cf222e");
                return;
            }
            if (!resp.ok) {
                if (resp.busy) {
                    // 服务端被别的任务占用：给出停止入口，并继续轮询等它结束
                    setStatus(resp.error || "服务端正忙", "#9a6700");
                    showStop(true);
                    startPolling();
                    return;
                }
                setStatus(resp.error || "提交失败", "#cf222e");
                return;
            }
            ownTask = true;
            // 后台按服务端 queue_position 给出文案（排队时「已加入队列，第 N 位」）
            setStatus(resp.message || "已交给 yt-dlp，开始下载…", "#1a7f37");
            startPolling();
        });
    }

    function setStatus(text, color) {
        if (!statusEl) return;
        statusEl.textContent = text;
        statusEl.style.color = color || "#57606a";
    }

    function showStop(show) {
        const btn = root && root.querySelector("#stopBtn");
        if (btn) btn.style.display = show ? "inline-block" : "none";
    }

    function startPolling() {
        if (polling) clearInterval(polling);
        polling = setInterval(() => {
            chrome.runtime.sendMessage({ type: "sniff_log" }, (data) => {
                if (chrome.runtime.lastError || !data) return;
                const prog = data.prog;
                if (prog && prog.pct != null && !data.done) {
                    showStop(true);
                    let t = "下载中 " + prog.pct.toFixed(1) + "%";
                    if (prog.speed) t += " · " + prog.speed;
                    if (prog.eta) t += " · 剩余 " + prog.eta;
                    if (!ownTask) t = "服务端其它任务进行中 " + prog.pct.toFixed(1) + "%（非本次提交）";
                    setStatus(t, "#0969da");
                } else if (data.done) {
                    showStop(false);
                    if (!ownTask) {
                        // 之前被 409 挡下的场景：别人的任务结束了
                        setStatus("服务端任务已结束，可以重新点「下载」", "#57606a");
                        clearInterval(polling);
                        polling = null;
                        return;
                    }
                    let text, color;
                    if (userStopped) {
                        text = data.code === 0 ? "已停止（文件已保存）" : "已停止（未完成的临时文件可重新下载）";
                        color = "#9a6700";
                        userStopped = false;
                    } else if (data.code === 0) {
                        text = "完成，文件在 downloads 目录";
                        color = "#1a7f37";
                    } else {
                        // 服务端把常见失败翻译成「下一步怎么做」的提示
                        text = data.hint || (data.last_error ? "失败：" + data.last_error : "结束（退出码 " + data.code + "）");
                        if (data.hint && lastKind === "page") {
                            const n = Object.keys(items).length;
                            if (n > 0) text += "（列表里已有 " + n + " 条嗅探结果，可直接点其「下载」）";
                        }
                        color = "#cf222e";
                    }
                    setStatus(text, color);
                    clearInterval(polling);
                    polling = null;
                } else if (ownTask && data.last_text && data.last_text.indexOf("[自动重试]") === 0) {
                    // 服务端正在换姿势重试：把原因和方法直接显示出来
                    showStop(true);
                    setStatus(data.last_text.replace("[自动重试] ", "连接中断，"), "#9a6700");
                } else if (data.busy) {
                    showStop(true);
                    // 忙的若正是自己刚提交的任务（多半还在队列里等），不能用「其它任务」把
                    // 提交时的「已加入队列，第 N 位」冲掉：只亮出停止入口，等进度/结束分支接手。
                    // 自己那份结束时服务端会置 done，走上面的收尾分支。
                    if (!ownTask) {
                        setStatus("服务端正在执行其它任务（" + (data.running_kind || "未知") + "），可点「停止任务」或等它结束", "#9a6700");
                    }
                }
            });
        }, 1500);
    }

    // ---------------- 启动 ----------------
    if (isTop()) {
        ensureMounted();

        document.addEventListener("DOMContentLoaded", () => { ensureMounted(); scheduleScan(); });
        window.addEventListener("load", () => { ensureMounted(); scheduleScan(); });
        // 只关心 DOM 增删（懒加载/SPA 换内容）；面板自身的重建直接跳过，避免自我触发
        const mo = new MutationObserver((records) => {
            for (const rec of records) {
                const t = rec.target;
                if (host && t && (t === host || (t.nodeType === 1 && host.contains(t)))) continue;
                ensureMounted();
                scheduleScan();
                return;
            }
        });
        mo.observe(document.documentElement, { childList: true, subtree: true });

        // 页面转前台时补扫一次（后台标签页不做无谓工作）
        document.addEventListener("visibilitychange", () => { if (!document.hidden) scheduleScan(); });
        setInterval(() => syncCount(true), 10000);   // 心跳：纠正 Service Worker 首条消息丢失

        chrome.runtime.sendMessage({ type: "query_items" }, (resp) => {
            if (chrome.runtime.lastError || !resp) return;
            for (const it of (resp.items || [])) {
                addItem(it.url, guessKind(it.url), it.size || 0, "网络响应");
            }
            markDirty();
        });
    } else {
        // iframe 里的媒体元素交给后台转发给顶层（扫描器与顶层同一份）
        const push = () => {
            if (document.hidden) return;
            const found = [];
            const dom = window.SniffDom;
            if (dom && typeof dom.scan === "function") {
                for (const it of dom.scan(document)) {
                    found.push({ url: it.url, kind: it.kind, size: 0 });
                }
            } else {
                document.querySelectorAll("video, audio, source").forEach((el) => {
                    const src = el.currentSrc || el.src;
                    if (src && isHttpUrl(src)) found.push({ url: src, size: 0 });
                });
            }
            if (found.length) {
                chrome.runtime.sendMessage({ type: "query_items", items: found }, () => void chrome.runtime.lastError);
            }
        };
        window.addEventListener("load", push);
        setInterval(push, 8000);
    }
})();
