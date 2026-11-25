import { ready, call } from './bridge.js';
import { initTheme } from './theme.js';
import { initTabs } from './tabs.js';
import { initDownload } from './download.js';

await ready;
initTheme();
const tabs = initTabs();
const dialogs = { toast: (t, k) => console.log('[toast]', t, k) }; // Task 13 换真实实现
const download = initDownload({ dialogs, results: {}, tabs });
const r = await call('get_config');
download.applyConfig(r.cfg, r);