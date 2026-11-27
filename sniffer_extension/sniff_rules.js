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
        "minMimeBytes": 204800,
        "opaqueMime": "^(application/octet-stream|binary/octet-stream)?$",
        "staticExt": "\\.(js|css|json|html?|xml|txt|map|woff2?|ttf|otf|eot|svg|ico)(\\?|#|$)",
        "fallbackBytes": 1048576
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

    function accept(url, mime, size, opts) {
        const raw = String(url || "");
        if (!/^https?:\/\//i.test(raw)) return false;
        const u = noHash(raw);
        const path = noQuery(u);
        const m = String(mime || "");
        const n = Number(size) || 0;
        const ranged = !!(opts && (opts.range || opts.ranged));
        const mimeHit = R.mime.test(m);
        const extHit = R.mediaExt.test(u);
        const pathExtHit = R.mediaExt.test(path);
        if (mimeHit || extHit) {
            // 非媒体 MIME 只有路径本身带媒体后缀才收：日志/埋点常把页面或媒体
            // 地址塞进查询串，不能因为 ?url=...mp4 就当成视频。
            if (R.nonMedia.test(m) && !R.manifestMime.test(m) && !pathExtHit) return false;
            // 纯靠 MIME 命中且体积太小的（封面、提示音）忽略
            if (mimeHit && !extHit && n > 0 && n < SOURCES.minMimeBytes) return false;
            return true;
        }
        // 兜底：签名直链（无后缀 + application/octet-stream / 空 MIME）。
        // 这类地址只能靠「像不像大文件」判断：有 Content-Range（分片续传）或体积过阈值，
        // 宁可多收几条交给面板，也不要整条漏掉。传了 opts 才启用，不传维持旧行为。
        if (!opts || !R.opaqueMime.test(m.trim())) return false;
        // JS/CSS/字体等静态资源即便被 CDN 标成 octet-stream、体积过 1MB，也不是媒体
        if (R.staticExt.test(path)) return false;
        if (ranged) return true;
        return n >= (SOURCES.fallbackBytes || 1048576);
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