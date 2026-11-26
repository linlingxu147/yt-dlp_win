import { ready, call, on } from './bridge.js';
import { initTheme, resolvedTheme } from './theme.js';
import { initTabs } from './tabs.js';
import { initDownload } from './download.js';
import { initLog } from './log.js';
import { initResults } from './results.js';
import { initDialogs } from './dialogs.js';
import { initSniff } from './sniff.js';
import { initHistory } from './history.js';
import { initQueue } from './queue.js';

await ready;
initTheme();
const tabs = initTabs();
const dialogs = initDialogs();
const download = initDownload({ dialogs, results: {}, tabs });
window.__collectCfg = download.collectCfg;
const results = initResults({ download, dialogs });
initLog();
const queue = initQueue({ dialogs });

const r = await call('get_config');
download.applyConfig(r.cfg, r);

// 辅助操作（探测/排障/格式）的输出是公共日志：切到公共视图再发起
function showAuxLog() { queue.select(null); }

on('status', ev => {
  const el = document.getElementById('status');
  el.textContent = ev.text;
  el.dataset.tone = ev.tone;
});
on('task', ev => {
  document.getElementById('btn-start').disabled = ev.kind === 'update';
  document.getElementById('btn-detect').disabled = ev.busy;
  document.getElementById('btn-trouble').disabled = ev.busy;
  document.getElementById('btn-formats').disabled = ev.busy;
  document.getElementById('btn-update').disabled = ev.busy || ev.downloading;
  document.getElementById('btn-stop').disabled = !ev.downloading;
});
on('done', ev => {
  if (ev.code !== 0) dialogs.toast('任务 ' + (ev.task_id ?? '') + ' 失败或中断，见日志', 'err');
});

// ---------------- 工具栏 ----------------
document.getElementById('btn-browser').addEventListener('click', async () => {
  const res = await call('open_browser', resolvedTheme());
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
  if (!res.ok) { dialogs.toast(res.error || '无法开始下载', 'err'); return; }
  dialogs.toast(`已加入 ${(res.task_ids || []).length} 个任务`);
});
document.getElementById('btn-detect').addEventListener('click', async () => {
  const urls = urlsOrToast();
  if (!urls) return;
  results.clear();
  showAuxLog();
  const res = await call('detect', urls, download.collectCfg());
  if (!res.ok) dialogs.toast(res.error || '无法探测', 'err');
});
document.getElementById('btn-stop').addEventListener('click', async () => {
  const res = await call('stop_all');
  if (!res.ok) dialogs.toast(res.error || '停止失败', 'err');
});
document.getElementById('btn-trouble').addEventListener('click', async () => {
  const urls = urlsOrToast();
  if (!urls) return;
  showAuxLog();
  const res = await call('troubleshoot', urls, download.collectCfg());
  if (!res.ok) dialogs.toast(res.error || '无法排障', 'err');
});
document.getElementById('btn-update').addEventListener('click', async () => {
  const cfg = download.collectCfg();
  const res = await call('update_ytdlp', cfg.proxy, cfg.update_mirror);
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
  showAuxLog();
  const res = await call('fetch_formats', urls[0], download.collectCfg());
  if (!res.ok) dialogs.toast(res.error || '无法读取格式', 'err');
});

initSniff({ dialogs, tabs });
initHistory({ dialogs });