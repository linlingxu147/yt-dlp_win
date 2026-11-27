import { call, on } from './bridge.js';

let items = [];
let shown = [];
let sig = '';
let svcOff = false;
const selected = new Set();

function fmtSize(b) {
  if (!b) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(i ? 1 : 0) + u[i];
}

// 图片条目只来自内嵌浏览器的 DOM 补扫（网络层不收图片），默认折叠
function isImage(r) {
  return r.cat === 'image' || r.kind === 'image';
}

function render() {
  const body = document.getElementById('sniff-body');
  body.textContent = '';
  shown.forEach((r, i) => {
    const tr = document.createElement('tr');
    if (r.seg) tr.classList.add('seg');
    if (r.audio) tr.classList.add('audio');
    const tdSel = document.createElement('td');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = selected.has(r.url);
    cb.addEventListener('change', () => {
      if (cb.checked) selected.add(r.url); else selected.delete(r.url);
    });
    tdSel.appendChild(cb);
    tr.appendChild(tdSel);
    const entry = r.entry === 'embedded' ? '内嵌'
      : r.entry === 'extension' ? '扩展' : (r.entry || '');
    for (const v of [r.title || '', r.url || '', r.audio ? '音频' : (r.mime || r.kind || ''),
                     entry, fmtSize(r.size), r.source || '', r.page_url || '',
                     new Date((r.ts || 0) * 1000).toLocaleTimeString('zh-CN', { hour12: false })]) {
      const td = document.createElement('td');
      td.textContent = v;
      td.className = 'ellipsis';
      td.title = v;
      tr.appendChild(td);
    }
    body.appendChild(tr);
  });
  const fold = document.getElementById('sn-fold').checked;
  const showAudio = document.getElementById('sn-audio').checked;
  const showImage = document.getElementById('sn-image').checked;
  const audioCount = items.filter(r => r.audio).length;
  const imageCount = items.filter(isImage).length;
  const audioHint = audioCount ? `${showAudio ? '显示' : '隐藏'} ${audioCount} 条` : '无';
  const imageHint = imageCount ? `${showImage ? '显示' : '隐藏'} ${imageCount} 条` : '无';
  document.getElementById('sn-count').textContent =
    `共 ${shown.length} 条（折叠分片：${fold ? '开' : '关'}；音频流：${audioHint}；图片：${imageHint}）`
    + (svcOff ? '；扩展上报未启用，目前只有内嵌浏览器记录' : '');
}

async function refresh() {
  const d = await call('sniff_items');
  // svc=False 只代表扩展上报通道没开；内嵌浏览器的条目仍要照常显示
  svcOff = !d.svc;
  items = d.items || [];
  const fold = document.getElementById('sn-fold').checked;
  const showAudio = document.getElementById('sn-audio').checked;
  const showImage = document.getElementById('sn-image').checked;
  shown = items.filter(r => (r.audio ? showAudio : !(fold && r.seg))
                             && (showImage || !isImage(r)));
  const s = JSON.stringify([fold, showAudio, showImage, svcOff,
    shown.map(r => [r.url, r.ts, r.size, r.entry, r.cat, r.mime])]);
  if (s === sig) return;
  sig = s;
  render();
}

export function initSniff({ dialogs, tabs }) {
  document.getElementById('btn-sn-refresh').addEventListener('click', refresh);
  document.getElementById('sn-fold').addEventListener('change', () => { sig = ''; refresh(); });
  document.getElementById('sn-audio').addEventListener('change', () => { sig = ''; refresh(); });
  document.getElementById('sn-image').addEventListener('change', () => { sig = ''; refresh(); });
  document.getElementById('btn-sn-clear').addEventListener('click', async () => {
    await call('sniff_clear');
    selected.clear();
    sig = '';
    refresh();
  });
  document.getElementById('btn-sn-download').addEventListener('click', async () => {
    const its = shown.filter(r => selected.has(r.url));
    if (!its.length) { dialogs.toast('先选中要下载的条目', 'err'); return; }
    const r = await call('sniff_download', its);
    if (!r.ok) dialogs.toast(r.error || '无法下载', 'err');
    else tabs.select(document.getElementById('tab-download'));
  });
  document.getElementById('btn-sn-preview').addEventListener('click', async () => {
    const its = shown.filter(r => selected.has(r.url));
    if (its.length !== 1) { dialogs.toast('预览一次只选一条', 'err'); return; }
    document.getElementById('sn-pv').textContent = '正在抓预览…';
    document.getElementById('btn-sn-play').disabled = true;
    const r = await call('sniff_preview', its[0].url);
    const img = document.getElementById('sn-img');
    if (!r.ok) {
      document.getElementById('sn-pv').textContent = '预览失败：' + (r.error || '未知原因');
      return;
    }
    if (r.thumb_data_url) { img.src = r.thumb_data_url; img.hidden = false; }
    const dur = r.duration ? `${Math.floor(r.duration / 60)} 分 ${Math.round(r.duration % 60)} 秒` : '未知';
    document.getElementById('sn-pv').textContent =
      `时长 ${dur}` + (r.has_clip ? '　片段已就绪，可播放' : '　（此源没有可抓的片段）');
    document.getElementById('btn-sn-play').disabled = !r.has_clip;
  });
  document.getElementById('btn-sn-play').addEventListener('click', async () => {
    const r = await call('sniff_play_clip');
    if (!r.ok) dialogs.toast(r.error || '播放失败', 'err');
  });
  // 只在嗅探页可见时轮询（沿用旧版 3 秒节奏）
  setInterval(() => {
    if (!document.getElementById('panel-sniff').hidden) refresh();
  }, 3000);
}