const COLORS = { Low: "#2e9e5b", Moderate: "#d99a1b", High: "#d14343" };
const DEFAULT_AREAS = ["India", "China, mainland", "Bangladesh", "Indonesia", "Viet Nam"];
const METRIC_SCALE = { production_t: 1e6, area_ha: 1e6, yield_kg_ha: 1000 };

const state = { date: null, selected: null, areas: new Set(DEFAULT_AREAS), charts: {} };
let map, markers;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtDate = (iso) => new Date(iso + "T00:00:00").toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
const todayIso = () => new Date().toLocaleDateString("en-CA");

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

function chart(id, config) {
  state.charts[id]?.destroy();
  state.charts[id] = new Chart($(id), config);
}

function initMap() {
  map = L.map("map", { scrollWheelZoom: false }).setView([10.9, 78.9], 7);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 12,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  markers = L.layerGroup().addTo(map);
}

async function loadHealth() {
  const h = await api("/api/health");
  const t = h.last_weather_fetch ? new Date(h.last_weather_fetch).toLocaleString() : "never";
  $("freshness").textContent = `Weather updated: ${t} · model ${h.model_version}`;
}

async function loadRisk(date) {
  const data = await api(date ? `/api/risk?date=${date}` : "/api/risk");
  state.date = data.date;
  renderDates(data.available_dates);
  renderMap(data.districts);
  renderRanking(data.districts);
  // Open the detail panel on the most at-risk district so the page is never half empty.
  if (state.selected == null && data.districts.length) {
    const upcoming = await api(`/api/risk?date=${data.available_dates.at(-1)}`);
    const top = upcoming.districts[0] ?? data.districts[0];
    selectDistrict(top.id, top.name);
  }
}

function renderDates(dates) {
  const today = todayIso();
  $("dates").innerHTML = dates.map((d) => `
    <button role="tab" data-date="${d}" aria-selected="${d === state.date}" class="${d > today ? "fc" : ""}">
      ${d === today ? "Today" : fmtDate(d)}
    </button>`).join("");
  $("dates").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => loadRisk(b.dataset.date)));
}

function renderMap(rows) {
  markers.clearLayers();
  rows.forEach((d) => {
    L.circleMarker([d.lat, d.lon], {
      radius: 9 + d.score / 12,
      color: COLORS[d.level],
      fillColor: COLORS[d.level],
      fillOpacity: 0.55,
      weight: 2,
      dashArray: d.is_forecast ? "4 3" : null,
    })
      .bindTooltip(`<b>${esc(d.name)}</b><br>${d.level} (${d.score})<br>Wet run ${d.longest_wet_run} h · Rain ${d.rain_mm} mm`)
      .on("click", () => selectDistrict(d.id, d.name))
      .addTo(markers);
  });
}

function renderRanking(rows) {
  $("ranking").innerHTML = rows.map((d) => `
    <tr data-id="${d.id}" class="${d.id === state.selected ? "sel" : ""}">
      <td>${esc(d.name)}</td>
      <td><span class="lvl ${d.level}">${d.level}</span></td>
      <td class="num">${d.score}</td>
      <td class="num">${d.longest_wet_run}</td>
      <td class="num">${d.rain_mm}</td>
    </tr>`).join("");
  $("ranking").querySelectorAll("tr").forEach((tr) =>
    tr.addEventListener("click", () => selectDistrict(Number(tr.dataset.id), tr.cells[0].textContent)));
}

async function selectDistrict(id, name) {
  state.selected = id;
  document.querySelectorAll("#ranking tr").forEach((tr) => tr.classList.toggle("sel", Number(tr.dataset.id) === id));
  $("detail-title").textContent = name;
  $("detail-body").hidden = true;
  $("detail").hidden = false;

  const [risk, weather, advice] = await Promise.all([
    api(`/api/districts/${id}/risk`),
    api(`/api/districts/${id}/weather?hours_back=48`),
    api(`/api/districts/${id}/advice`),
  ]);

  chart("risk-chart", {
    type: "bar",
    data: {
      labels: risk.series.map((r) => fmtDate(r.date)),
      datasets: [{
        label: "Risk score",
        data: risk.series.map((r) => r.score),
        backgroundColor: risk.series.map((r) => COLORS[r.level] + (r.is_forecast ? "99" : "")),
        borderColor: risk.series.map((r) => COLORS[r.level]),
        borderWidth: 1,
      }],
    },
    options: {
      maintainAspectRatio: false,
      scales: { y: { min: 0, max: 100 } },
      plugins: { legend: { display: false },
        tooltip: { callbacks: { afterLabel: (c) => {
          const r = risk.series[c.dataIndex];
          return `${r.level}${r.is_forecast ? " (forecast)" : ""} · wet run ${r.longest_wet_run} h · rain ${r.rain_mm} mm`;
        } } } },
    },
  });

  chart("weather-chart", {
    type: "line",
    data: {
      labels: weather.hours.map((h) => h.ts.slice(5, 13).replace("T", " ") + "h"),
      datasets: [
        { label: "RH %", data: weather.hours.map((h) => h.rh_pct), borderColor: "#3b82c4", pointRadius: 0, borderWidth: 1.5, yAxisID: "rh" },
        { label: "Temp °C", data: weather.hours.map((h) => h.temp_c), borderColor: "#d9822b", pointRadius: 0, borderWidth: 1.5, yAxisID: "t" },
      ],
    },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { ticks: { maxTicksLimit: 8 } },
        rh: { position: "left", min: 0, max: 100, title: { display: true, text: "RH %" } },
        t: { position: "right", grid: { drawOnChartArea: false }, title: { display: true, text: "°C" } },
      },
    },
  });

  const varieties = advice.varieties.length
    ? `<ul>${advice.varieties.map((v) => `<li><b>${esc(v.name)}</b> — ${esc(v.genes.join(" + "))} (${esc(v.status)}). ${esc(v.note)}
         <a href="${esc(v.source_url)}" target="_blank" rel="noopener">source</a></li>`).join("")}</ul>`
    : `<p class="muted">No curated varieties for ${esc(advice.state)} yet.</p>`;
  $("advice").innerHTML = `
    <h3 style="margin-top:0">Next 5 days: ${advice.level ? `<span class="lvl ${advice.level}">${advice.level}</span>` : "no data"}</h3>
    <p>${esc(advice.message)}</p>
    <p class="muted small">${esc(advice.next_season)}</p>
    <h3>Resistant varieties for ${esc(advice.state)}</h3>${varieties}`;
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

async function main() {
  initMap();
  $("metric").addEventListener("change", loadProduction);
  $("from-year").addEventListener("change", loadProduction);
  try {
    await Promise.all([loadHealth(), loadRisk(), loadAreas().then(loadProduction), loadGenes()]);
  } catch (err) {
    console.error(err);
    $("freshness").textContent = "Could not load data — run `python -m blastwatch all` first.";
  }
}

main();
