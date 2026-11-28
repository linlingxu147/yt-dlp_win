import { call, on } from './bridge.js';

const STATUS = { ok: '成功', failed: '失败', stopped: '已停止', canceled: '已取消' };
const SOURCE = { gui: '桌面', ext: '扩展', sniff: '嗅探页',
                 'embedded-page': '下载本页', 'embedded-link': '页面下载' };

async function refresh() {
  const r = await call('history_list');
  const body = document.getElementById('history-body');
  body.textContent = '';
  (r.items || []).forEach((rec, i) => {
    const tr = document.createElement('tr');
    const cells = [
      new Date(rec.ts * 1000).toLocaleString('zh-CN', { hour12: false }),
      rec.title || (rec.urls || [])[0] || '',
      STATUS[rec.status] || rec.status,
      rec.elapsed ? Math.round(rec.elapsed) + ' 秒' : '-',
      SOURCE[rec.source] || rec.source,
    ];
    for (const v of cells) {
      const td = document.createElement('td');
      td.textContent = v;
      td.className = 'ellipsis';
      td.title = v;
      tr.appendChild(td);
    }
    const tdOp = document.createElement('td');
    // failed/stopped 的 .part 还在：同一入口续传（yt-dlp 自动复用 .part）
    const resumable = rec.status === 'failed' || rec.status === 'stopped';
    const re = document.createElement('button');
    re.textContent = resumable ? '继续下载' : '重新下载';
    re.addEventListener('click', async () => {
      const res = await call('history_redownload', i);
      if (!res.ok) window.__toast(res.error || '无法下载', 'err');
      else window.__toast(`已加入 ${(res.task_ids || []).length} 个任务`);
    });
    const open = document.createElement('button');
    open.textContent = '打开目录';
    open.addEventListener('click', () => call('open_outdir', rec.outdir));
    tdOp.append(re, open);
    tr.appendChild(tdOp);
    body.appendChild(tr);
  });
}

export function initHistory({ dialogs }) {
  window.__toast = dialogs.toast;
  document.getElementById('btn-history-clear').addEventListener('click', async () => {
    const ok = await dialogs.confirm('清空历史', '确定清空全部下载历史？此操作不可恢复。', '清空');
    if (!ok) return;
    await call('history_clear');
    refresh();
  });
  on('done', refresh);
  document.getElementById('tab-history').addEventListener('click', refresh);
  refresh();
}