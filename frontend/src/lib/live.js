// One EventSource for the whole app. Views subscribe with useLive(handler), which unsubscribes when
// the view unmounts. The browser reconnects by itself and sends Last-Event-ID, so the server replays
// what was missed. ?live=0 gives a static page (printing, screenshots for a report).
import { onUnmounted, reactive } from "vue";

const handlers = new Set();
export const live = reactive({ connected: false, disabled: false, toasts: [] });
let source = null;
let toastId = 0;

export function startLive() {
  live.disabled = new URLSearchParams(location.search).get("live") === "0";
  if (source || live.disabled || !window.EventSource) return;
  source = new EventSource("/api/stream");
  source.onopen = () => { live.connected = true; };
  source.onerror = () => { live.connected = false; };
  source.onmessage = (msg) => {
    let event;
    try { event = JSON.parse(msg.data); } catch { return; }
    handlers.forEach((fn) => { try { fn(event); } catch (err) { console.error(err); } });
  };
}

export function useLive(handler) {
  handlers.add(handler);
  onUnmounted(() => handlers.delete(handler));
}

export function toast(text, tone = "info") {
  const id = ++toastId;
  live.toasts.unshift({ id, text, tone, out: false });
  live.toasts.splice(4);
  setTimeout(() => { const t = live.toasts.find((x) => x.id === id); if (t) t.out = true; }, 6000);
  setTimeout(() => { const i = live.toasts.findIndex((x) => x.id === id); if (i >= 0) live.toasts.splice(i, 1); }, 6600);
}

// Coalesce bursts (a refresh publishes several events) into one reload.
export function debounce(fn, ms = 700) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}
