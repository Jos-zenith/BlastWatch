<script setup>
import { computed, onMounted, reactive, ref, watch } from "vue";
import ChartCanvas from "../components/ChartCanvas.vue";
import { api } from "../lib/api.js";
import { useLive } from "../lib/live.js";

const DEFAULT_AREAS = ["India", "China, mainland", "Bangladesh", "Indonesia", "Viet Nam"];
const PREFERRED = [...DEFAULT_AREAS, "Thailand", "Myanmar", "Pakistan", "Philippines", "Japan", "Asia"];
const METRIC_SCALE = { production_t: 1e6, area_ha: 1e6, yield_kg_ha: 1000 };
const PALETTE = ["#2f6f4f", "#d14343", "#3b82c4", "#d99a1b", "#8a5cc2", "#4aa3a2", "#a0522d", "#6b7280", "#c2185b", "#1f2937", "#7cb342"];
const KIND_LABELS = {
  "pilot district": "Rice blast named in a pilot district",
  "outside pilot": "Named districts outside the pilot",
  "statewide, no district named": "Statewide statement, no district",
  "no rice blast observation reported": "No rice blast observation",
};

const validation = ref(null);
const compare = ref(null);
const compareDays = ref(30);
const health = ref(null);
const genes = ref([]);
const gene = ref(null);
const areas = ref([]);
const production = ref(null);
const prod = reactive({ metric: "production_t", from: "1990", chosen: new Set(DEFAULT_AREAS) });

const progress = (have, need) => `${Math.min(100, Math.round((100 * have) / need))}%`;

const prodConfig = computed(() => {
  const data = production.value;
  if (!data) return null;
  const years = [...new Set(Object.values(data.series).flat().map((r) => r.year))].sort();
  return {
    type: "line",
    data: {
      labels: years,
      datasets: Object.entries(data.series).map(([area, rows], i) => {
        const byYear = Object.fromEntries(rows.map((r) => [r.year, r[prod.metric]]));
        return { label: area, data: years.map((y) => (byYear[y] == null ? null : byYear[y] / METRIC_SCALE[prod.metric])),
                 borderColor: PALETTE[i % PALETTE.length], borderWidth: area === "India" ? 3 : 1.5, pointRadius: 0, spanGaps: true };
      }),
    },
    options: { maintainAspectRatio: false, interaction: { mode: "index", intersect: false } },
  };
});
const prodNote = computed(() => {
  const india = production.value?.series.India?.at(-1);
  return india ? `India ${india.year}: ${(india.production_t / 1e6).toFixed(1)} Mt paddy from ${(india.area_ha / 1e6).toFixed(1)} Mha at ${
    (india.yield_kg_ha / 1000).toFixed(2)} t/ha. Source: ${production.value.source}.` : "";
});

async function loadProduction() {
  if (!prod.chosen.size) { production.value = null; return; }
  const params = new URLSearchParams({ start: prod.from });
  prod.chosen.forEach((a) => params.append("areas", a));
  production.value = await api(`/api/production?${params}`);
}
function toggleArea(a) {
  prod.chosen.has(a) ? prod.chosen.delete(a) : prod.chosen.add(a);
  loadProduction().catch(console.error);
}
watch(() => [prod.metric, prod.from], () => loadProduction().catch(console.error));

const loadCompare = () => api(`/api/rules/compare?days=${compareDays.value}`).then((c) => { compare.value = c; });
watch(compareDays, () => loadCompare().catch(console.error));
const loadHealth = () => api("/api/health").then((h) => { health.value = h; });
async function showGene(symbol) { gene.value = await api(`/api/genes/${encodeURIComponent(symbol)}`); }

useLive((e) => {
  if (["refresh.done", "ensemble.updated"].includes(e.kind)) loadHealth().catch(console.error);
  if (e.kind === "blocks.updated") loadCompare().catch(console.error);
});

onMounted(async () => {
  const tasks = [
    api("/api/validation").then((v) => { validation.value = v; }),
    loadCompare(), loadHealth(),
    api("/api/genes").then((g) => { genes.value = g; }),
    api("/api/production/areas").then((all) => { areas.value = PREFERRED.filter((a) => all.includes(a)); }).then(loadProduction),
  ];
  (await Promise.allSettled(tasks)).filter((r) => r.status === "rejected").forEach((r) => console.error(r.reason));
});
</script>

