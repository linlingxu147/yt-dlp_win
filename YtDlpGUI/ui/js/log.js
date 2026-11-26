// 运行日志：按 task_id 缓冲（0 = 辅助操作/更新的公共输出），选中任务切换显示。
import { on } from './bridge.js';

const MAX = 4000;
const buffers = new Map();   // task_id -> [{text, tag}]
let current = 0;
let box = null, progEl = null;

function appendDom(text, tag) {
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  if (tag === 'prog') {
    if (!progEl) {
      progEl = document.createElement('div');
      progEl.className = 'prog';
      box.appendChild(progEl);
    }
    progEl.textContent = text.replace(/\n$/, '');
  } else {
    if (progEl) { progEl.remove(); progEl = null; }
    const div = document.createElement('div');
    if (tag) div.className = tag;
    div.textContent = text.replace(/\n$/, '');
    box.appendChild(div);
  }
  while (box.childElementCount > MAX) box.removeChild(box.firstChild);
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

function paint(items) {
  box.textContent = '';
  progEl = null;
  for (const it of items) appendDom(it.text, it.tag);
  box.scrollTop = box.scrollHeight;
}

export function initLog() {
  box = document.getElementById('log');
  // id 为任务号；null 表示看辅助操作公共输出（缓冲键 0）
  window.__selectLog = (id) => {
    current = id ?? 0;
    paint(buffers.get(current) || []);
  };
  on('logbatch', ev => {
    const id = ev.task_id ?? current ?? 0;
    const arr = buffers.get(id) || [];
    for (const it of ev.items) {
      arr.push(it);
      if (id === current) appendDom(it.text, it.tag);
    }
    while (arr.length > MAX) arr.shift();
    buffers.set(id, arr);
  });
  document.getElementById('btn-clearlog').addEventListener('click', () => {
    buffers.set(current, []);
    paint([]);
  });
  return { clear: () => { buffers.clear(); paint([]); } };
}
