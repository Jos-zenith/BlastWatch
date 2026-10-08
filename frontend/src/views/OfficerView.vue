<script setup>
import { computed, markRaw, onBeforeUnmount, onMounted, ref, watch } from "vue";
import L from "leaflet";
import DayStrip from "../components/DayStrip.vue";
import ChartCanvas from "../components/ChartCanvas.vue";
import { api, post } from "../lib/api.js";
import { lang, localName, locale, T } from "../lib/i18n.js";
import { debounce, toast, useLive } from "../lib/live.js";
import { ACTION_COLORS, dateTime, dayName, LEVEL_COLORS, todayIso } from "../lib/format.js";

const data = ref(null);
const alerts = ref([]);
const selected = ref(null);
const weather = ref(null);
const checks = ref([]);
const blockData = ref(null);
const selectedBlock = ref(null);
const blockMessage = ref(null);
const flash = ref(new Set());
const copied = ref("");
const busy = ref(false);
const loadError = ref(false);
// Optional details for a field check: with them an observation records place, variety, stage and severity.
const STAGES = ["vegetative", "booting", "heading", "ripening"];
const emptyCheck = () => ({ block: "", variety: "", crop_stage: "", severity_ses: "" });
const checkForm = ref(emptyCheck());

const mapEl = ref(null);
const detailEl = ref(null);
let map = null;
let districtLayer = null;
let blockLayer = null;
let fitted = false;

const district = computed(() => data.value?.districts.find((d) => d.id === selected.value) ?? null);
const counts = computed(() => {
  const c = { alert: 0, watch: 0, none: 0, unknown: 0 };
  data.value?.districts.forEach((d) => c[d.action]++);
  return c;
});
const freshness = computed(() => {
  const f = data.value?.freshness;
  if (!f) return { cls: loadError.value ? "expired" : "fresh", text: loadError.value ? T("never") : "…" };
  if (!f.last_success) return { cls: f.status, text: T("never") };
  if (f.status === "fresh") {
    const text = f.age_hours < 1 ? T("fresh_min")(Math.max(1, Math.round(f.age_hours * 60)), f.source)
      : T("fresh")(Math.round(f.age_hours), f.source);
    return { cls: f.status, text };
  }
  if (f.status === "stale") return { cls: f.status, text: T("stale")(Math.round(f.age_hours)) };
  return { cls: f.status, text: T("expired") };
});
// The "why" card explains the riskiest of the next 3 days (the earliest one on a tie).
const focusDay = computed(() => (district.value?.days || []).filter((x) => x.horizon === "near")
  .reduce((best, x) => (best == null || x.score > best.score ? x : best), null));
const confidence = computed(() => {
  const c = focusDay.value?.confidence;
  if (!c) return { level: null, text: T("conf_none") };
  const text = { stale: T("conf_stale"), lead: T("conf_lead"), no_ensemble: T("conf_none"),
    agreement: T("conf_agree")(Math.round((c.agree ?? 0) * 100), focusDay.value.ens_members) }[c.reason];
  return { level: c.level, text };
});
const cropLine = computed(() => {
  const f = district.value?.farmers;
  return f?.subscribers ? T("why_crop")(f.susceptible, f.subscribers) : T("why_crop_none");
});
const bases = computed(() => [...new Set((district.value?.days || []).map((x) => x.wetness_basis).filter(Boolean))]);
const ruleLine = computed(() => {
  const d = district.value;
  if (!d) return "";
  const names = { hq: T("rule_hq"), blocks_half: T("rule_blocks") };
  const other = d.rule_used === "hq" ? "blocks_half" : "hq";
  const otherAction = d.rule_actions?.[other];
  if (otherAction == null) return `${names[d.rule_used]}. ${T("rule_no_blocks")}`;
  return T("rule_line")(names[d.rule_used], names[other], T("action_" + otherAction));
});
// Only the district's current episode: a "ready" message is regenerated with every refresh and an
// out-of-date one is expired by the server, so nothing here can show days that have passed.
const districtAlert = computed(() => {
  const d = district.value;
  return d?.alert && d.alert.status !== "expired" ? alerts.value.find((a) => a.id === d.alert.id) ?? null : null;
});
const blockDates = computed(() =>
  blockData.value?.blocks.find((b) => b.days.length)?.days.slice(0, 3).map((x) => x.date) ?? []);
