<script setup>
import { computed, markRaw, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import L from "leaflet";
import ChartCanvas from "../components/ChartCanvas.vue";
import { api, post } from "../lib/api.js";
import { debounce, toast, useLive } from "../lib/live.js";
import { clock, dayLong, hourLabel, LEVEL_COLORS, NO_DATA, todayIso } from "../lib/format.js";

const BASIS = { sensor: "Field sensor", lwp: "Modelled leaf wetness", dpd: "Dew point", rh: "Humidity" };
const NODES = [
  { id: "source", title: "Weather in", text: "Field sensor or Open-Meteo forecast" },
  { id: "ingest", title: "Ingest", text: "Validated, floored to the hour, stored" },
  { id: "risk", title: "Risk engine", text: "Infection-night score, rules-v3" },
  { id: "alert", title: "Alert rule", text: "≥ 2 High of next 3 days, fresh data" },
  { id: "push", title: "Dashboards", text: "Pushed to open pages" },
];

const status = ref(null);
const districts = ref([]);
const feed = ref([]);
const hot = reactive({});
const caption = ref("Waiting for activity…");
const form = reactive({ district: null, scenario: "dew", speed: 1.5 });
const reading = ref(null);
const series = reactive({ labels: [], rh: [], wet: [], temp: [] });
const now = ref(Date.now());
const mapEl = ref(null);
let map = null;
let markerLayer = null;
const markers = new Map();
let simDistrict = null;

const demo = computed(() => status.value?.demo ?? false);
const sim = computed(() => status.value?.sim ?? { running: false, step: 0, total: 0 });
const wetChart = computed(() => ({
  type: "line",
  data: {
    labels: [...series.labels],
    datasets: [
      { label: "Relative humidity %", data: [...series.rh], borderColor: "#2a78d6", borderWidth: 2, pointRadius: 0 },
      { label: "Leaf wet (% of the hour)", data: [...series.wet], borderColor: "#1baf7a", backgroundColor: "#1baf7a33",
        borderWidth: 2, pointRadius: 0, fill: true, stepped: "middle" },
    ],
  },
  options: { animation: false, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
             plugins: { legend: { position: "bottom", labels: { boxWidth: 12, boxHeight: 2 } } },
             scales: { x: { ticks: { maxTicksLimit: 6 } }, y: { min: 0, max: 100 } } },
}));
const tempChart = computed(() => ({
  type: "line",
  data: { labels: [...series.labels],
          datasets: [{ label: "Air temperature °C", data: [...series.temp], borderColor: "#eb6834", borderWidth: 2, pointRadius: 0 }] },
  options: { animation: false, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
             plugins: { legend: { position: "bottom", labels: { boxWidth: 12, boxHeight: 2 } } },
             scales: { x: { ticks: { maxTicksLimit: 6 } } } },
}));

function countdown(iso) {
  now.value; // re-evaluate every second
  if (!iso) return "not scheduled (server started without --with-scheduler)";
  const s = Math.max(0, Math.round((new Date(iso) - Date.now()) / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return `in ${h ? `${h} h ` : ""}${m} min ${String(sec).padStart(2, "0")} s`;
}

function pulse(nodes, text) {
  nodes.forEach((id, i) => setTimeout(() => {
    hot[id] = false;
    requestAnimationFrame(() => { hot[id] = true; setTimeout(() => { hot[id] = false; }, 1400); });
  }, i * 160));
  if (text) caption.value = text;
}

function describe(e) {
  const d = e.data || {};
  switch (e.kind) {
    case "refresh.started": return ["⟳", `Forecast refresh started (${d.trigger})`];
    case "refresh.weather": return ["☁", `Weather fetched: ${d.rows} hourly rows from ${d.freshness?.source ?? "?"}`];
    case "refresh.weather_failed": return ["⚠", `All forecast sources failed; keeping previous data. ${d.error}`];
    case "refresh.done": return ["✓", `Refresh finished: ${d.risk_days} district-days scored, ${d.changed} levels changed`];
    case "blocks.updated": return ["▦", `Block forecast: ${d.hours} hourly rows, ${d.days} block-days scored`];
    case "blocks.failed": return ["⚠", `Block forecast failed; district risk unaffected. ${d.error}`];
    case "risk.unchanged": return ["•", `Risk recomputed (${d.cause}): no level changed`];
    case "risk.changed": {
      const list = (d.changes || []).slice(0, 4).map((c) => `${c.district} ${dayLong(c.date)} ${c.from ?? "new"} → ${c.to}`);
      const more = d.changes.length > 4 ? ` and ${d.changes.length - 4} more` : "";
      return ["▲", `Risk changed (${d.cause}): ${list.join("; ")}${more}`];
    }
    case "alerts.evaluated": {
      if (d.suppressed) return ["⏸", `Alert rule paused: ${d.suppressed}`];
      const parts = [];
      if (d.created?.length) parts.push(`new alert for ${d.created.join(", ")}`);
      if (d.updated?.length) parts.push(`updated ${d.updated.join(", ")}`);
      if (d.expired?.length) parts.push(`${d.expired.length} withdrawn`);
      return ["✉", `Alert rule (${d.cause}): ${parts.join("; ") || "no change"}`];
    }
    case "sensor.reading": {
      const r = d.reading || {};
      const day = d.day ? ` · ${dayLong(d.day.date)} now ${Math.round(d.day.score)} ${d.day.level}` : "";
      return ["◉", `${d.station} → ${d.district} ${hourLabel(r.ts)}: ${r.temp_c} °C, RH ${r.rh_pct} %, leaf wet ${r.leaf_wet_min} min${day}`];
    }
    case "sim.started": return ["▶", `Virtual station ${d.station} started in ${d.district}: ${d.description} (${d.hours} hours)`];
    case "sim.done": return ["■", `Virtual station ${d.stopped ? "stopped" : "finished"} after ${d.steps} hours in ${d.district}`];
    case "sim.failed": return ["⚠", `Virtual station failed: ${d.error}`];
    case "sim.reset": return ["↺", `Simulated readings deleted in ${d.districts} district(s)`];
    case "ensemble.started": return ["⟳", "Ensemble refresh started: ECMWF, GEFS, ICON"];
    case "ensemble.model": return ["☁", `${d.label}: ${d.members} members scored`];
    case "ensemble.failed": return ["⚠", `${d.label} ensemble failed; other models kept`];
    case "ensemble.updated": return ["✓", `Ensemble updated: ${d.models.length} models, ${d.rows} district-night rows`];
    default: return ["•", e.kind];
  }
}

function addToFeed(e) {
  const [icon, text] = describe(e);
  feed.value.unshift({ id: e.id, at: e.at, icon, text, kind: e.kind.split(".")[0],
                       strong: e.kind === "risk.changed" || e.kind.endsWith("failed") });
  feed.value.splice(80);
}

async function loadStatus() { status.value = await api("/api/live/status"); }

async function loadMap() {
  const risk = await api(`/api/risk?date=${todayIso()}`);
  const byId = new Map(risk.districts.map((d) => [d.id, d]));
  districts.value.forEach((d) => {
    const r = byId.get(d.id);
    const color = r ? LEVEL_COLORS[r.level] : NO_DATA;
    let m = markers.get(d.id);
    if (!m) {
      m = L.circleMarker([d.lat, d.lon], { radius: 9, weight: 2, fillOpacity: 0.65 }).addTo(markerLayer);
      m.on("click", () => { form.district = d.id; });
      markers.set(d.id, m);
    }
    const changed = m.options.fillColor && m.options.fillColor !== color;
    m.setStyle({ color, fillColor: color });
    m.bindTooltip(r ? `${d.name}: ${r.level} (${Math.round(r.score)}) · ${BASIS[r.wetness_basis] || "—"}` : `${d.name}: no data`);
    if (changed) {
      const ring = L.circleMarker([d.lat, d.lon], { radius: 10, color: "#d14343", weight: 3, fill: false, className: "ping" }).addTo(map);
      setTimeout(() => ring.remove(), 1600);
    }
  });
}

function clearSeries() { series.labels = []; series.rh = []; series.wet = []; series.temp = []; reading.value = null; }

function plot(r) {
  series.labels = [...series.labels, hourLabel(r.ts)];
  series.rh = [...series.rh, r.rh_pct];
  series.wet = [...series.wet, r.leaf_wet_min != null ? (r.leaf_wet_min / 60) * 100 : null];
  series.temp = [...series.temp, r.temp_c];
}

async function act(fn) {
  try { await fn(); } catch (err) { toast(err.message, "bad"); }
  loadStatus().catch(console.error);
}
const start = () => act(async () => {
  clearSeries();
  simDistrict = form.district;
  await post("/api/sim/start", { district_id: form.district, scenario: form.scenario, seconds_per_hour: Number(form.speed) });
});
const stop = () => act(() => post("/api/sim/stop"));
const reset = () => act(async () => {
  const r = await post("/api/sim/reset");
  toast(`Simulated readings removed from ${r.districts} district(s)`);
});
const refreshNow = () => act(() => post("/api/refresh"));
const ensembleNow = () => act(() => post("/api/ensemble/refresh"));

const reloadMap = debounce(() => loadMap().catch(console.error), 400);
const reloadStatus = debounce(() => loadStatus().catch(console.error), 300);

useLive((e) => {
  addToFeed(e);
  const d = e.data || {};
  switch (e.kind) {
    case "sensor.reading":
      pulse(["source", "ingest", "risk", "push"], `${d.station}: reading for ${hourLabel(d.reading.ts)} stored, ${d.district} rescored`);
      if (simDistrict == null || d.district_id === simDistrict) {
        simDistrict = d.district_id;
        plot(d.reading);
        reading.value = d.day;
      }
      reloadStatus();
      break;
    case "refresh.weather":
      pulse(["source", "ingest"], "Forecast fetched from Open-Meteo");
      reloadStatus();
      break;
    case "blocks.updated":
      pulse(["source", "ingest", "risk"], `${d.days} block-days scored`);
      break;
    case "risk.changed":
    case "risk.unchanged":
      pulse(["risk", "push"], e.kind === "risk.changed" ? `${d.changes.length} district-day level(s) changed` : "Risk recomputed, no change");
      reloadMap();
      break;
    case "alerts.evaluated":
      pulse(["alert", "push"], d.created?.length ? `New alert ready for ${d.created.join(", ")}` : "Alert rule checked");
      if (d.created?.length) toast(`New farmer alert ready: ${d.created.join(", ")}`, "bad");
      break;
    case "sim.reset":
      clearSeries();
      reloadMap();
      reloadStatus();
      break;
    default:
      reloadStatus();
  }
});

let tick, poll;
onMounted(async () => {
  map = markRaw(L.map(mapEl.value, { scrollWheelZoom: false }).setView([10.9, 78.9], 7));
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 12, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  markerLayer = markRaw(L.layerGroup().addTo(map));
  tick = setInterval(() => { now.value = Date.now(); }, 1000);
  poll = setInterval(() => loadStatus().catch(console.error), 30000);
  try {
    const [ds, st, recent] = await Promise.all([api("/api/districts"), api("/api/live/status"), api("/api/events?limit=40")]);
    districts.value = ds;
    status.value = st;
    form.district = ds.find((d) => d.name === "Thanjavur")?.id ?? ds[0]?.id;
    recent.forEach(addToFeed);
    map.fitBounds(L.latLngBounds(ds.map((d) => [d.lat, d.lon])), { padding: [20, 20] });
    await loadMap();
  } catch (err) { console.error(err); toast(err.message, "bad"); }
});
onBeforeUnmount(() => { clearInterval(tick); clearInterval(poll); map?.remove(); });
</script>

<template>
  <div>
  <main>
    <section class="card wide">
      <div class="card-head">
        <h2>How a reading becomes an alert</h2>
        <span class="muted small">{{ caption }}</span>
      </div>
      <ol class="flow">
        <li v-for="n in NODES" :key="n.id" :class="{ hot: hot[n.id] }">
          <b>{{ n.title }}</b>
          <span v-if="n.id === 'push' && status">{{ status.listeners }} open page{{ status.listeners === 1 ? "" : "s" }} receive each change</span>
          <span v-else>{{ n.text }}</span>
        </li>
      </ol>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>Virtual field station</h2>
        <span class="muted small">Same ingest path as a real leaf-wetness sensor</span>
      </div>
      <p v-if="status && !demo" class="banner stale">Demo controls are off. Start the server with
        <code>BLASTWATCH_DEMO=1</code> to run the virtual station and manual refreshes.</p>
      <p class="muted small">Replays last night's infection window (yesterday 12:00 to now) for one district, one simulated
        hour per step. Each reading overrides grid weather for that hour, the district is rescored, and every open
        dashboard updates. <b>Reset</b> deletes all simulated readings.</p>
      <div class="form-row">
        <label>District
          <select v-model="form.district"><option v-for="d in districts" :key="d.id" :value="d.id">{{ d.name }}</option></select>
        </label>
        <label>Speed
          <select v-model="form.speed">
            <option :value="0.5">Fast (0.5 s per hour)</option>
            <option :value="1.5">Normal (1.5 s per hour)</option>
            <option :value="3">Slow (3 s per hour)</option>
          </select>
        </label>
      </div>
      <fieldset class="scenarios">
        <legend class="muted small">Scenario</legend>
        <label v-for="(text, key) in status?.scenarios || {}" :key="key" class="scenario">
          <input v-model="form.scenario" type="radio" name="scenario" :value="key">
          <span><b>{{ key[0].toUpperCase() + key.slice(1) }}</b><small>{{ text }}</small></span>
        </label>
      </fieldset>
      <div class="msg-actions">
        <button class="primary" :disabled="!demo || sim.running" @click="start">Start</button>
        <button :disabled="!demo || !sim.running" @click="stop">Stop</button>
        <button :disabled="!demo || sim.running" @click="reset">Reset simulated data</button>
      </div>
      <div class="progress" aria-hidden="true"><i :style="{ width: sim.total ? `${(100 * sim.step) / sim.total}%` : '0' }"></i></div>
      <p class="muted small">
        <template v-if="sim.running">Streaming {{ sim.district }} ({{ sim.scenario }}): hour {{ sim.step }} of {{ sim.total }}<template
          v-if="sim.last">, simulated clock {{ hourLabel(sim.last.reading.ts) }}</template></template>
        <template v-else-if="sim.step">Last run: {{ sim.district }}, {{ sim.step }} of {{ sim.total }} hours.</template>
        <template v-else>Idle.</template>
      </p>
      <div class="tiles">
        <div class="tile"><span class="muted small">Today's score</span><b>{{ reading ? `${Math.round(reading.score)} / 100` : "–" }}</b>
          <span v-if="reading" class="badge" :style="{ '--c': LEVEL_COLORS[reading.level] }">{{ reading.level }}</span></div>
        <div class="tile"><span class="muted small">Longest wet run</span><b>{{ reading ? `${reading.longest_wet_run} h` : "–" }}</b></div>
        <div class="tile"><span class="muted small">Wet hours</span><b>{{ reading ? `${reading.wet_hours} h` : "–" }}</b></div>
        <div class="tile"><span class="muted small">Wetness from</span><b>{{ reading ? BASIS[reading.wetness_basis] || "—" : "–" }}</b></div>
      </div>
      <h3>Humidity and leaf wetness (%)</h3>
      <ChartCanvas :config="wetChart" height="150px" />
      <h3>Temperature (°C)</h3>
      <ChartCanvas :config="tempChart" height="150px" />
    </section>

    <section class="card">
      <div class="card-head"><h2>Today's risk</h2><span class="muted small">Recoloured on every change</span></div>
      <div id="live-map" ref="mapEl"></div>
      <div class="legend small">
        <span><i :style="{ '--c': LEVEL_COLORS.Low }"></i>Low</span>
        <span><i :style="{ '--c': LEVEL_COLORS.Moderate }"></i>Moderate</span>
        <span><i :style="{ '--c': LEVEL_COLORS.High }"></i>High</span>
        <span><i :style="{ '--c': NO_DATA }"></i>No data</span>
      </div>
      <h3>Data pipeline</h3>
      <table class="kv">
        <tbody v-if="status">
          <tr><th>Forecast</th><td>
            <template v-if="status.freshness.last_success"><span class="dot" :class="status.freshness.status"></span>{{ status.freshness.status }} ·
              {{ status.freshness.age_hours }} h old · {{ status.freshness.source }}<b v-if="status.refresh_running"> · refreshing…</b></template>
            <template v-else>never loaded</template></td></tr>
          <tr><th>Next forecast refresh</th><td>{{ countdown(status.next_runs.refresh) }}</td></tr>
          <tr><th>Ensemble (122 members)</th><td>
            <template v-if="status.ensemble_run_at">last run {{ new Date(status.ensemble_run_at).toLocaleString("en-IN") }}</template>
            <template v-else>not run yet</template><b v-if="status.ensemble_running"> · running…</b></td></tr>
          <tr><th>Next ensemble refresh</th><td>{{ countdown(status.next_runs.ensemble) }}</td></tr>
          <tr><th>Open dashboards</th><td>{{ status.listeners }} connected</td></tr>
        </tbody>
      </table>
      <div class="msg-actions">
        <button :disabled="!demo || status?.refresh_running" @click="refreshNow">Refresh forecast now</button>
        <button :disabled="!demo || status?.ensemble_running" @click="ensembleNow">Refresh ensemble now</button>
      </div>
    </section>

    <section class="card wide">
      <div class="card-head"><h2>Event feed</h2><span class="muted small">Newest first · streamed over Server-Sent Events</span></div>
      <ol class="feed">
        <li v-for="f in feed" :key="f.id" :class="[`k-${f.kind}`, { strong: f.strong }]">
          <time>{{ clock(f.at) }}</time><span class="icon" aria-hidden="true">{{ f.icon }}</span><span>{{ f.text }}</span>
        </li>
      </ol>
      <p v-if="!feed.length" class="muted small">No events yet. Start the virtual station or refresh the forecast.</p>
    </section>
  </main>
  <footer class="muted small">Simulated readings carry station ids starting "SIM". Until you reset them they change the risk
    shown for that district, so reset after every demonstration. A real station posts the same JSON to
    <code>POST /api/sensors/readings</code> (see README, Field sensors).</footer>
  </div>
</template>
