// content.js —— 运行在隔离世界（world: ISOLATED）
// 职责：
//   1) 接收页面世界钩子抛出的 CustomEvent（injected.js）
//   2) 接收后台 webRequest 嗅探到的网络条目（net_items）
//   3) 扫描页面 <video>/<audio> 元素
//   4) 顶层框架渲染悬浮条（Shadow DOM 隔离，防站点 CSS 干扰），
//      点「下载」把地址交给后台 → 本机 yt-dlp 服务
"use strict";

(function () {
    // 只跳过 yt-dlp 自己的 Web 界面，其余本地页面照常显示
    function isOwnWebUI() {
        var h = location.hostname;
        var loop = /^(localhost|127(\.\d+){3}|\[::1\])$/i.test(h);
        if (!loop) return false;
        var p = parseInt(location.port, 10) || 80;
        return p >= 8765 && p <= 8775;
    }
    if (isOwnWebUI()) return;

    const MAX_ITEMS = 60;
    const items = {};          // url -> {url, kind, size, source}
    const HOST_ID = "ytdlp-sniff-host";
    let host = null, root = null, panel = null, listEl = null, statusEl = null, badge = null, polling = null;

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
        if (/\.m3u8(\?|#|$)/i.test(url)) return "m3u8";
        if (/\.mpd(\?|#|$)/i.test(url)) return "mpd";
        if (/\.(mp3|m4a|aac|flac|wav|ogg|opus)(\?|#|$)/i.test(url)) return "audio";
        return "video";
    }

    function addItem(url, kind, size, source) {
        if (!isHttpUrl(url)) return;
        if (items[url]) {
            if (size && !items[url].size) items[url].size = size;
            return;
        }
        items[url] = {
            url: url,
            kind: kind || guessKind(url),
            size: size || 0,
            source: source || "",
            ts: Date.now(),
        };
        const keys = Object.keys(items);
        if (keys.length > MAX_ITEMS) {
            keys.sort((a, b) => items[a].ts - items[b].ts);
            for (let i = 0; i < keys.length - MAX_ITEMS; i++) delete items[keys[i]];
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
        if (!isHttpUrl(d.url)) return;
        if (netSeen.has(d.url)) { addItem(d.url, d.kind, d.size, "页面请求"); return; }
        // 钩子可能比后台的 webRequest 回传更早，给网络侧 4 秒追上来的机会
        if (pendingMain.length < 200) pendingMain.push({ url: d.url, kind: d.kind, size: d.size, ts: Date.now() });
        if (pendingMain.length === 1) setTimeout(flushPendingMain, 600);
    });

    // ---------------- 来源 2：后台网络嗅探 ----------------
    chrome.runtime.onMessage.addListener((msg) => {
        if (!msg || typeof msg !== "object") return;
        if (msg.type === "net_items" || msg.type === "frame_items") {
            for (const it of (msg.items || [])) {
                if (isHttpUrl(it.url)) netSeen.add(it.url);
                addItem(it.url, guessKind(it.url), it.size || 0,
                        msg.type === "net_items" ? "网络响应" : "iframe");
            }
        } else if (msg.type === "toggle_panel") {
            togglePanel(true);
        }
    });

    // ---------------- 来源 3：页面媒体元素 ----------------
    function scanElements() {
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
        #head { display: flex; justify-content: space-between; align-items: center; margin: 2px 4px 6px; }
        #head b { font-size: 12px; font-weight: 600; }
        #pageBtn { border: 1px solid #d0d7de; background: #f6f8fa; border-radius: 6px;
                   padding: 3px 10px; cursor: pointer; font-size: 12px; color: #1f2328; }
        #stopBtn { display: none; border: 1px solid #cf222e; background: #ffebe9; color: #cf222e;
                   border-radius: 6px; padding: 3px 10px; cursor: pointer; font-size: 12px; }
        .row { display: flex; align-items: center; gap: 8px; padding: 4px; border-bottom: 1px solid #f0f0f0; }
        .tag { flex: none; background: #ddf4ff; color: #0969da; border-radius: 4px;
               padding: 0 6px; font-size: 11px; min-width: 34px; text-align: center; }
        .label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #1f2328; }
        .dl { flex: none; border: 1px solid #1f883d; background: #2da44e; color: #fff;
              border-radius: 6px; padding: 2px 10px; cursor: pointer; font-size: 12px; }
        #empty { color: #57606a; padding: 10px; text-align: center; }
        #status { margin: 6px 4px 2px; color: #57606a; font-size: 11px; min-height: 15px; }
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

        listEl = document.createElement("div");
        listEl.id = "list";
        panel.appendChild(listEl);

        statusEl = document.createElement("div");
        statusEl.id = "status";
        panel.appendChild(statusEl);

        wrap.appendChild(panel);
        root.appendChild(wrap);
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

    function render() {
        if (!panel) return;
        dirty = false;
        const keys = Object.keys(items).sort((a, b) => (items[b].ts || 0) - (items[a].ts || 0));
        if (badge) badge.textContent = String(keys.length);
        lastCount = keys.length;
        syncCount(true);
        listEl.textContent = "";
        for (const k of keys) {
            const it = items[k];
            const row = document.createElement("div");
            row.className = "row";

            const kindTag = document.createElement("span");
            kindTag.className = "tag";
            kindTag.textContent = it.kind.toUpperCase();
            row.appendChild(kindTag);

            const label = document.createElement("span");
            label.className = "label";
            label.textContent = shortUrl(it.url) + (it.size ? "  (" + fmtSize(it.size) + ")" : "");
            label.title = it.url + (it.source ? "\n来源：" + it.source : "");
            row.appendChild(label);

            const btn = document.createElement("button");
            btn.className = "dl";
            btn.textContent = "下载";
            btn.addEventListener("click", () => doSubmit(it.url, "media"));
            row.appendChild(btn);

            listEl.appendChild(row);
        }
        if (!keys.length) {
            const empty = document.createElement("div");
            empty.id = "empty";
            empty.textContent = "暂未发现媒体——播放一下视频试试";
            listEl.appendChild(empty);
        }
    }

    // ---------------- 提交与进度轮询 ----------------
    let lastKind = "media";
    let userStopped = false;
    let ownTask = false;      // 服务端当前任务是否由本次点击发起

    function doSubmit(url, kind) {
        lastKind = kind;
        userStopped = false;
        ownTask = false;
        setStatus("提交中…", "#0969da");
        chrome.runtime.sendMessage({
            type: "submit",
            url: url,
            kind: kind,
            title: document.title || "",
            referer: location.href,
            ua: navigator.userAgent,
            page_url: location.href,
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
            setStatus("已交给 yt-dlp，开始下载…", "#1a7f37");
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
                    setStatus("服务端正在执行其它任务（" + (data.running_kind || "未知") + "），可点「停止任务」或等它结束", "#9a6700");
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
        // iframe 里的媒体元素交给后台转发给顶层
        const push = () => {
            if (document.hidden) return;
            if (!document.querySelector("video, audio, source")) return;   // 没有媒体元素就别白扫
            const found = [];
            document.querySelectorAll("video, audio, source").forEach((el) => {
                const src = el.currentSrc || el.src;
                if (src && isHttpUrl(src)) found.push({ url: src, size: 0 });
            });
            if (found.length) {
                chrome.runtime.sendMessage({ type: "query_items", items: found }, () => void chrome.runtime.lastError);
            }
        };
        window.addEventListener("load", push);
        setInterval(push, 8000);
    }
})();