<template>
  <main>
    <section v-if="validation" class="card wide validation">
      <div class="card-head"><h2>Validation status</h2><span class="muted small">Pre-registered criteria: config/eval_criteria.toml</span></div>
      <p class="lede">The risk rules are <b>not yet validated against outbreaks</b>. The evaluation needs labelled field observations,
        both blast found and blast not found, before it can report precision and recall. These are the minimums it was set
        to require before seeing any results.</p>
      <div class="tiles">
        <div class="tile">
          <span class="muted small">Blast found (observations)</span>
          <b>{{ validation.observations.present }} / {{ validation.required.present }}</b>
          <div class="progress"><i :style="{ width: progress(validation.observations.present, validation.required.present) }"></i></div>
        </div>
        <div class="tile">
          <span class="muted small">Blast not found</span>
          <b>{{ validation.observations.absent }} / {{ validation.required.absent }}</b>
          <div class="progress"><i :style="{ width: progress(validation.observations.absent, validation.required.absent) }"></i></div>
        </div>
        <div class="tile">
          <span class="muted small">Districts with blast found</span>
          <b>{{ validation.observations.districts_with_present }} / {{ validation.required.districts_with_present }}</b>
        </div>
        <div class="tile">
          <span class="muted small">Field checks · confirmed farmer reports</span>
          <b>{{ validation.observations.from_field_checks }} · {{ validation.observations.from_farmer_reports }}</b>
        </div>
        <div class="tile">
          <span class="muted small">Complete records (block, variety, stage, severity)</span>
          <b>{{ validation.observations.complete }} / {{ validation.observations.present + validation.observations.absent }}</b>
        </div>
      </div>
      <p class="small muted">Presence and absence alone can test the weather rules. Learning how variety and crop stage
        change risk needs complete records: {{ validation.observations.with_block }} have a block,
        {{ validation.observations.with_variety }} a variety, {{ validation.observations.with_stage }} a crop stage, and
        {{ validation.observations.with_severity }} of {{ validation.observations.present }} positives a severity score.
        Until then this is an environmental risk score, not a validated disease model.</p>

      <h3>Public surveillance records: audited</h3>
      <p class="small">We looked for outbreak records to backtest against. {{ validation.audit.source }},
        {{ validation.audit.reports }} monthly reports, {{ validation.audit.first }} to {{ validation.audit.last }}:</p>
      <ul class="small">
        <li v-for="(n, kind) in validation.audit.by_kind" :key="kind"><b>{{ n }}</b> · {{ KIND_LABELS[kind] || kind }}</li>
      </ul>
      <p class="small">Only one report names rice blast in a pilot district, by month and together with two other diseases.
        Public records cannot support a precision/recall backtest, which is why every field check and verified farmer report is
        stored as a labelled observation. A presence-only backtest would also mislead: with High on about 1 night in 5, a
        12-day lead window would contain a High night about 90 % of the time by chance if nights were independent
        (somewhat less, since wet nights cluster), so "most outbreaks were preceded by a High night" proves little.</p>
      <details>
        <summary class="small">Every report that mentions rice blast ({{ validation.audit.records.length }})</summary>
        <div class="table-scroll">
          <table class="small audit">
            <thead><tr><th>Month</th><th>Statement (verbatim)</th><th>Usable for the pilot</th></tr></thead>
            <tbody>
              <tr v-for="r in validation.audit.records" :key="r.month">
                <td><a :href="r.report_url" target="_blank" rel="noopener">{{ r.month }}</a></td>
                <td>{{ r.rice_blast_statement }}</td><td>{{ r.usable_for_pilot }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </details>
    </section>

    <section class="card wide">
      <div class="card-head">
        <h2>District alert rule: A vs B</h2>
        <div class="controls">
          <select v-model="compareDays" aria-label="Period">
            <option :value="7">Last 7 days + forecast</option><option :value="30">Last 30 days + forecast</option>
            <option :value="120">Last 120 days + forecast</option>
          </select>
        </div>
      </div>
      <p class="muted small">Rule A rates a district from its headquarters point (rules-v3 was tuned on this; it decides alerts
        by default). Rule B rates it High when at least {{ compare ? Math.round(compare.block_share * 100) : 50 }} % of its blocks are
        High. Every district-day with scored blocks is compared, past days (their last, near-analysis score) and the coming
        7-day forecast; the history fills in as the season runs. Switch with
        <code>[alerts] district_rule</code> in risk_rules.toml.</p>
      <div v-if="compare" class="table-scroll">
        <table class="small">
          <thead><tr><th>District</th><th class="num">Days</th><th class="num">A High</th><th class="num">B High</th>
            <th class="num">Both High</th><th class="num">Same level</th></tr></thead>
          <tbody>
            <tr v-for="d in [...compare.districts, { district: 'All districts', ...compare.total }]" :key="d.district"
                :style="d.district === 'All districts' ? 'font-weight:600' : ''">
              <td>{{ d.district }}</td><td class="num">{{ d.days }}</td><td class="num">{{ d.hq_high }}</td>
              <td class="num">{{ d.blocks_high }}</td><td class="num">{{ d.both_high }}</td>
              <td class="num">{{ d.days ? `${Math.round((100 * d.agree) / d.days)}%` : "–" }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="!compare.total.days" class="muted small">No days with block data yet in this period.</p>
      </div>
    </section>

    <section class="card wide">
      <div class="card-head">
        <h2>Rice production context <span class="muted">· FAOSTAT · national totals, background only</span></h2>
        <div class="controls">
          <select v-model="prod.metric" aria-label="Metric">
            <option value="production_t">Production (Mt)</option>
            <option value="area_ha">Area harvested (Mha)</option>
            <option value="yield_kg_ha">Yield (t/ha)</option>
          </select>
          <select v-model="prod.from" aria-label="From year">
            <option value="1961">Since 1961</option><option value="1990">Since 1990</option><option value="2010">Since 2010</option>
          </select>
        </div>
      </div>
      <div class="chips">
        <button v-for="a in areas" :key="a" :class="{ on: prod.chosen.has(a) }" @click="toggleArea(a)">{{ a }}</button>
      </div>
      <ChartCanvas :config="prodConfig" height="320px" />
      <p class="muted small">{{ prodNote }}</p>
    </section>

    <section class="card wide">
      <div class="card-head"><h2>Resistance genes <span class="muted">· GenBank</span></h2></div>
      <p class="muted small">Record counts measure how much has been sequenced, not how well a gene resists local blast races.
        Field resistance depends on the variety and the pathogen population, and is not used by the warnings.</p>
      <table class="genes">
        <thead><tr><th>Gene</th><th>Organism</th><th>Role</th><th>Description</th><th class="num">GenBank records</th></tr></thead>
        <tbody>
          <tr v-for="g in genes" :key="g.symbol" :class="{ sel: gene?.symbol === g.symbol }" @click="showGene(g.symbol)">
            <td><b>{{ g.symbol }}</b></td><td><i>{{ g.organism }}</i></td><td>{{ g.role.replace("-", " ") }}</td>
            <td>{{ g.description }}</td><td class="num">{{ g.ncbi_hits ?? "—" }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="gene" id="gene-records">
        <h3>{{ gene.symbol }}: {{ gene.records.length }} cached records (of {{ gene.ncbi_hits ?? "?" }} in GenBank)</h3>
        <ul>
          <li v-for="r in gene.records" :key="r.accession">
            <a :href="r.url" target="_blank" rel="noopener">{{ r.accession }}</a> {{ r.title }}
            <span class="muted">({{ r.length ?? "?" }} bp)</span>
          </li>
        </ul>
      </div>
    </section>

    <section class="card wide">
      <div class="card-head"><h2>Data pipeline</h2>
        <span v-if="health" class="muted small">{{ health.model_version }} · forecast {{ health.forecast.status }}</span></div>
      <table>
        <thead><tr><th>Source</th><th>Last success</th><th>Last failure</th></tr></thead>
        <tbody v-if="health">
          <tr v-for="[source, s] in Object.entries(health.sources).sort()" :key="source">
            <td>{{ source }}</td>
            <td>{{ s.last_ok ? new Date(s.last_ok).toLocaleString() : "—" }}</td>
            <td>{{ s.last_failed ? new Date(s.last_failed).toLocaleString() : "—" }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </main>
</template>
