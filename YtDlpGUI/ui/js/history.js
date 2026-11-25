import { call, on } from './bridge.js';

const STATUS = { ok: '成功', failed: '失败', stopped: '已停止' };
const SOURCE = { gui: '桌面', ext: '扩展', sniff: '嗅探页' };

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
    const re = document.createElement('button');
    re.textContent = '重新下载';
    re.addEventListener('click', async () => {
      const res = await call('history_redownload', i);
      if (!res.ok) window.__toast(res.error || '无法重下', 'err');
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