const blockName = computed(() => {
  const b = blockData.value?.blocks.find((x) => x.id === selectedBlock.value);
  return b ? localName(b) : "";
});
const chartConfig = computed(() => {
  const hours = weather.value?.hours;
  if (!hours) return null;
  lang.value; // relabel on language change
  return {
    type: "line",
    data: {
      labels: hours.map((h) => new Date(h.ts).toLocaleString(locale(), { weekday: "short", hour: "numeric" })),
      datasets: [
        { label: T("chart_lwp"), data: hours.map((h) => (h.leaf_wet_min != null ? h.leaf_wet_min / 0.6 : h.leaf_wet_prob)),
          borderColor: "#3b82c4", backgroundColor: "#3b82c433", fill: true, pointRadius: 0, borderWidth: 1.5, yAxisID: "w" },
        { label: T("chart_temp"), data: hours.map((h) => h.temp_c), borderColor: "#d9822b", pointRadius: 0, borderWidth: 1.5, yAxisID: "t" },
      ],
    },
    options: {
      maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      scales: { x: { ticks: { maxTicksLimit: 7 } }, w: { min: 0, max: 100, position: "left" },
                t: { position: "right", grid: { drawOnChartArea: false } } },
    },
  };
});

// ---- Data ----------------------------------------------------------------------------------

async function load() {
  const [outlook, alertList] = await Promise.all([api("/api/outlook"), api("/api/alerts")]);
  data.value = outlook;
  alerts.value = alertList;
  loadError.value = false;
  if (selected.value == null && outlook.districts.length) selected.value = outlook.districts[0].id;
  if (selected.value != null) await loadDistrict(selected.value);
}

async function loadDistrict(id) {
  const [w, c, b] = await Promise.all([
    api(`/api/districts/${id}/weather?hours_back=24`),
    api(`/api/districts/${id}/field-checks`),
    api(`/api/districts/${id}/blocks`),
  ]);
  if (selected.value !== id) return;
  weather.value = w;
  checks.value = c;
  blockData.value = b;
  if (selectedBlock.value != null && !b.blocks.some((x) => x.id === selectedBlock.value)) selectedBlock.value = null;
  if (selectedBlock.value != null) loadBlockMessage(selectedBlock.value);
}

