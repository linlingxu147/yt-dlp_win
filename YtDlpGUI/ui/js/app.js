import { ready, call, on } from './bridge.js';
import { initTheme } from './theme.js';
import { initTabs } from './tabs.js';
import { initDownload } from './download.js';
import { initLog } from './log.js';
import { initResults } from './results.js';
import { initDialogs } from './dialogs.js';
import { initSniff } from './sniff.js';
import { initHistory } from './history.js';

await ready;
initTheme();
const tabs = initTabs();
const dialogs = initDialogs();
const download = initDownload({ dialogs, results: {}, tabs });
window.__collectCfg = download.collectCfg;
const results = initResults({ download, dialogs });
initLog();

const r = await call('get_config');
download.applyConfig(r.cfg, r);

on('progress', ev => {
  document.getElementById('prog-bar').value = ev.pct;
  document.getElementById('prog-pct').textContent = Math.round(ev.pct) + '%';
  document.getElementById('prog-file').textContent = '正在下载：' + (ev.file || '');
  const bits = [];
  if (ev.speed) bits.push('速度 ' + ev.speed);
  if (ev.eta) bits.push('剩余 ' + ev.eta);
  if (ev.size) bits.push('大小 ' + ev.size);
  if (ev.item_total) bits.push(`第 ${Math.max(ev.item_i, 1)}/${ev.item_total} 个`);
  if (ev.done_count) bits.push(`已完成 ${ev.done_count} 个文件`);
  document.getElementById('prog-detail').textContent = bits.join('   ·   ') || '—';
});
on('status', ev => {
  const el = document.getElementById('status');
  el.textContent = ev.text;
  el.dataset.tone = ev.tone;
});
on('task', ev => {
  document.getElementById('btn-start').disabled = ev.busy;
  document.getElementById('btn-detect').disabled = ev.busy;
  document.getElementById('btn-trouble').disabled = ev.busy;
  document.getElementById('btn-update').disabled = ev.busy;
  document.getElementById('btn-formats').disabled = ev.busy;
  document.getElementById('btn-stop').disabled = !ev.busy;
});
on('done', ev => {
  if (ev.code === 0) {
    document.getElementById('prog-bar').value = 100;
    document.getElementById('prog-pct').textContent = '100%';
    document.getElementById('prog-file').textContent = '全部完成';
  } else {
    document.getElementById('prog-file').textContent = '出错或中断 — 见日志中的原因与建议';
  }
});
on('playable', ev => {
  document.getElementById('btn-play').disabled = !ev.ok;
  document.getElementById('play-reason').textContent =
    ev.reason || (ev.ok ? '可以边下边播' : '此源不支持边下边播');
});
document.getElementById('btn-play').addEventListener('click', async () => {
  const r = await call('play_now');
  if (!r.ok) dialogs.toast(r.error || '无法播放', 'err');
});

// ---------------- 工具栏 ----------------
document.getElementById('btn-browser').addEventListener('click', async () => {
  const res = await call('open_browser');
  if (!res.ok) dialogs.toast(res.error || '无法打开内嵌浏览器', 'err');
});

function urlsOrToast() {
  const urls = download.getUrls();
  if (!urls.length) { dialogs.toast('请先粘贴至少一个链接', 'err'); return null; }
  return urls;
}
document.getElementById('btn-start').addEventListener('click', async () => {
  const urls = urlsOrToast();
  if (!urls) return;
  const cfg = download.collectCfg();
  if (cfg.aria && !r.env.has_aria2c) {
    const ok = await dialogs.confirm('未检测到 aria2c',
      '程序目录和 PATH 里都没有 aria2c.exe。这次会改用 yt-dlp 自带下载器继续。要继续吗？');
    if (!ok) return;
  }
  const res = await call('start_download', urls, cfg);
  if (!res.ok) dialogs.toast(res.error || '无法开始下载', 'err');
});
document.getElementById('btn-detect').addEventListener('click', async () => {
  const urls = urlsOrToast();
  if (!urls) return;
  results.clear();
  const res = await call('detect', urls, download.collectCfg());
  if (!res.ok) dialogs.toast(res.error || '无法探测', 'err');
});
document.getElementById('btn-stop').addEventListener('click', () => call('stop'));
document.getElementById('btn-trouble').addEventListener('click', async () => {
  const urls = urlsOrToast();
  if (!urls) return;
  const res = await call('troubleshoot', urls, download.collectCfg());
  if (!res.ok) dialogs.toast(res.error || '无法排障', 'err');
});
document.getElementById('btn-update').addEventListener('click', async () => {
  const res = await call('update_ytdlp', download.collectCfg().proxy);
  if (!res.ok) dialogs.toast(res.error || '无法更新', 'err');
});
document.getElementById('btn-copy').addEventListener('click', async () => {
  const res = await call('copy_cmd', download.getUrls(), download.collectCfg());
  if (!res.ok) { dialogs.toast(res.error || '复制失败', 'err'); return; }
  try {
    await navigator.clipboard.writeText(res.text);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = res.text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  dialogs.toast('命令已复制');
});
document.getElementById('btn-formats').addEventListener('click', async () => {
  const urls = download.getUrls();
  if (!urls.length) { dialogs.toast('请先填写一个链接', 'err'); return; }
  dialogs.openFormats(fid => {
    document.getElementById('mode').value = r.mode_name['custom'];
    document.getElementById('mode').dispatchEvent(new Event('change'));
    document.getElementById('fmt').value = fid;
    dialogs.toast('已选择格式：' + fid);
  });
  const res = await call('fetch_formats', urls[0], download.collectCfg());
  if (!res.ok) dialogs.toast(res.error || '无法读取格式', 'err');
});

initSniff({ dialogs, tabs });
initHistory({ dialogs });