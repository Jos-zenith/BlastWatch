const COLORS = { Low: "#2e9e5b", Moderate: "#d99a1b", High: "#d14343" };
const ACTION_COLORS = { alert: "#d14343", watch: "#d99a1b", none: "#2e9e5b", unknown: "#8a948f" };

const state = { lang: "en", data: null, alerts: [], selected: null, chart: null };
let map, markers;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const T = (key) => STRINGS[state.lang][key] ?? STRINGS.en[key] ?? key;
const locale = () => (state.lang === "ta" ? "ta-IN" : "en-IN");
const dayName = (iso) => new Date(iso + "T00:00:00").toLocaleDateString(locale(), { weekday: "short", day: "numeric" });
const dname = (d) => (state.lang === "ta" && d.name_ta ? d.name_ta : d.name);

function loadLang() {
  // ?lang=ta in a shared link wins over the remembered choice.
  const fromUrl = new URLSearchParams(location.search).get("lang");
  if (fromUrl in STRINGS) return fromUrl;
  try { return localStorage.getItem("bw-lang") || "en"; } catch { return "en"; }
}
function saveLang(lang) {
  try { localStorage.setItem("bw-lang", lang); } catch { /* storage unavailable */ }
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

function applyStatic() {
  document.documentElement.lang = state.lang;
  document.querySelectorAll("[data-t]").forEach((el) => { el.textContent = T(el.dataset.t); });
  document.querySelectorAll("[data-lang]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.lang === state.lang));
}

function renderFreshness(f) {
  const el = $("freshness");
  el.className = `banner ${f.status}`;
  if (!f.last_success) el.textContent = T("never");
  else if (f.status === "fresh") el.textContent = T("fresh")(f.age_hours < 1 ? "<1" : Math.round(f.age_hours), f.source);
  else if (f.status === "stale") el.textContent = T("stale")(Math.round(f.age_hours));
  else el.textContent = T("expired");
}

function dayStrip(days, big = false) {
  return days.map((d) => `
    <span class="day ${d.horizon}" style="--c:${COLORS[d.level]}" title="${esc(d.date)} · ${esc(T("level_" + d.level))} · ${d.leaf_wet_hours} ${esc(T("wet_hours"))}">
      <b>${esc(dayName(d.date))}</b>${big ? `<i>${esc(T("level_" + d.level))}</i><small>${d.leaf_wet_hours} h</small>` : ""}
    </span>`).join("");
}

function render() {
  applyStatic();
  const data = state.data;
  if (!data) return;
  renderFreshness(data.freshness);

  const counts = { alert: 0, watch: 0, none: 0, unknown: 0 };
  data.districts.forEach((d) => counts[d.action]++);
  $("summary").innerHTML = ["alert", "watch", "none"].map((a) =>
    `<span class="pill" style="--c:${ACTION_COLORS[a]}"><b>${counts[a]}</b> ${esc(T("summary_" + a))}</span>`).join("");

  $("actions").innerHTML = data.districts.map((d) => `
    <li>
      <button class="action-row ${d.id === state.selected ? "sel" : ""}" data-id="${d.id}">
        <span class="row-head">
          <span class="name">${esc(dname(d))}</span>
          <span class="badge" style="--c:${ACTION_COLORS[d.action]}">${esc(T("action_" + d.action))}</span>
        </span>
        <span class="day-strip">${dayStrip(d.days)}</span>
      </button>
    </li>`).join("");
  $("actions").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => select(Number(b.dataset.id), true)));

  markers.clearLayers();
  data.districts.forEach((d) => {
    L.circleMarker([d.lat, d.lon], {
      radius: d.id === state.selected ? 13 : 9,
      color: ACTION_COLORS[d.action], fillColor: ACTION_COLORS[d.action], fillOpacity: 0.6, weight: 2,
    }).bindTooltip(`${esc(dname(d))}: ${esc(T("action_" + d.action))}`).on("click", () => select(d.id)).addTo(markers);
  });

  if (state.selected != null) renderDetail();
}

