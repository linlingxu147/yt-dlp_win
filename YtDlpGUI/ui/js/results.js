import { call, on } from './bridge.js';

let rows = [];
const checked = new Set();
let gen = 0;
let previewUrl = '';

function fmtDur(sec) {
  if (!sec && sec !== 0) return '-';
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + ':' : '') + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}
function fmtSize(b) {
  if (!b) return '-';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(i ? 1 : 0) + u[i];
}

function updateCount() {
  document.getElementById('result-count').textContent =
    rows.length ? `共 ${rows.length} 条，已选 ${checked.size} 条` : '尚未探测';
  document.getElementById('btn-dl-sel').disabled = !rows.length;
}

function renderRow(i) {
  const info = rows[i];
  const tr = document.createElement('tr');
  tr.dataset.i = i;
  if (checked.has(i)) tr.classList.add('checked');
  const tdCheck = document.createElement('td');
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = checked.has(i);
  cb.setAttribute('aria-label', '勾选 ' + (info.title || ''));
  cb.addEventListener('click', e => e.stopPropagation());
  cb.addEventListener('change', () => toggle(i));
  tdCheck.appendChild(cb);
  tr.appendChild(tdCheck);
  for (const [key, val, cls] of [
    ['title', info.title || '', 'ellipsis'], ['dur', fmtDur(info.dur), ''],
    ['res', info.res || '-', ''], ['size', fmtSize(info.size), ''],
    ['up', info.up || '-', 'ellipsis'],
  ]) {
    const td = document.createElement('td');
    td.textContent = val;
    if (cls) td.className = cls;
    if (key === 'title' || key === 'up') td.title = val;
    tr.appendChild(td);
  }
  tr.addEventListener('click', () => { toggle(i); showPreview(i); });
  return tr;
}

function toggle(i) {
  if (checked.has(i)) checked.delete(i); else checked.add(i);
  const tr = document.querySelector(`#results-body tr[data-i="${i}"]`);
  tr.classList.toggle('checked', checked.has(i));
  tr.querySelector('input').checked = checked.has(i);
  updateCount();
}

async function showPreview(i) {
  const info = rows[i];
  previewUrl = info.url || '';
  document.getElementById('btn-pv-open').disabled = !previewUrl;
  document.getElementById('pv-meta').textContent =
    `${info.title || ''}\n时长 ${fmtDur(info.dur)} · 清晰度 ${info.res || '-'} · ` +
    `大小 ${fmtSize(info.size)}\n上传者 ${info.up || '-'}`;
  const img = document.getElementById('pv-img');
  const txt = document.getElementById('pv-img-text');
  if (!info.thumb) {
    img.hidden = true; txt.hidden = false; txt.textContent = '这个条目没有封面地址';
    return;
  }
  img.hidden = true; txt.hidden = false; txt.textContent = '封面加载中…';
  const myGen = ++gen;
  const r = await call('get_thumb', info.thumb, window.__collectCfg());
  if (myGen !== gen) return;
  if (r.ok) {
    img.src = r.data_url;
    img.hidden = false; txt.hidden = true;
    img.onerror = () => {
      img.hidden = true; txt.hidden = false; txt.textContent = '封面格式无法显示';
    };
  } else {
    txt.textContent = '封面取不到：' + (r.err || '未知原因');
  }
}

export function initResults({ download, dialogs }) {
  const body = document.getElementById('results-body');
  on('detrow', ev => {
    const i = rows.length;
    rows.push(ev.info);
    checked.add(i);
    body.appendChild(renderRow(i));
    updateCount();
  });
  on('detdone', () => updateCount());
  document.getElementById('btn-all').addEventListener('click', () => {
    rows.forEach((_, i) => checked.add(i));
    body.querySelectorAll('tr').forEach(tr => {
      tr.classList.add('checked');
      tr.querySelector('input').checked = true;
    });
    updateCount();
  });
  document.getElementById('btn-none').addEventListener('click', () => {
    checked.clear();
    body.querySelectorAll('tr').forEach(tr => {
      tr.classList.remove('checked');
      tr.querySelector('input').checked = false;
    });
    updateCount();
  });
  document.getElementById('btn-clear-results').addEventListener('click', () => {
    rows = []; checked.clear(); body.textContent = ''; updateCount();
  });
  document.getElementById('btn-dl-sel').addEventListener('click', async () => {
    const urls = [...checked].sort((a, b) => a - b)
      .map(i => rows[i].url).filter(Boolean);
    if (!urls.length) { dialogs.toast('请至少勾选一个条目', 'err'); return; }
    const r = await call('start_download', urls, download.collectCfg(), true);
    if (!r.ok) dialogs.toast(r.error || '无法开始下载', 'err');
  });
  document.getElementById('btn-pv-open').addEventListener('click', () =>
    call('open_url', previewUrl));
  return {
    clear: () => { rows = []; checked.clear(); body.textContent = ''; updateCount(); },
    selectedUrls: () => [...checked].sort((a, b) => a - b).map(i => rows[i].url).filter(Boolean),
  };
}