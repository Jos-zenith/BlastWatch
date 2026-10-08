<script setup>
import { computed, onMounted, ref } from "vue";
import ChartCanvas from "../components/ChartCanvas.vue";
import { api } from "../lib/api.js";
import { toast, useLive } from "../lib/live.js";
import { ACTION_COLORS, cssVar, LEVEL_COLORS, pct } from "../lib/format.js";

const ACTION_LABELS = { alert: "Alert", watch: "Watch", none: "No action", unknown: "No data" };
const MODEL_ORDER = ["ecmwf", "gefs", "icon"];
const MODEL_COLORS = { ecmwf: "--m-ecmwf", gefs: "--m-gefs", icon: "--m-icon" };
const dayLabel = (iso) => new Date(iso + "T00:00:00").toLocaleDateString("en-IN", { weekday: "short", day: "numeric" });

const data = ref(null);
const outlook = ref(null);
const selected = ref(null);
const error = ref("");

const ruleAction = (id) => outlook.value?.districts.find((d) => d.id === id)?.action ?? "unknown";
const district = computed(() => data.value?.districts.find((d) => d.id === selected.value) ?? null);
const age = computed(() => (data.value?.run_at ? (Date.now() - new Date(data.value.run_at)) / 3.6e6 : null));
const dates = computed(() => [...new Set((data.value?.districts || []).flatMap((d) => d.days.map((x) => x.date)))].sort().slice(0, 7));
const nearMax = (d) => Math.max(0, ...d.days.slice(0, 3).map((x) => x.p_high));
const rows = computed(() => [...(data.value?.districts || [])].sort((a, b) => nearMax(b) - nearMax(a) || a.name.localeCompare(b.name)));

// Nights where one centre finds High likely and another finds it unlikely.
const splitNights = (days) => days.filter((d) => {
  const ps = MODEL_ORDER.map((m) => d.models[m]?.p_high).filter((p) => p != null);
  return ps.length > 1 && Math.max(...ps) >= 0.5 && Math.min(...ps) < 0.1;
});

const stats = computed(() => {
  if (!data.value) return [];
  const ds = data.value.districts.filter((d) => d.days.length);
  const nights = ds.flatMap((d) => d.days);
  return [
    [ds.filter((d) => d.days.slice(0, data.value.alert_rule.horizon_days).some((x) => x.p_high >= 0.5)).length,
      "Districts with a likely High night", "P(High) ≥ 50 % in the next 3 nights"],
    [nights.filter((x) => x.level === "High" && x.p_high < 0.2).length, "Rule-based High, ensemble unconvinced", "High nights with P(High) < 20 %"],
    [nights.filter((x) => x.level !== "High" && x.level != null && x.p_high >= 0.5).length, "Ensemble High, rule-based not", "nights with P(High) ≥ 50 %"],
    [ds.reduce((n, d) => n + splitNights(d.days).length, 0), "Nights the centres split on", "one ≥ 50 %, another < 10 %"],
    [ds.filter((d) => d.ensemble_action && d.ensemble_action !== ruleAction(d.id)).length, "Districts where the actions differ", "rule-based vs ensemble rule"],
  ];
});

function tip(day) {
  const models = MODEL_ORDER.filter((m) => day.models[m]).map((m) => `${data.value.models[m].label}: ${pct(day.models[m].p_high)}`).join("\n");
  return `${dayLabel(day.date)}: P(High) ${pct(day.p_high)} of ${day.members} members\nRule-based: ${day.level ?? "no data"}${
    day.score != null ? ` (${Math.round(day.score)})` : ""}\nScore spread ${Math.round(day.score_p10)}–${Math.round(day.score_p90)}, median ${Math.round(day.score_p50)}\n${models}`;
}

