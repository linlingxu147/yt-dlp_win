"use strict";
// 音频单轨识别：与 ytdlp_core.looks_like_audio 同口径。
// B 站 DASH 的音频轨是独立 m4s，流 ID 在 302xx（30216/30232/30280…）；
// 视频轨是 100026/30080 这类，不能混。
const AUDIO_STREAM_RE = /[-_]302\d{2}\.m4s$/i;
const AUDIO_EXT_RE = /\.(m4a|mp3|aac|flac|wav|opus)$/i;

function isAudioStream(url) {
    const path = String(url || "").split("#")[0].split("?")[0];
    return AUDIO_STREAM_RE.test(path) || AUDIO_EXT_RE.test(path);
}