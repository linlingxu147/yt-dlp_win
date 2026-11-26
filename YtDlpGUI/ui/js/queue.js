// 下载队列列表：任务行渲染、选中切换日志、按任务进度更新、全部停止。
// 日志按 task_id 缓冲在 log.js；选中行通过 window.__selectLog 切换右边日志面板。
import { call, on } from './bridge.js';

const STATE = { queued: '排队', running: '下载中', paused: '已暂停',
                done: '完成', failed: '失败', canceled: '已取消' };
const ACTIVE = ['queued', 'running', 'paused'];

let cache = [];
let selected = null;
let follow = true;   // 默认跟随最新运行中任务；用户点行/看辅助日志后停用
let busy = false;    // 辅助操作/更新占闸时，不用队列计数覆盖状态栏
let onSelect = () => {};

function detailText(t) {
  return [t.speed, t.eta, t.size].filter(Boolean).join(' · ') || '—';
}

function row(t) {
  const el = document.createElement('div');
  el.className = 'task-row';
  el.dataset.id = t.id;
  el.setAttribute('role', 'listitem');
  if (t.id === selected) el.classList.add('selected');
  const title = document.createElement('span');
  title.className = 'ellipsis';
  title.textContent = t.title || (t.urls || [])[0] || ('任务 ' + t.id);
  title.title = title.textContent;
  const badge = document.createElement('span');
  badge.className = 'badge ' + t.state;
  badge.textContent = STATE[t.state] || t.state;
  const bar = document.createElement('progress');
  bar.max = 100;
  bar.value = Math.round(t.pct || 0);
  const detail = document.createElement('span');
  detail.className = 'hint';
  detail.dataset.role = 'detail';
  detail.textContent = detailText(t);
  const ops = document.createElement('span');
  ops.className = 'row';
  const mk = (label, fn) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.addEventListener('click', async (e) => {
      e.stopPropagation();
      await fn();
    });
    return b;
  };
  if (t.state === 'running') ops.append(mk('暂停', () => call('pause_task', t.id)));
  if (t.state === 'paused' || t.state === 'queued')
    ops.append(mk('继续', () => call('resume_task', t.id)));
  if (ACTIVE.includes(t.state))
    ops.append(mk('取消', async () => {
      const ok = await window.__confirm('取消任务', '取消将删除已下载的部分文件，确定？', '取消任务');
      if (ok) await call('cancel_task', t.id);
    }));
  el.append(title, badge, bar, detail, ops);
  el.addEventListener('click', () => {
    follow = false;
    selected = t.id;
    onSelect(t.id);
    render();
  });
  return el;
}

function render() {
  const box = document.getElementById('queue-list');
  box.textContent = '';
  if (!cache.length) {
    const empty = document.createElement('div');
    empty.className = 'hint';
    empty.textContent = '还没有任务';
    box.appendChild(empty);
  } else {
    for (const t of cache) box.appendChild(row(t));
  }
  document.getElementById('btn-stop-all').disabled =
    !cache.some(t => ACTIVE.includes(t.state));
}

function paintStat(running, queued) {
  document.getElementById('queue-stat').textContent = `${running} 下载中 · ${queued} 排队`;
  // 下载 executor 不发 status 事件：状态栏由队列计数驱动（辅助操作进行中不覆盖）
  const st = document.getElementById('status');
  if (running || queued) {
    st.textContent = `${running} 个下载中 · ${queued} 排队`;
    st.dataset.tone = 'run';
  } else if (!busy) {
    st.textContent = '就绪';
    st.dataset.tone = 'idle';
  }
}

function adopt(r) {
  cache = (r && r.tasks) || [];
  paintStat((r && r.running) || 0, (r && r.queued) || 0);
  if (follow) {
    const running = cache.filter(t => t.state === 'running');
    const pick = running.length ? running[running.length - 1].id : null;
    if (pick != null) {
      if (pick !== selected) {
        selected = pick;
        onSelect(pick);
      }
    } else if (selected != null && !cache.some(t => t.id === selected)) {
      selected = null;
      onSelect(null);
    }
  }
  render();
}

async function refresh() {
  try {
    const r = await call('queue_snapshot');
    adopt(r);
  } catch {
    /* 窗口收尾阶段取不到快照：保持现有列表 */
  }
}

export function initQueue({ dialogs }) {
  window.__confirm = dialogs.confirm;
  onSelect = (id) => window.__selectLog && window.__selectLog(id);
  document.getElementById('btn-stop-all').addEventListener('click', async () => {
    const ok = await dialogs.confirm('全部停止', '停止所有运行中任务并清空排队？', '全部停止');
    if (!ok) return;
    const r = await call('stop_all');
    if (!r.ok) dialogs.toast(r.error || '停止失败', 'err');
  });
  on('queue_changed', ev => paintStat(ev.running, ev.queued));
  on('task', ev => { busy = !!ev.busy; });
  on('queue', ev => {
    // 新任务入队且当前没盯活跃任务（公共日志/已结束任务）时，恢复跟随最新运行中任务
    if (ev.action === 'added') {
      const cur = cache.find(t => t.id === selected);
      if (selected == null || !cur || !ACTIVE.includes(cur.state)) follow = true;
    }
    refresh();
  });
  on('progress', ev => {
    const t = cache.find(x => x.id === ev.task_id);
    if (!t) return;
    Object.assign(t, { pct: ev.pct, speed: ev.speed, eta: ev.eta, size: ev.size, file: ev.file });
    const el = document.querySelector(`.task-row[data-id="${ev.task_id}"]`);
    if (!el) return;
    el.querySelector('progress').value = Math.round(ev.pct || 0);
    const detail = el.querySelector('[data-role="detail"]');
    if (detail) detail.textContent = detailText(ev);
  });
  refresh();
  return {
    select: (id) => {
      follow = false;
      selected = id ?? null;
      onSelect(selected);
      render();
    },
  };
}