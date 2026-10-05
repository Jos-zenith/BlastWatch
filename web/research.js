const DEFAULT_AREAS = ["India", "China, mainland", "Bangladesh", "Indonesia", "Viet Nam"];
const METRIC_SCALE = { production_t: 1e6, area_ha: 1e6, yield_kg_ha: 1000 };

const state = { areas: new Set(DEFAULT_AREAS), charts: {} };

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

function chart(id, config) {
  state.charts[id]?.destroy();
  state.charts[id] = new Chart($(id), config);
}

async function loadAreas() {
  const all = await api("/api/production/areas");
  const preferred = [...DEFAULT_AREAS, "Thailand", "Myanmar", "Pakistan", "Philippines", "Japan", "Asia"];
  const shown = preferred.filter((a) => all.includes(a));
  $("areas").innerHTML = shown.map((a) => `<button data-area="${esc(a)}" class="${state.areas.has(a) ? "on" : ""}">${esc(a)}</button>`).join("");
  $("areas").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    const a = b.dataset.area;
    state.areas.has(a) ? state.areas.delete(a) : state.areas.add(a);
    b.classList.toggle("on");
    loadProduction();
  }));
}

async function loadProduction() {
  const metric = $("metric").value;
  const params = new URLSearchParams({ start: $("from-year").value });
  state.areas.forEach((a) => params.append("areas", a));
  if (!state.areas.size) return;
  const data = await api(`/api/production?${params}`);
  const palette = ["#2f6f4f", "#d14343", "#3b82c4", "#d99a1b", "#8a5cc2", "#4aa3a2", "#a0522d", "#6b7280", "#c2185b", "#1f2937", "#7cb342"];
  const years = [...new Set(Object.values(data.series).flat().map((r) => r.year))].sort();
  chart("prod-chart", {
    type: "line",
    data: {
      labels: years,
      datasets: Object.entries(data.series).map(([area, rows], i) => {
        const byYear = Object.fromEntries(rows.map((r) => [r.year, r[metric]]));
        return {
          label: area,
          data: years.map((y) => (byYear[y] == null ? null : byYear[y] / METRIC_SCALE[metric])),
          borderColor: palette[i % palette.length],
          borderWidth: area === "India" ? 3 : 1.5,
          pointRadius: 0,
          spanGaps: true,
        };
      }),
    },
    options: { maintainAspectRatio: false, interaction: { mode: "index", intersect: false } },
  });
  const india = data.series["India"]?.at(-1);
  $("prod-note").textContent = india
    ? `India ${india.year}: ${(india.production_t / 1e6).toFixed(1)} Mt paddy from ${(india.area_ha / 1e6).toFixed(1)} Mha at ${(india.yield_kg_ha / 1000).toFixed(2)} t/ha. Source: ${data.source}.`
    : "";
}

async function loadGenes() {
  const genes = await api("/api/genes");
  $("genes").innerHTML = genes.map((g) => `
    <tr data-symbol="${esc(g.symbol)}">
      <td><b>${esc(g.symbol)}</b></td>
      <td><i>${esc(g.organism)}</i></td>
      <td>${esc(g.role.replace("-", " "))}</td>
      <td>${esc(g.description)}</td>
      <td class="num">${g.ncbi_hits ?? "—"}</td>
    </tr>`).join("");
  $("genes").querySelectorAll("tr").forEach((tr) => tr.addEventListener("click", () => showGene(tr.dataset.symbol)));
}

async function showGene(symbol) {
  const g = await api(`/api/genes/${encodeURIComponent(symbol)}`);
  document.querySelectorAll("#genes tr").forEach((tr) => tr.classList.toggle("sel", tr.dataset.symbol === symbol));
  $("gene-records").innerHTML = `
    <h3>${esc(g.symbol)}: ${g.records.length} cached records (of ${g.ncbi_hits ?? "?"} in GenBank)</h3>
    <ul>${g.records.map((r) => `<li><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.accession)}</a>
      ${esc(r.title)} <span class="muted">(${r.length ?? "?"} bp)</span></li>`).join("")}</ul>`;
}

async function loadHealth() {
  const h = await api("/api/health");
  const rows = Object.entries(h.sources).sort().map(([source, s]) => `
    <tr><td>${esc(source)}</td>
      <td>${s.last_ok ? esc(new Date(s.last_ok).toLocaleString()) : "—"}</td>
      <td>${s.last_failed ? esc(new Date(s.last_failed).toLocaleString()) : "—"}</td></tr>`).join("");
  $("sources").innerHTML = rows || `<tr><td colspan="3" class="muted">No ingest runs yet.</td></tr>`;
  $("model").textContent = `${h.model_version} · forecast ${h.forecast.status}`;
}

async function main() {
  $("metric").addEventListener("change", loadProduction);
  $("from-year").addEventListener("change", loadProduction);
  try {
    await Promise.all([loadAreas().then(loadProduction), loadGenes(), loadHealth()]);
  } catch (err) {
    console.error(err);
  }
}

main();
