// 三态主题：system（删存储值）/ light / dark；meta 与 CSS color-scheme 同步。
const KEY = 'color-scheme';
const meta = document.querySelector('meta[name="color-scheme"]');

function stored() {
  try { return localStorage.getItem(KEY); } catch { return null; }
}

export function initTheme() {
  const sel = document.getElementById('theme');
  meta.content = stored() || 'light dark';
  sel.value = stored() || 'system';
  sel.addEventListener('change', () => {
    const v = sel.value;
    try {
      if (v === 'system') localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, v);
    } catch {}
    meta.content = v === 'system' ? 'light dark' : v;
  });
}