const fanConfig = computed(() => {
  const d = district.value;
  if (!d?.days.length) return null;
  const muted = cssVar("--muted", "#64706a");
  return {
    type: "line",
    data: {
      labels: d.days.map((x) => dayLabel(x.date)),
      datasets: [
        { label: "10th–90th percentile", data: d.days.map((x) => x.score_p90), borderWidth: 0, pointRadius: 0,
          backgroundColor: "rgba(42,120,214,0.18)", fill: "+1" },
        { label: "10th percentile", data: d.days.map((x) => x.score_p10), borderWidth: 0, pointRadius: 0, fill: false },
        { label: "Ensemble median", data: d.days.map((x) => x.score_p50), borderColor: "#2a78d6", borderWidth: 2, pointRadius: 0 },
        { label: "Rule-based score", data: d.days.map((x) => x.score), showLine: false, pointRadius: 6, pointHoverRadius: 8,
          pointBackgroundColor: d.days.map((x) => LEVEL_COLORS[x.level] || muted), pointBorderColor: cssVar("--card", "#fff"),
          pointBorderWidth: 2, borderColor: muted },
        { label: "High (65)", data: d.days.map(() => data.value.levels.high), borderColor: LEVEL_COLORS.High,
          borderDash: [5, 4], borderWidth: 1, pointRadius: 0 },
        { label: "Moderate (35)", data: d.days.map(() => data.value.levels.moderate), borderColor: LEVEL_COLORS.Moderate,
          borderDash: [5, 4], borderWidth: 1, pointRadius: 0 },
      ],
    },
    options: {
      maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 12, filter: (item) => item.text !== "10th percentile" } },
        tooltip: { callbacks: { afterBody: (items) => {
          const x = d.days[items[0].dataIndex];
          return `P(High) ${pct(x.p_high)} of ${x.members} members`;
        } } },
      },
      scales: { y: { min: 0, max: 100, title: { display: true, text: "Risk score" } } },
    },
  };
});

