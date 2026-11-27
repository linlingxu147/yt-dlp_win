// injected.js —— 运行在页面真实 JS 世界（world: MAIN）
// 原理借鉴 FileCentipede 的 content_injected.js：
//   页面自己发起的请求（XHR / fetch / JSON.parse 的接口返回）里
//   藏着 m3u8 / MPD / 直接媒体地址，靠钩子把它们截下来，
//   再用 CustomEvent 抛给隔离世界的内容脚本（content.js）。
//
// 三路互补，任何一路漏了另两路还能兜：
//   1) 请求钩子：XHR / fetch / JSON.parse（请求进行中就拿到）；
//   2) 资源时序表：performance.getEntriesByType('resource') 定时补扫，
//      能捡回 document_start 之前、或绕过钩子发出的请求；
//   3) WebSocket：不少站点把清单地址经 WS 下发，钩 message 事件。
//
// 性能约定（这段代码跑在每一个页面、每一个 iframe 里，必须便宜）：
//   * 先做「廉价的字符串特征判断」，再决定是否真的解析/遍历，避免为主流
//     页面的普通 JSON 白白付出一次解析 + 一次深度遍历的代价；
//   * 能拿到 content-length 时先看体积，超限直接放弃，不去 clone 响应；
//   * JSON.parse 钩子直接复用页面已经解析出来的对象，不再自己 parse 一遍；
//   * 遍历有节点预算，防止超大接口体把主线程拖住；
//   * 时序表扫描按条数封顶、按 URL 去重、有限次数，不常驻轮询。
(function () {
    "use strict";

    // 每次加载一个随机 nonce：MAIN world 和页面共享同一个 JS 环境，页面脚本
    // 自己也能 dispatch 同名事件，带上 nonce 后至少不能「照抄一次就能一直伪造」。
    // 真正的兜底在 content.js 那边（必须同时被后台网络嗅探看到）。
    var TOKEN = "";
    try {
        var buf = new Uint8Array(16);
        crypto.getRandomValues(buf);
        TOKEN = Array.prototype.map.call(buf, function (b) { return ("0" + b.toString(16)).slice(-2); }).join("");
    } catch (e) { TOKEN = String(Math.random()).slice(2) + String(Date.now()); }

    var send = function (detail) {
        try {
            detail.token = TOKEN;
            window.dispatchEvent(new CustomEvent("ytdlp-sniff", { detail: detail }));
        } catch (e) { /* ignore */ }
    };

    try {
        window.dispatchEvent(new CustomEvent("ytdlp-sniff-token", { detail: { token: TOKEN } }));
    } catch (e) { /* ignore */ }

    var MAX_BODY = 8 * 1024 * 1024;      // 超过 8MB 的响应不再读正文
    var MAX_HTML_BODY = 256 * 1024;      // HTML 只在小体积时扫描（内联脚本里的播放地址）
    var MAX_SCAN_NODES = 4000;           // 单次 JSON 遍历的节点上限
    var MAX_FOUND = 8;

    // URL 后缀特征：命中的地址才值得细看
    var URL_HINT_RE = /\.(m3u8|mpd|mp4|flv|ts|m4s|f4v|m2ts|webm|mkv|mov|avi|wmv|mpg|mpeg|mp3|m4a|aac|flac|wav|ogg|opus)([?#]|$)/i;
    // 时序表补扫用的特征：故意不含 .ts/.m4s 分片，否则一个 HLS 页面就会刷出几百条
    var PERF_HINT_RE = /\.(m3u8|mpd|mp4|flv|webm|mkv|mov|mp3|m4a|aac|flac|ogg|opus)([?#]|$)/i;
    // 正文里是否可能藏着播放地址（JSON 里带转义斜杠，所以也要认 \" 结尾）
    var MEDIA_HINT_RE = /\.(m3u8|mpd|mp4|flv|m4s|f4v|m2ts|webm|mkv|mov|ts|mp3|m4a|aac|flac|ogg|opus)([?#"'\\]|$)|#EXTM3U|<MPD/i;
    var TEXTISH_CT_RE = /mpegurl|dash\+xml|json|text\/|javascript/i;

    function isM3U8(t) {
        if (typeof t !== "string" || t.length < 7) return false;
        var head = t.substring(0, 64).toUpperCase();
        return head.indexOf("#EXTM3U") === 0 ||
               t.indexOf("data:application/vnd.apple.mpegurl") === 0;
    }

    function isMPD(t) {
        if (typeof t !== "string" || t.length < 5) return false;
        var head = t.substring(0, 256).toUpperCase();
        return head.indexOf("<MPD") !== -1;
    }

    function headerInt(xhr, name) {
        try { return parseInt(xhr.getResponseHeader(name) || "0", 10) || 0; } catch (e) { return 0; }
    }

    // ---------------- 1) XMLHttpRequest ----------------
    var openOrig = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) {
        var self = this;
        self.__ytdlpUrl = String(url || "");
        // 同一个 XHR 对象常被复用（open 多次）：监听器只挂一次，避免重复读取同一个响应
        if (!self.__ytdlpHooked) {
            self.__ytdlpHooked = true;
            this.addEventListener("load", function () {
                try {
                    var rt = self.responseType;
                    // json 是「页面已经解析好」的形态：很多播放接口就是 responseType="json"，
                    // 以前在这里直接 return，等于整条漏掉
                    if (rt !== "" && rt !== "text" && rt !== "json") return;
                    // 读 responseText 会强制把正文实体化成字符串，所以先用响应头挡一层：
                    // 既不是文本/清单类，地址也不像媒体的（大文件、二进制流）直接跳过。
                    var ct = "";
                    try { ct = self.getResponseHeader("content-type") || ""; } catch (e) { /* ignore */ }
                    var reqUrl = self.__ytdlpUrl || "";
                    var urlHint = URL_HINT_RE.test(reqUrl.split("#")[0]);
                    if (ct && !TEXTISH_CT_RE.test(ct) && !urlHint &&
                        !URL_HINT_RE.test(self.responseURL || "")) return;
                    var size = headerInt(self, "content-length");
                    if (size > MAX_BODY) return;
                    if (rt === "json") {
                        if (self.response) scanJsonObj(self.response);
                        return;
                    }
                    var t = self.responseText || "";
                    if (t.length < 8 || t.length > MAX_BODY) return;
                    if (isM3U8(t)) {
                        send({ kind: "m3u8", url: self.responseURL || reqUrl, size: t.length });
                    } else if (isMPD(t)) {
                        send({ kind: "mpd", url: self.responseURL || reqUrl, size: t.length });
                    }
                } catch (e) { /* ignore */ }
            });
        }
        return openOrig.apply(this, arguments);
    };
    try {
        XMLHttpRequest.prototype.open.toString = function () { return openOrig.toString(); };
    } catch (e) { /* ignore */ }

    // ---------------- 2) fetch ----------------
    if (window.fetch) {
        var fetchOrig = window.fetch;
        window.fetch = function (input) {
            var resp = fetchOrig.apply(this, arguments);
            resp.then(function (r) {
                try {
                    var url = typeof input === "string" ? input : (input && input.url) || "";
                    var ct = (r.headers && r.headers.get("content-type")) || "";
                    var isManifest = /mpegurl|dash\+xml/i.test(ct);
                    var isHtml = /text\/html/i.test(ct);
                    // 只对文本类/清单类响应读正文，避免误吞二进制视频流
                    if (!isManifest && !isHtml && !/json|text\/|javascript/i.test(ct)) return;
                    var len = parseInt((r.headers && r.headers.get("content-length")) || "0", 10) || 0;
                    // 大 HTML 页面正文几乎没有播放地址，读一遍纯属浪费
                    if (len > (isHtml && !isManifest ? MAX_HTML_BODY : MAX_BODY)) return;
                    r.clone().text().then(function (t) {
                        if (!t || t.length <= 5 || t.length >= MAX_BODY) return;
                        if (isM3U8(t)) {
                            send({ kind: "m3u8", url: r.url || url, size: t.length });
                        } else if (isMPD(t)) {
                            send({ kind: "mpd", url: r.url || url, size: t.length });
                        } else if (MEDIA_HINT_RE.test(t)) {
                            // 只有正文里确实出现媒体地址的样子，才值得解析
                            scanJsonText(t);
                        }
                    }).catch(function () { /* ignore */ });
                } catch (e) { /* ignore */ }
                return r;
            }).catch(function (err) { throw err; });
            return resp;
        };
        try {
            window.fetch.toString = function () { return fetchOrig.toString(); };
        } catch (e) { /* ignore */ }
    }

    // ---------------- 3) JSON.parse（接口返回挖矿） ----------------
    // 苹果CMS 这类站点的真实 m3u8 只在播放入口接口里给出，所以这个钩子必须保留。
    var parseOrig = JSON.parse;
    JSON.parse = function (text) {
        var obj = parseOrig.apply(this, arguments);
        try {
            // 页面已经解析过一次了：这里直接遍历结果，绝不重复 parse。
            // 先用正则确认正文里像是有媒体地址，绝大多数接口在这一步就被放过。
            if (typeof text === "string" && text.length > 16 && text.length < 4 * 1024 * 1024 &&
                MEDIA_HINT_RE.test(text)) {
                scanJsonObj(obj);
            }
        } catch (e) { /* ignore */ }
        return obj;
    };
    try {
        JSON.parse.toString = function () { return parseOrig.toString(); };
    } catch (e) { /* ignore */ }

    function scanJsonText(text) {
        try {
            scanJsonObj(parseOrig(text));
        } catch (e) { /* ignore */ }
    }

    function scanJsonObj(obj) {
        try {
            var found = [];
            var budget = MAX_SCAN_NODES;
            (function walk(node, depth) {
                if (!node || depth > 6 || found.length >= MAX_FOUND || budget <= 0) return;
                budget--;
                if (Object.prototype.toString.call(node) === "[object Array]") {
                    for (var i = 0; i < node.length; i++) walk(node[i], depth + 1);
                    return;
                }
                if (typeof node === "object") {
                    for (var k in node) {
                        var v = node[k];
                        if (typeof v === "string" && looksMediaUrl(v)) {
                            if (found.indexOf(v) === -1) found.push(v);
                        } else {
                            walk(v, depth + 1);
                        }
                    }
                }
            })(obj, 0);
            for (var j = 0; j < found.length; j++) {
                send({ kind: "json", url: found[j], size: 0 });
            }
        } catch (e) { /* ignore */ }
    }

    function looksMediaUrl(v) {
        return typeof v === "string" && v.length > 8 &&
               (v.indexOf("http://") === 0 || v.indexOf("https://") === 0) &&
               URL_HINT_RE.test(v.split("#")[0].replace(/[?#].*$/, ""));
    }

    // ---------------- 4) WebSocket（不少站点经 WS 下发清单地址） ----------------
    // 不包构造函数（会破坏 instanceof 和静态常量），只给原型上的 addEventListener
    // 挂一个自己的监听：页面用 onmessage 或 addEventListener 都照样会触发 message。
    var WS = window.WebSocket;
    if (WS && WS.prototype && typeof WS.prototype.addEventListener === "function") {
        var wsAdd = WS.prototype.addEventListener;
        WS.prototype.addEventListener = function (type, listener, opts) {
            if (type === "message" && !this.__ytdlpWsHooked) {
                this.__ytdlpWsHooked = true;
                try {
                    wsAdd.call(this, "message", function (ev) {
                        try {
                            var d = ev && ev.data;
                            if (typeof d !== "string" || d.length < 8 || d.length > MAX_BODY) return;
                            scanRawText(d);
                        } catch (e) { /* ignore */ }
                    });
                } catch (e) { /* ignore */ }
            }
            return wsAdd.apply(this, arguments);
        };
        try { WS.prototype.addEventListener.toString = function () { return wsAdd.toString(); }; }
        catch (e) { /* ignore */ }
    }

    // 正文里的绝对媒体地址（JSON 常见转义斜杠，所以先还原再匹配）
    var ABS_MEDIA_RE = /https?:\/\/[^\s"'<>\\)]+?\.(m3u8|mpd|mp4|flv|m4s|f4v|m2ts|webm|mkv|mov|ts|mp3|m4a|aac|flac|ogg|opus)(\?[^\s"'<>\\)]*)?/gi;
    function scanRawText(text) {
        try {
            var t = String(text || "").replace(/\\\//g, "/");
            if (t.length > MAX_BODY) t = t.substring(0, MAX_BODY);
            ABS_MEDIA_RE.lastIndex = 0;
            var n = 0, m;
            while (n < MAX_FOUND && (m = ABS_MEDIA_RE.exec(t)) !== null) {
                var u = m[0];
                if (u.length < 12 || !/^https?:\/\//i.test(u)) continue;
                send({ kind: kindOfUrl(u), url: u, size: 0 });
                n++;
            }
        } catch (e) { /* ignore */ }
    }

    function kindOfUrl(u) {
        if (/\.m3u8([?#]|$)/i.test(u)) return "m3u8";
        if (/\.mpd([?#]|$)/i.test(u)) return "mpd";
        if (/\.(mp3|m4a|aac|flac|ogg|opus)([?#]|$)/i.test(u)) return "audio";
        return "video";
    }

    // ---------------- 5) 资源时序表补扫 ----------------
    // performance 里的条目是浏览器自己记的，包括注入之前、以及绕过上面钩子的请求。
    // 按 URL 去重、只扫清单/整片后缀（不含 .ts/.m4s 分片），且只补扫有限轮次。
    var perfSeen = Object.create(null);
    var perfCount = 0;
    var perfRuns = 0;

    function sweepPerf() {
        try {
            if (!window.performance || typeof performance.getEntriesByType !== "function") return;
            var list = performance.getEntriesByType("resource");
            if (!list || !list.length) return;
            var sent = 0;
            // 从后往前：最新的条目往往就是播放器刚拿到的清单
            for (var i = list.length - 1; i >= 0 && sent < MAX_FOUND; i--) {
                var entry = list[i];
                // 标准 ResourceTiming 是 { name }；个别环境/假实现直接给字符串，也认
                var name = String((entry && (entry.name || entry)) || "");
                if (!name || perfSeen[name]) continue;
                if (perfCount >= 2000) {
                    perfSeen = Object.create(null);
                    perfCount = 0;
                }
                perfSeen[name] = 1;
                perfCount++;
                if (!PERF_HINT_RE.test(name.split("#")[0])) continue;
                send({ kind: kindOfUrl(name), url: name, size: 0 });
                sent++;
            }
        } catch (e) { /* ignore */ }
    }

    function schedulePerf() {
        if (perfRuns >= 15) return;          // 约 1 分钟，之后交给钩子
        perfRuns++;
        sweepPerf();
        setTimeout(schedulePerf, 4000);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", schedulePerf);
    } else {
        schedulePerf();
    }
    window.addEventListener("load", sweepPerf);
})();
