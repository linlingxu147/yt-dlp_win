// sniff_rules.js —— 嗅探判定的唯一真源（扩展 + 桌面内嵌浏览器共用）
//
// 扩展侧：background.js 用 importScripts 加载；content.js 由 manifest 先加载。
// 桌面侧：项目根目录的 sniff_rules.py 解析下面 RULE-SOURCES 块里的 JSON 表，
//         用同一组正则做判定（不复制第二份正则）。
//
// 改规则只改 RULE-SOURCES-BEGIN / END 之间；两侧都有测试覆盖同一批样例。
(function (root) {
    "use strict";

    // RULE-SOURCES-BEGIN
    const SOURCES = {
        "mime": "^(video/|audio/|application/(vnd\\.apple\\.mpegurl|x-mpegurl|dash\\+xml|ogg))",
        "nonMedia": "^(image/|text/html|text/css|application/javascript|text/javascript|font/|text/plain)",
        "manifestMime": "mpegurl|dash\\+xml",
        "mediaExt": "\\.(m3u8|mpd|mp4|flv|ts|m4s|f4v|m2ts|webm|mkv|mov|avi|wmv|mpg|mpeg|mp3|m4a|aac|flac|wav|ogg|opus)(\\?|#|$)",
        "manifest": "\\.(m3u8|mpd)(\\?|#|$)",
        "segment": "\\.(ts|m4s|f4v|m2ts)(\\?|#|$)",
        "audioExt": "\\.(m4a|mp3|aac|flac|wav|ogg|opus)$",
        "biliAudio": "[-_]302\\d{2}\\.m4s$",
        "imageExt": "\\.(png|jpe?g|gif|webp|bmp|svg|avif)(\\?|#|$)",
        "minMimeBytes": 204800
    };
    // RULE-SOURCES-END

    const R = {};
    for (const k in SOURCES) {
        if (typeof SOURCES[k] === "string") R[k] = new RegExp(SOURCES[k], "i");
    }

    function noHash(url) {
        return String(url || "").split("#")[0];
    }

    function noQuery(url) {
        return noHash(url).split("?")[0];
    }

    function isManifest(url) {
        return R.manifest.test(noHash(url));
    }

    function isSegment(url) {
        return R.segment.test(noHash(url));
    }

    function isAudio(url) {
        const p = noQuery(url);
        return R.biliAudio.test(p) || R.audioExt.test(p);
    }

    function guessKind(url) {
        const u = noHash(url);
        if (R.manifest.test(u)) return /\.mpd(\?|#|$)/i.test(u) ? "mpd" : "m3u8";
        if (isAudio(url)) return "audio";
        return "video";
    }

    function classify(url, mime) {
        const m = String(mime || "").toLowerCase();
        if (isAudio(url) || m.indexOf("audio/") === 0) return "audio";
        if (isManifest(url) || isSegment(url) || R.manifestMime.test(m)) return "stream";
        if (m.indexOf("video/") === 0) return "video";
        if (m.indexOf("image/") === 0 || R.imageExt.test(noHash(url))) return "image";
        if (R.mediaExt.test(noHash(url))) return "video";
        return "other";
    }

    function accept(url, mime, size) {
        const raw = String(url || "");
        if (!/^https?:\/\//i.test(raw)) return false;
        const u = noHash(raw);
        const m = String(mime || "");
        const n = Number(size) || 0;
        const mimeHit = R.mime.test(m);
        const extHit = R.mediaExt.test(u);
        if (!mimeHit && !extHit) return false;
        // text/html 之类即便后缀命中也多为伪装路由，宁可放过也不刷屏
        if (R.nonMedia.test(m) && !R.manifestMime.test(m) && !extHit) return false;
        // 纯靠 MIME 命中且体积太小的（封面、提示音）忽略
        if (mimeHit && !extHit && n > 0 && n < SOURCES.minMimeBytes) return false;
        return true;
    }

    function normalizeUrl(url) {
        return String(url || "").trim()
            .replace(/[\s\u200b-\u200f\u2028\u2029\ufeff]+/g, "")
            .replace(/&amp;/g, "&")
            .replace(/&#38;/g, "&")
            .replace(/^["']|["']$/g, "");
    }

    root.SniffRules = {
        SOURCES: SOURCES,
        MIME_RE: R.mime,
        NON_MEDIA_RE: R.nonMedia,
        MEDIA_EXT: R.mediaExt,
        MANIFEST_RE: R.manifest,
        SEGMENT_RE: R.segment,
        MANIFEST_MIME_RE: R.manifestMime,
        isManifest: isManifest,
        isSegment: isSegment,
        isAudio: isAudio,
        guessKind: guessKind,
        classify: classify,
        accept: accept,
        normalizeUrl: normalizeUrl
    };
})(typeof self !== "undefined" ? self : globalThis);