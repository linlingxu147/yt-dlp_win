import { on } from './bridge.js';

export function initDialogs() {
  const toastBox = document.getElementById('toast');
  function toast(text, kind) {
    const div = document.createElement('div');
    div.className = 'item' + (kind === 'err' ? ' err' : '');
    div.textContent = text;
    toastBox.appendChild(div);
    setTimeout(() => div.remove(), 5000);
  }

  const dlg = document.getElementById('dlg-confirm');
  function confirm(title, text, okLabel = '确定') {
    return new Promise(resolve => {
      document.getElementById('confirm-title').textContent = title;
      document.getElementById('confirm-text').textContent = text;
      document.getElementById('btn-confirm-ok').textContent = okLabel;
      dlg.returnValue = '';
      dlg.showModal();
      dlg.addEventListener('close', () => {
        const v = dlg.returnValue;
        dlg.returnValue = '';
        resolve(v === 'ok');
      }, { once: true });
    });
  }

  const fd = document.getElementById('dlg-formats');
  const ftext = document.getElementById('formats-text');
  let pickHandler = null;
  function openFormats(onPick) {
    ftext.textContent = '正在读取…';
    pickHandler = onPick;
    fd.returnValue = '';
    fd.showModal();
  }
  // 每行一个 div：双击直接命中行元素，避免文本偏移定位的歧义
  ftext.addEventListener('dblclick', e => {
    const d = e.target.closest('div');
    if (!d || !pickHandler) return;
    const m = d.textContent.match(/^\s*([A-Za-z0-9_\-]+)\s+(\w+)\s+/);
    if (m) { pickHandler(m[1]); fd.close(); }
  });
  fd.addEventListener('close', () => {
    const v = fd.returnValue;
    fd.returnValue = '';
    if (v === 'best' && pickHandler) pickHandler('bv*+ba/b');
    if (v === 'audio' && pickHandler) pickHandler('ba');
  });
  on('formats', ev => {
    ftext.textContent = '';
    for (const ln of ev.lines) {
      const d = document.createElement('div');
      d.textContent = ln;
      ftext.appendChild(d);
    }
    const tail = document.createElement('div');
    tail.className = 'hint';
    tail.textContent = ev.code !== 0 ? '（读取失败）' : `共 ${ev.lines.length} 行`;
    ftext.appendChild(tail);
  });
  on('exit_confirm', async (ev) => {
    // 后端只在有活跃任务时发；纯辅助操作/更新时下载计数为 0，改用通用文案
    let text = (ev && ev.text) || '';
    if (!text || /^0 个下载中 \/ 0 个排队/.test(text)) {
      text = '有任务正在进行，退出将停止。';
    }
    const ok = await confirm('退出', text, '退出');
    if (ok) window.pywebview.api.exit();
  });
  return { toast, confirm, openFormats };
}