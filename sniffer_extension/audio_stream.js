"use strict";
// 音频单轨识别：判定在 sniff_rules.js（与桌面内嵌浏览器共用），这里保留旧函数名。
function isAudioStream(url) {
    return typeof SniffRules !== "undefined" && SniffRules.isAudio(url);
}