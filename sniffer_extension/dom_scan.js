// dom_scan.js —— 页面媒体元素扫描（扩展 content.js 与桌面内嵌浏览器共用）
//
// 判定只走 sniff_rules.js（normalizeUrl / classify），这里不写第二份正则。
// 返回 [{url, kind, w, h, title}]：按 URL 去重、最多 300 条；无副作用，不碰 DOM。
// 图片过滤也在这里（两侧共用同一阈值）：宽高都 ≥ 200 才收，量不到尺寸的不收。
(function (root) {
    "use strict";

    const MAX_ITEMS = 300;
    const MIN_IMAGE_SIDE = 200;
    const LAZY_ATTRS = ["data-src", "data-original", "data-lazy-src", "data-url"];
    const MEDIA_SELECTOR = "video, audio, source";
    const IMAGE_SELECTOR = "img, picture source";

    function tagOf(el) {
        return String((el && el.tagName) || "").toUpperCase();
    }

    function attr(el, name) {
        try {
            return el && el.getAttribute ? String(el.getAttribute(name) || "") : "";
        } catch (e) {
            return "";
        }
    }

    function num(v) {
        const n = Number(v);
        return isFinite(n) && n > 0 ? n : 0;
    }

    // srcset：挑描述符（w 或 x）最大的一个；都没描述符时取第一个
    function fromSrcset(value) {
        let best = "", bestScore = 0;
        for (const part of String(value || "").split(",")) {
            const bits = part.trim().split(/\s+/);
            const url = bits[0] || "";
            if (!url) continue;
            const score = num(parseFloat(bits[1]));
            if (!best || score > bestScore) {
                best = url;
                bestScore = score;
            }
        }
        return best;
    }

    // 懒加载地址：先看元素自己，<source> 再看最近的 <picture>
    function lazyUrl(el) {
        for (const name of LAZY_ATTRS) {
            const v = attr(el, name);
            if (v) return v;
        }
        if (tagOf(el) !== "SOURCE") return "";
        let pic = null;
        try {
            pic = el.parentElement || null;
        } catch (e) {
            pic = null;
        }
        if (!pic || tagOf(pic) !== "PICTURE") return "";
        for (const name of LAZY_ATTRS) {
            const v = attr(pic, name);
            if (v) return v;
        }
        return "";
    }

    function candidateUrl(el) {
        // currentSrc 是浏览器实际选中的候选；其次 srcset 里最大的一项（懒加载页常把
        // 占位图放在 src），再退到 src，最后试懒加载属性。
        const current = String((el && el.currentSrc) || "");
        if (current) return current;
        const picked = fromSrcset(el && el.srcset);
        if (picked) return picked;
        const direct = String((el && el.src) || "");
        return direct || lazyUrl(el);
    }

    // <source> 在 <video>/<audio> 里是媒体，在 <picture> 里是图片（两个选择器都会选中它）
    function inPicture(el) {
        if (tagOf(el) !== "SOURCE") return false;
        try {
            return tagOf(el.parentElement) === "PICTURE";
        } catch (e) {
            return false;
        }
    }

    function scan(doc) {
        const R = root.SniffRules;
        const out = [], seen = new Set();
        if (!R || !doc || typeof doc.querySelectorAll !== "function") return out;

        function add(el, kindOf) {
            if (out.length >= MAX_ITEMS) return;
            const url = R.normalizeUrl(candidateUrl(el));
            if (!/^https?:\/\//i.test(url)) return;      // blob: / data: 交给页面自己
            if (seen.has(url)) return;
            const kind = kindOf(url);
            const w = num(el.naturalWidth) || num(el.width);
            const h = num(el.naturalHeight) || num(el.height);
            if (kind === "image" && (w < MIN_IMAGE_SIDE || h < MIN_IMAGE_SIDE)) return;
            seen.add(url);
            out.push({
                url: url,
                kind: kind,
                w: kind === "image" ? w : 0,
                h: kind === "image" ? h : 0,
                title: attr(el, "title") || attr(el, "alt"),
            });
        }

        for (const el of doc.querySelectorAll(MEDIA_SELECTOR)) {
            if (inPicture(el)) continue;
            add(el, (url) => (tagOf(el) === "AUDIO" || R.classify(url, "") === "audio")
                ? "audio" : "video");
        }
        for (const el of doc.querySelectorAll(IMAGE_SELECTOR)) {
            add(el, () => "image");
        }
        return out;
    }

    root.SniffDom = { scan: scan };
})(typeof self !== "undefined" ? self : globalThis);