async function select(id, fromList = false) {
  state.selected = id;
  state.weather = state.advice = null;
  state.chart?.destroy();
  $("varieties").innerHTML = "";
  render();
  // On phones the detail sits below the list; bring it into view after a tap.
  if (fromList && window.matchMedia("(max-width: 900px)").matches) {
    $("detail").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  const [weather, advice] = await Promise.all([
    api(`/api/districts/${id}/weather?hours_back=24`),
    api(`/api/districts/${id}/advice`),
  ]);
  if (state.selected !== id) return;
  state.weather = weather;
  state.advice = advice;
  renderDetail();
}

function renderDetail() {
  const d = state.data.districts.find((x) => x.id === state.selected);
  if (!d) return;
  $("detail").hidden = false;
  $("detail-title").textContent = dname(d);
  const badge = $("detail-action");
  badge.textContent = T("action_" + d.action);
  badge.style.setProperty("--c", ACTION_COLORS[d.action]);
  $("detail-todo").textContent = T("todo_" + d.action);
  $("detail-days").innerHTML = dayStrip(d.days, true);

  const bases = [...new Set(d.days.map((x) => x.wetness_basis).filter(Boolean))];
  $("detail-basis").textContent = bases.length
    ? `${T("based_on")}: ${bases.map((b) => T("basis")[b] || b).join(", ")}` : "";

  const alert = state.alerts.find((a) => a.district === d.name && a.status === "ready")
    || state.alerts.find((a) => a.district === d.name);
  $("messages").innerHTML = alert ? `
    <div class="message">
      <p lang="ta">${esc(alert.message_ta)}</p>
      <p lang="en" class="muted">${esc(alert.message_en)}</p>
      <div class="msg-actions">
        <button data-copy="ta">${esc(T("copy"))} (தமிழ்)</button>
        <button data-copy="en">${esc(T("copy"))} (English)</button>
        ${alert.status === "sent"
          ? `<span class="muted small">${esc(T("sent"))} ${esc(new Date(alert.sent_at).toLocaleString(locale()))}</span>`
          : `<button data-sent="${alert.id}">${esc(T("mark_sent"))}</button>`}
      </div>
    </div>` : `<p class="muted">${esc(T("no_message"))}</p>`;
  $("messages").querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(b.dataset.copy === "ta" ? alert.message_ta : alert.message_en);
      b.textContent = T("copied");
    } catch { /* clipboard blocked; the text is selectable */ }
  }));
  $("messages").querySelectorAll("[data-sent]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/alerts/${b.dataset.sent}/sent`, { method: "POST" });
    state.alerts = await api("/api/alerts");
    renderDetail();
  }));

  if (state.advice) {
    $("varieties").innerHTML = state.advice.varieties.map((v) =>
      `<li><b>${esc(v.name)}</b> <span class="muted">(${esc(v.genes.join(" + "))}, ${esc(v.status)})</span></li>`).join("");
  }
  if (state.weather) drawChart(state.weather.hours);
}

function drawChart(hours) {
  state.chart?.destroy();
  const labels = hours.map((h) => new Date(h.ts).toLocaleString(locale(), { weekday: "short", hour: "numeric" }));
  state.chart = new Chart($("wet-chart"), {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: T("chart_lwp"), data: hours.map((h) => h.leaf_wet_min != null ? h.leaf_wet_min / 0.6 : h.leaf_wet_prob),
          borderColor: "#3b82c4", backgroundColor: "#3b82c433", fill: true, pointRadius: 0, borderWidth: 1.5, yAxisID: "w" },
        { label: T("chart_temp"), data: hours.map((h) => h.temp_c),
          borderColor: "#d9822b", pointRadius: 0, borderWidth: 1.5, yAxisID: "t" },
      ],
    },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { ticks: { maxTicksLimit: 7 } },
        w: { min: 0, max: 100, position: "left" },
        t: { position: "right", grid: { drawOnChartArea: false } },
      },
    },
  });
}

async function load() {
  const [data, alerts] = await Promise.all([api("/api/outlook"), api("/api/alerts")]);
  state.data = data;
  state.alerts = alerts;
  if (state.selected == null && data.districts.length) state.selected = data.districts[0].id;
  render();
  if (state.selected != null) select(state.selected);
}

function main() {
  state.lang = loadLang();
  map = L.map("map", { scrollWheelZoom: false }).setView([10.9, 78.9], 7);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 12,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  markers = L.layerGroup().addTo(map);
  document.querySelectorAll("[data-lang]").forEach((b) => b.addEventListener("click", () => {
    state.lang = b.dataset.lang;
    saveLang(state.lang);
    render();
    if (state.weather) drawChart(state.weather.hours);
  }));
  applyStatic();
  load().catch((err) => {
    console.error(err);
    $("freshness").className = "banner expired";
    $("freshness").textContent = T("never");
  });
  setInterval(() => load().catch(console.error), 15 * 60 * 1000);
}

main();
