import { call, on } from './bridge.js';

// id → [cfg 键, 类型]；checkbox 用 checked，其余用 value
const FIELDS = {
  outdir: ['outdir', 'text'], tpl: ['tpl', 'text'], mode: ['mode', 'mode'],
  merge: ['merge', 'text'], fmt: ['fmt', 'text'],
  browser: ['browser', 'text'], browser_profile: ['browser_profile', 'text'],
  ua: ['ua', 'text'], referer: ['referer', 'text'], headers: ['headers', 'text'],
  generic: ['generic', 'bool'], extractor_args: ['extractor_args', 'text'],
  retries: ['retries', 'text'],
  pl: ['pl', 'bool'], items: ['items', 'text'], ignore: ['ignore', 'bool'],
  open_after: ['open_after', 'bool'], net_hard: ['net_hard', 'bool'],
  sub: ['sub', 'bool'], sublang: ['sublang', 'text'], embedsub: ['embedsub', 'bool'],
  thumb: ['thumb', 'bool'], meta: ['meta', 'bool'],
  rate: ['rate', 'text'], proxy: ['proxy', 'text'], update_mirror: ['update_mirror', 'text'],
  cookie: ['cookie', 'text'],
  queue_n: ['queue_n', 'text'],
  conc: ['conc', 'bool'], conc_n: ['conc_n', 'text'],
  aria: ['aria', 'bool'], aria_args: ['aria_args', 'text'],
  flat: ['flat', 'bool'], extra: ['extra', 'text'], ext_service: ['ext_service', 'bool'],
};

let meta = { mode_fmt: {}, mode_name: {} };
let labelToKey = {};

export function collectCfg() {
  const cfg = {};
  for (const [id, [key, kind]] of Object.entries(FIELDS)) {
    const el = document.getElementById(id);
    if (kind === 'bool') cfg[key] = el.checked;
    else if (kind === 'mode') cfg[key] = labelToKey[el.value] || el.value;
    else cfg[key] = el.value.trim();
  }
  return cfg;
}

export function getUrls() {
  return document.getElementById('url').value.split('\n')
    .map(s => s.trim()).filter(s => s && !s.startsWith('#'));
}

function syncMode(keepFmt) {
  const key = labelToKey[document.getElementById('mode').value];
  document.getElementById('fmt').disabled = key !== 'custom';
  if (!keepFmt && meta.mode_fmt[key]) document.getElementById('fmt').value = meta.mode_fmt[key];
}

export function applyConfig(cfg, m) {
  meta = m;
  labelToKey = Object.fromEntries(Object.entries(m.mode_name).map(([k, v]) => [v, k]));
  const mode = document.getElementById('mode');
  mode.innerHTML = m.modes.map(x => `<option>${x}</option>`).join('');
  document.getElementById('tpl').innerHTML =
    m.templates.map(x => `<option>${x}</option>`).join('');
  document.getElementById('browser').innerHTML =
    m.browsers.map(x => `<option>${x}</option>`).join('');
  document.getElementById('ua-list').innerHTML =
    m.ua_presets.map(x => `<option value="${x.replace(/"/g, '&quot;')}">`).join('');
  for (const [id, [key, kind]] of Object.entries(FIELDS)) {
    const el = document.getElementById(id);
    const v = cfg[key];
    if (kind === 'bool') el.checked = !!v;
    else if (kind === 'mode') el.value = m.mode_name[v] || m.modes[0];
    else el.value = v ?? '';
  }
  syncMode(true);
  document.getElementById('aria-state').textContent =
    m.env.has_aria2c ? '已就绪' : '未检测到 aria2c.exe';
  document.getElementById('env').textContent =
    `yt-dlp: ${m.env.yt_dlp_path} · ffmpeg 已就绪 · ` +
    (m.env.has_aria2c ? 'aria2c 已就绪' : '未检测到 aria2c');
}

export function initDownload({ dialogs, results, tabs }) {
  document.getElementById('mode').addEventListener('change', () => syncMode(false));
  // 同时下载数改动即时生效（host.save_config 会 set_max）；其余设置仍随任务启动保存
  document.getElementById('queue_n').addEventListener('change', async () => {
    const r = await call('save_config', collectCfg());
    if (!r.ok) dialogs.toast('保存同时下载数失败', 'err');
  });
  document.getElementById('btn-paste').addEventListener('click', async () => {
    try {
      const t = await navigator.clipboard.readText();
      const box = document.getElementById('url');
      box.value += (box.value && !box.value.endsWith('\n') ? '\n' : '') + t.trim();
    } catch { dialogs.toast('读剪贴板失败', 'err'); }
  });
  document.getElementById('btn-url-clear').addEventListener('click', () => {
    document.getElementById('url').value = '';
  });
  document.getElementById('btn-browse-outdir').addEventListener('click', async () => {
    const r = await call('browse_outdir');
    if (r.path) document.getElementById('outdir').value = r.path;
  });
  document.getElementById('btn-open-outdir').addEventListener('click', () =>
    call('open_outdir', document.getElementById('outdir').value));
  document.getElementById('btn-browse-cookie').addEventListener('click', async () => {
    const r = await call('browse_cookie');
    if (r.path) document.getElementById('cookie').value = r.path;
  });
  document.getElementById('ext_service').addEventListener('change', async (e) => {
    const r = await call('set_ext_service', e.target.checked);
    if (!r.ok) dialogs.toast(r.error || '扩展服务切换失败', 'err');
  });
  on('applyfix', (ev) => {
    for (const k of ['ua', 'referer', 'browser', 'generic']) {
      const el = document.getElementById(k);
      if (el.type === 'checkbox') el.checked = !!ev.cfg[k];
      else el.value = ev.cfg[k] ?? '';
    }
    const parts = [];
    if (ev.touched?.length) parts.push('已自动应用：' + ev.touched.join('、'));
    if (ev.manual?.length) parts.push('需手动设置：' + ev.manual.join('、'));
    dialogs.toast(parts.join('；') || '排障完成');
  });
  on('svc', (ev) => {
    const el = document.getElementById('svc');
    el.textContent = ev.text;
    el.style.color = ev.ok ? 'var(--ok)' : 'var(--warn)';
  });
  on('sniff_started', () => tabs.select(document.getElementById('tab-download')));
  return { applyConfig, collectCfg, getUrls };
}