const modelsConfig = computed(() => {
  const d = district.value;
  if (!d?.days.length) return null;
  return {
    type: "bar",
    data: {
      labels: d.days.map((x) => dayLabel(x.date)),
      datasets: MODEL_ORDER.map((m) => ({
        label: `${data.value.models[m].label} (${data.value.models[m].members})`,
        data: d.days.map((x) => (x.models[m] ? Math.round(x.models[m].p_high * 100) : null)),
        backgroundColor: cssVar(MODEL_COLORS[m], "#888"), borderRadius: 4, borderSkipped: "bottom",
        borderColor: cssVar("--card", "#fff"), borderWidth: { left: 1, right: 1 },
      })),
    },
    options: {
      maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { position: "bottom", labels: { boxWidth: 12 } },
                 tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${c.parsed.y}% High` } } },
      scales: { y: { min: 0, max: 100, title: { display: true, text: "P(High) %" } } },
    },
  };
});

async function load() {
  const [ens, out] = await Promise.all([api("/api/ensemble"), api("/api/outlook")]);
  data.value = ens;
  outlook.value = out;
  if (selected.value == null && ens.run_at) {
    selected.value = [...ens.districts].filter((d) => d.days.length).sort((a, b) => nearMax(b) - nearMax(a))[0]?.id ?? null;
  }
}

useLive((e) => {
  if (e.kind === "ensemble.updated") { toast("Ensemble updated"); load().catch(console.error); }
  if (e.kind === "risk.changed") load().catch(console.error);
});
onMounted(() => load().catch((err) => { console.error(err); error.value = err.message; }));
</script>

<template>
  <div>
    <div class="banner" :class="error ? 'expired' : !data ? 'fresh' : !data.run_at ? 'stale' : age < 13 ? 'fresh' : 'stale'" role="status">
      <template v-if="error">{{ error }}</template>
      <template v-else-if="!data">Loading…</template>
      <template v-else-if="!data.run_at">No ensemble run yet. Run <code>python -m blastwatch ensemble</code>, or start the server with
        --with-scheduler (first run after a minute).</template>
      <template v-else>Ensemble fetched {{ new Date(data.run_at).toLocaleString("en-IN") }} ({{ age < 1 ? "<1" : Math.round(age) }} h ago) ·
        {{ data.model_version }}</template>
    </div>

    <main>
      <section class="card wide">
        <div class="card-head">
          <h2>How sure is a High-risk night?</h2>
          <span v-if="data?.run_at" class="muted small">
            {{ Object.values(data.models).reduce((n, m) => n + m.members, 0) }} members per district</span>
        </div>
        <p class="lede">The officer view rates each night from one forecast. Leaf wetness is where weather models disagree most,
          so BlastWatch also scores <b>every member</b> of three global ensembles:
          <span class="model-key" style="--c:var(--m-ecmwf)">ECMWF IFS ENS · 51</span>,
          <span class="model-key" style="--c:var(--m-gefs)">NCEP GEFS · 31</span> and
          <span class="model-key" style="--c:var(--m-icon)">DWD ICON EPS · 40</span>.
          Each member is one plausible version of the coming nights. <b>P(High)</b> is the share of members whose night
          scores High, with the three centres weighted equally.</p>
        <div class="tiles">
          <div v-for="[value, label, note] in stats" :key="label" class="tile">
            <span class="muted small">{{ label }}</span><b>{{ value }}</b><span class="muted small">{{ note }}</span>
          </div>
        </div>
      </section>

      <section class="card wide">
        <div class="card-head">
          <h2>P(High) by district and night</h2>
          <div class="legend small">
            <span class="ramp" aria-hidden="true"></span><span>0 % → 100 % of members High</span>
            <span><i class="det" :style="{ '--c': LEVEL_COLORS.High }"></i>Rule-based level</span>
          </div>
        </div>
        <div class="table-scroll">
          <table v-if="data?.run_at" class="heat">
            <thead>
              <tr><th>District</th>
                <th v-for="(d, i) in dates" :key="d" :class="{ near: i < data.alert_rule.horizon_days }">{{ dayLabel(d) }}</th>
                <th>Rule-based</th><th>Ensemble</th></tr>
            </thead>
            <tbody>
              <tr v-for="d in rows" :key="d.id" :class="{ sel: d.id === selected }" @click="selected = d.id">
                <th scope="row">{{ d.name }}</th>
                <template v-for="date in dates" :key="date">
                  <template v-for="day in [d.days.find((x) => x.date === date)]" :key="date">
                    <td v-if="day" class="cell" :style="{ '--p': `${Math.round(day.p_high * 100)}%`, color: day.p_high >= 0.45 ? '#fff' : 'var(--text)' }"
                        :title="tip(day)">
                      <span>{{ pct(day.p_high) }}</span>
                      <i v-if="day.level" class="det" :style="{ '--c': LEVEL_COLORS[day.level] }" :aria-label="`rule-based ${day.level}`"></i>
                    </td>
                    <td v-else class="cell empty">–</td>
                  </template>
                </template>
                <td><span class="badge" :style="{ '--c': ACTION_COLORS[ruleAction(d.id)] }">{{ ACTION_LABELS[ruleAction(d.id)] }}</span></td>
                <td><span v-if="d.ensemble_action" class="badge" :style="{ '--c': ACTION_COLORS[d.ensemble_action] }">
                  {{ ACTION_LABELS[d.ensemble_action] }}</span><template v-else>–</template></td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="muted small">Rule-based action uses the live forecast and decides alerts. The ensemble action (≥ 2 of the next 3
          nights with P(High) ≥ {{ Math.round((data?.alert_p ?? 0.5) * 100) }} %) is experimental and shown only for comparison.
          Click a district for detail.</p>
      </section>

      <section class="card">
        <div class="card-head"><h2>Score spread<template v-if="district"> · {{ district.name }}</template></h2></div>
        <p class="muted small">Shaded: 10th–90th percentile of member scores across the three ensembles. Line: median. Dots: the
          rule-based score from the live forecast. Dashed: Moderate (35) and High (65) thresholds.</p>
        <ChartCanvas :config="fanConfig" height="320px" />
      </section>

      <section class="card">
        <div class="card-head"><h2>Do the centres agree?<template v-if="district"> · {{ district.name }}</template></h2></div>
        <p class="muted small">P(High) from each ensemble separately. When one centre says likely and another says no, the rating
          depends on which model you trust, and a scout visit is worth more than the forecast.</p>
        <ChartCanvas :config="modelsConfig" height="320px" />
        <details v-if="district">
          <summary class="small">Table view</summary>
          <div class="table-scroll">
            <table class="small">
              <thead><tr><th>Night</th><th v-for="m in MODEL_ORDER" :key="m" class="num">{{ data.models[m].label }}</th>
                <th class="num">Pooled</th><th>Rule-based</th></tr></thead>
              <tbody>
                <tr v-for="x in district.days" :key="x.date"><td>{{ dayLabel(x.date) }}</td>
                  <td v-for="m in MODEL_ORDER" :key="m" class="num">{{ x.models[m] ? pct(x.models[m].p_high) : "–" }}</td>
                  <td class="num"><b>{{ pct(x.p_high) }}</b></td><td>{{ x.level ?? "–" }}</td></tr>
              </tbody>
            </table>
          </div>
        </details>
      </section>

      <section class="card wide">
        <h2>What this adds, and what it does not</h2>
        <ul class="small">
          <li><b>Adds:</b> a probability for every night instead of one level, and a visible split between weather centres. A
            High night that only one centre supports is a weaker basis for an alert than one all three support.</li>
          <li><b>Does not add:</b> evidence that the warnings predict blast. The members are scored with the same uncalibrated
            rules; whether 70 % nights are followed by blast more often than 30 % nights needs field observations.</li>
          <li>The ensembles are coarser than the live forecast (roughly 25–40 km vs about 9 km) and their leaf-wetness estimates
            differ by centre, so systematic gaps between centres are partly model bias, not only weather uncertainty.</li>
          <li>Alerts still follow the rule-based forecast. The ensemble refreshes every 6 hours.</li>
        </ul>
      </section>
    </main>
  </div>
</template>
