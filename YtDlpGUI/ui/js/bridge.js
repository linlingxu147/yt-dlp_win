// pywebview 桥：JS→Python 调用 + Python→JS 事件路由。
export const ready = new Promise((resolve) => {
  if (window.pywebview?.api) resolve();
  else window.addEventListener('pywebviewready', () => resolve(), { once: true });
});

export async function call(method, ...args) {
  await ready;
  return window.pywebview.api[method](...args);
}

const subscribers = new Map();
export function on(type, fn) {
  if (!subscribers.has(type)) subscribers.set(type, []);
  subscribers.get(type).push(fn);
}

window.__dispatch = (ev) => {
  for (const fn of subscribers.get(ev.type) || []) fn(ev);
};