async function selectDistrict(id, fromList = false) {
  if (selected.value !== id) {
    selected.value = id;
    weather.value = null;
    checks.value = [];
    blockData.value = null;
    selectedBlock.value = null;
    blockMessage.value = null;
    checkForm.value = emptyCheck();
  }
  // On phones the detail sits below the list; bring it into view after a tap.
  if (fromList && window.matchMedia("(max-width: 900px)").matches) {
    detailEl.value?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  await loadDistrict(id);
  if (fromList && blockData.value?.blocks.length) {
    map.fitBounds(L.latLngBounds(blockData.value.blocks.map((b) => [b.lat, b.lon])), { padding: [30, 30], maxZoom: 10 });
  }
}

async function selectBlock(id) {
  selectedBlock.value = selectedBlock.value === id ? null : id;
  blockMessage.value = null;
  if (selectedBlock.value != null) await loadBlockMessage(selectedBlock.value);
}

async function loadBlockMessage(id) {
  const msg = await api(`/api/blocks/${id}/message`);
  if (selectedBlock.value === id) blockMessage.value = msg;
}

async function copy(text, key) {
  try {
    await navigator.clipboard.writeText(text);
    copied.value = key;
    setTimeout(() => { if (copied.value === key) copied.value = ""; }, 2000);
  } catch { /* clipboard blocked; the text is selectable */ }
}

async function markSent(id) {
  await post(`/api/alerts/${id}/sent`);
  alerts.value = await api("/api/alerts");
}

async function recordCheck(found) {
  const d = district.value;
  busy.value = true;
  try {
    const f = checkForm.value;
    await post(`/api/districts/${d.id}/field-checks`, {
      blast_found: found, alert_id: districtAlert.value?.id ?? null,
      block: f.block || null, variety: f.variety || null, crop_stage: f.crop_stage || null,
      severity_ses: found && f.severity_ses !== "" ? Number(f.severity_ses) : null,
    });
    checkForm.value = emptyCheck();
    checks.value = await api(`/api/districts/${d.id}/field-checks`);
  } finally { busy.value = false; }
}

// ---- Map ------------------------------------------------------------------------------------

function drawDistricts() {
  if (!map || !data.value) return;
  districtLayer.clearLayers();
  data.value.districts.forEach((d) => {
    L.circleMarker([d.lat, d.lon], {
      radius: d.id === selected.value ? 13 : 9,
      color: ACTION_COLORS[d.action], fillColor: ACTION_COLORS[d.action], fillOpacity: 0.6, weight: 2,
    }).bindTooltip(`${localName(d)}: ${T("action_" + d.action)}`).on("click", () => selectDistrict(d.id)).addTo(districtLayer);
  });
  // A fixed centre left edge districts outside the drawn area; fit them all once.
  if (!fitted && data.value.districts.length) {
    map.invalidateSize();
    map.fitBounds(L.latLngBounds(data.value.districts.map((d) => [d.lat, d.lon])), { padding: [20, 20] });
    fitted = true;
  }
}

function drawBlocks() {
  if (!map) return;
  blockLayer.clearLayers();
  blockData.value?.blocks.forEach((b) => {
    if (!b.max_level) return;
    const on = b.id === selectedBlock.value;
    L.circleMarker([b.lat, b.lon], {
      radius: on ? 8 : 5, weight: on ? 3 : 1.5, color: on ? "#111" : "#fff",
      fillColor: LEVEL_COLORS[b.max_level], fillOpacity: 0.9,
    }).bindTooltip(`${localName(b)}: ${T("level_" + b.max_level)} (${dayName(b.max_date)}, ${Math.round(b.max_score)})`)
      .on("click", () => selectBlock(b.id)).addTo(blockLayer);
  });
}

watch([data, selected, lang], drawDistricts);
watch([blockData, selectedBlock, lang], drawBlocks);

// ---- Live updates -----------------------------------------------------------------------------

const reload = debounce(() => load().catch(console.error), 700);
const reloadSlow = debounce(() => load().catch(console.error), 1500);

useLive((event) => {
  const e = event.data || {};
  switch (event.kind) {
    case "risk.changed": {
      const today = todayIso();
      const near = (e.changes || []).filter((c) => c.date >= today);
      flash.value = new Set(near.map((c) => c.district_id));
      near.slice(0, 3).forEach((c) => toast(
        T("toast_change")(c.district, dayName(c.date), c.from && T("level_" + c.from), T("level_" + c.to), e.cause),
        c.to === "High" ? "bad" : "info"));
      setTimeout(() => { flash.value = new Set(); }, 4000);
      reload();
      break;
    }
    case "alerts.evaluated":
      if (e.created?.length) toast(T("toast_alert")(e.created.join(", ")), "bad");
      reload();
      break;
    case "refresh.done":
      toast(T("toast_refresh"));
      reload();
      break;
    case "ensemble.updated":
    case "blocks.updated":
    case "sim.reset":
      reload();
      break;
    case "sensor.reading":
      if (e.district_id === selected.value) reloadSlow();
      break;
    default:
  }
});

let timer;
onMounted(() => {
  map = markRaw(L.map(mapEl.value, { scrollWheelZoom: false }).setView([10.9, 78.9], 7));
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 12, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  districtLayer = markRaw(L.layerGroup().addTo(map));
  blockLayer = markRaw(L.layerGroup().addTo(map));
  load().catch((err) => { console.error(err); loadError.value = true; });
  timer = setInterval(() => load().catch(console.error), 15 * 60 * 1000);
});
onBeforeUnmount(() => { clearInterval(timer); map?.remove(); });
</script>

<template>
  <div>
    <div id="freshness" class="banner" :class="freshness.cls" role="status">{{ freshness.text }}</div>

    <main>
      <section class="card">
        <div class="summary">
          <span v-for="a in ['alert', 'watch', 'none']" :key="a" class="pill" :style="{ '--c': ACTION_COLORS[a] }">
            <b>{{ counts[a] }}</b> {{ T("summary_" + a) }}
          </span>
        </div>
        <TransitionGroup tag="ul" name="list" class="actions">
          <li v-for="d in data?.districts || []" :key="d.id">
            <button class="action-row" :class="{ sel: d.id === selected, flash: flash.has(d.id) }"
                    @click="selectDistrict(d.id, true)">
              <span class="row-head">
                <span class="name">{{ localName(d) }}</span>
                <span class="badge" :style="{ '--c': ACTION_COLORS[d.action] }">{{ T("action_" + d.action) }}</span>
              </span>
              <DayStrip :days="d.days" />
            </button>
          </li>
        </TransitionGroup>
        <p v-if="!data" class="skeleton">…</p>
      </section>

      <section ref="detailEl" class="card detail-card">
        <div id="map" ref="mapEl"></div>
        <div v-if="district" id="detail">
          <div class="card-head">
            <h2>{{ localName(district) }}</h2>
            <span class="badge" :style="{ '--c': ACTION_COLORS[district.action] }">{{ T("action_" + district.action) }}</span>
          </div>
          <p v-if="!focusDay">{{ T("todo_" + district.action) }}</p>
          <div v-else class="why-card" :style="{ '--c': LEVEL_COLORS[focusDay.level] }">
            <div class="why-head">
              <span class="muted small">{{ T("why_title") }} · {{ dayName(focusDay.date) }}</span>
              <b class="why-level">{{ T("level_" + focusDay.level) }}</b>
              <span>{{ T("why_conf") }}: <b>{{ confidence.level ? T("level_" + confidence.level) : "–" }}</b>
                <span class="muted small"> ({{ confidence.text }})</span></span>
              <span class="muted small">{{ T("conf_hint") }}</span>
            </div>
            <h4>{{ T("why_because") }}</h4>
            <ul class="small">
              <li>{{ T("why_run_line")(focusDay.longest_wet_run) }}</li>
              <li>{{ T("why_hours_line")(focusDay.leaf_wet_hours) }}</li>
              <li v-if="focusDay.mean_temp_c != null">{{ T("why_temp_line")(focusDay.mean_temp_c) }}</li>
              <li>{{ T("why_rain_line")(focusDay.rain_mm) }}</li>
              <li v-if="focusDay.mean_cloud_pct != null">{{ T("why_cloud_line")(focusDay.mean_cloud_pct) }}</li>
              <li v-if="focusDay.wetness_basis">{{ T("why_basis_line")(T("basis")[focusDay.wetness_basis] || focusDay.wetness_basis) }}</li>
              <li>{{ cropLine }}</li>
            </ul>
            <dl class="small why-meta">
              <dt>{{ T("why_data") }}</dt><dd>{{ freshness.text }}</dd>
              <dt>{{ T("why_evidence") }}</dt><dd>{{ T("why_evidence_text")(data.model_version) }}</dd>
            </dl>
            <p class="why-warn small">⚠ {{ T("why_warning") }}</p>
            <p class="why-action"><b>{{ T("why_action") }}:</b> {{ T("todo_" + district.action) }}</p>
          </div>
          <DayStrip :days="district.days" big />
          <p class="muted small">
            <template v-if="bases.length">{{ T("based_on") }}: {{ bases.map((b) => T("basis")[b] || b).join(", ") }}</template>
          </p>
          <p class="muted small rule-line">{{ ruleLine }}</p>

          <h3>{{ T("blocks_title") }}</h3>
          <p class="muted small">{{ T("blocks_hint") }}</p>
          <div v-if="blockData" class="table-scroll">
            <p v-if="!blockData.fresh" class="banner stale small">{{ T("blocks_stale") }}</p>
            <p v-if="!blockData.blocks.length" class="muted small">{{ T("blocks_none") }}</p>
            <table v-else class="blocks small">
              <thead>
                <tr>
                  <th>{{ T("block") }}</th>
                  <th v-for="x in blockDates" :key="x">{{ dayName(x) }}</th>
                  <th class="num">{{ T("col_block_max") }}</th>
                  <th class="num">{{ T("col_wet_run") }}</th>
                  <th class="num">{{ T("col_farmers") }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="b in blockData.blocks" :key="b.id" :class="{ sel: b.id === selectedBlock }" @click="selectBlock(b.id)">
                  <th scope="row">{{ localName(b) }}</th>
                  <td v-for="x in blockDates" :key="x">
                    <template v-for="day in [b.days.find((y) => y.date === x)]" :key="x">
                      <span v-if="day" class="chip" :style="{ '--c': LEVEL_COLORS[day.level] }"
                            :title="`${T('level_' + day.level)} · ${T('score')} ${Math.round(day.score)} · ${T('why_run')(day.longest_wet_run)}`">
                        {{ Math.round(day.score) }}</span>
                      <span v-else>–</span>
                    </template>
                  </td>
                  <td class="num">
                    <span v-if="b.max_level" class="badge" :style="{ '--c': LEVEL_COLORS[b.max_level] }">{{ T("level_" + b.max_level) }}</span>
                  </td>
                  <td class="num">{{ b.longest_wet_run != null ? `${b.longest_wet_run} h` : "–" }}</td>
                  <td class="num">{{ b.farmers }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <ChartCanvas :config="chartConfig" />

          <h3>{{ T("messages") }}</h3>
          <p class="muted small">
            {{ T("farmers_line")(district.farmers?.subscribers || 0, district.farmers?.susceptible || 0, district.farmers?.unverified_reports || 0) }}
          </p>
          <div v-if="districtAlert" class="message">
            <p class="muted small">{{ T("generated")(dateTime(districtAlert.generated_at)) }}</p>
            <p lang="ta">{{ districtAlert.message_ta }}</p>
            <p lang="en" class="muted">{{ districtAlert.message_en }}</p>
            <div class="msg-actions">
              <button @click="copy(districtAlert.message_ta, 'd-ta')">{{ copied === "d-ta" ? T("copied") : T("copy") }} (தமிழ்)</button>
              <button @click="copy(districtAlert.message_en, 'd-en')">{{ copied === "d-en" ? T("copied") : T("copy") }} (English)</button>
              <span v-if="districtAlert.status === 'sent'" class="muted small">
                {{ T("sent") }} {{ new Date(districtAlert.sent_at).toLocaleString(locale()) }}</span>
              <button v-else data-sent @click="markSent(districtAlert.id)">{{ T("mark_sent") }}</button>
            </div>
          </div>
          <p v-else class="muted">{{ T("no_message") }}</p>

          <template v-if="selectedBlock != null">
            <h3>{{ T("block_msg_title")(blockName) }}</h3>
            <div v-if="blockMessage?.message" class="message">
              <p lang="ta">{{ blockMessage.message.ta }}</p>
              <p lang="en" class="muted">{{ blockMessage.message.en }}</p>
              <div class="msg-actions">
                <button @click="copy(blockMessage.message.ta, 'b-ta')">{{ copied === "b-ta" ? T("copied") : T("copy") }} (தமிழ்)</button>
                <button @click="copy(blockMessage.message.en, 'b-en')">{{ copied === "b-en" ? T("copied") : T("copy") }} (English)</button>
              </div>
            </div>
            <p v-else-if="blockMessage" class="muted small">{{ T("block_msg_none")(blockName, blockMessage.reason) }}</p>
          </template>
          <p v-else-if="blockData?.blocks.length" class="muted small">{{ T("block_msg_hint") }}</p>

          <h3>{{ T("field_check") }}</h3>
          <p class="muted small">{{ T("field_check_hint") }}</p>
          <p class="muted small">{{ T("check_optional") }}</p>
          <div class="check-form small">
            <label>{{ T("block") }}
              <select v-model="checkForm.block">
                <option value="">–</option>
                <option v-for="b in blockData?.blocks || []" :key="b.id" :value="b.name">{{ localName(b) }}</option>
              </select></label>
            <label>{{ T("check_variety") }} <input v-model.trim="checkForm.variety" maxlength="80"></label>
            <label>{{ T("check_stage") }}
              <select v-model="checkForm.crop_stage">
                <option value="">–</option>
                <option v-for="s in STAGES" :key="s" :value="s">{{ T("stage")[s] }}</option>
              </select></label>
            <label>{{ T("check_ses") }}
              <select v-model="checkForm.severity_ses">
                <option value="">–</option>
                <option v-for="n in 10" :key="n" :value="n - 1">{{ n - 1 }}</option>
              </select></label>
          </div>
          <div class="msg-actions">
            <button :disabled="busy" @click="recordCheck(true)">{{ T("found_yes") }}</button>
            <button :disabled="busy" @click="recordCheck(false)">{{ T("found_no") }}</button>
          </div>
          <ul class="small">
            <li v-for="c in checks.slice(0, 5)" :key="c.id">
              {{ dayName(c.checked_on) }}: <b>{{ T(c.blast_found ? "found_yes" : "found_no") }}</b>
              <span v-if="c.alert_id" class="muted"> ({{ T("after_alert") }})</span>
            </li>
          </ul>
        </div>
      </section>
    </main>

    <footer class="muted small">{{ T("footer") }}</footer>
  </div>
</template>
