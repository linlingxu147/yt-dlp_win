import { on } from './bridge.js';

const MAX = 4000;
let progEl = null;

export function initLog() {
  const box = document.getElementById('log');
  function append(text, tag) {
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
  on('logbatch', ev => { for (const it of ev.items) append(it.text, it.tag); });
  document.getElementById('btn-clearlog').addEventListener('click', () => {
    box.textContent = '';
    progEl = null;
  });
  return { clear: () => { box.textContent = ''; progEl = null; } };
}