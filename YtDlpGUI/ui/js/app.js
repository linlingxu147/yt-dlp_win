import { ready, call, on } from './bridge.js';
import { initTheme } from './theme.js';
import { initTabs } from './tabs.js';
import { initDownload } from './download.js';
import { initLog } from './log.js';
import { initResults } from './results.js';

await ready;
initTheme();
const tabs = initTabs();
const dialogs = { toast: (t, k) => console.log('[toast]', t, k) }; // Task 13 换真实实现
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