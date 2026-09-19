const state = {
  flights: null,
  transit: null,
  kind: "arrival",
  search: "",
  route: "",
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

$$("nav button").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$("nav button").forEach((b) => b.classList.remove("active"));
    $$(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("#" + btn.dataset.panel).classList.add("active");
  });
});

$$(".pill").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".pill").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.kind = btn.dataset.kind;
    renderFlights();
  });
});

$("#flight-search").addEventListener("input", (e) => {
  state.search = e.target.value.toLowerCase();
  renderFlights();
});
$("#route-filter").addEventListener("input", (e) => {
  state.route = e.target.value.toUpperCase();
  renderVehicles();
});

$$(".chips button").forEach((btn) => {
  btn.addEventListener("click", () => {
    $("#plan-form").q.value = btn.dataset.q;
    runPlan(btn.dataset.q);
  });
});

$("#plan-form").addEventListener("submit", (e) => {
  e.preventDefault();
  runPlan(new FormData(e.target).get("q"));
});

async function boot() {
  try {
    const [flights, transit, extract] = await Promise.all([
      fetch("/api/flights").then(ok),
      fetch("/api/transit").then(ok),
      fetch("/api/extract").then(ok),
    ]);
    state.flights = flights;
    state.transit = transit;
    renderHub();
    renderFlights();
    renderVehicles();
    renderExtract(extract);
  } catch (err) {
    $("#stat-row").innerHTML = `<p class="error">${err.message}. Is the API running?</p>`;
  }
}

function ok(res) {
  if (!res.ok) throw new Error("Could not compile live feeds");
  return res.json();
}

function renderHub() {
  const f = state.flights;
  const t = state.transit;
  $("#stat-arrivals").textContent = f.summary.arrivals;
  $("#stat-deps").textContent = f.summary.departures;
  $("#stat-delay").textContent = `${f.summary.delayed} / ${f.summary.cancelled}`;
  $("#stat-buses").textContent = t.buses.count;
  $("#stat-flyer").textContent = t.flyer_vehicles.length;
  $("#flight-clock").textContent = f.airport_clock || "PIT local board";

  const pred = t.predictions || {};
  $("#hub-predictions").innerHTML = ["airport", "downtown", "oakland"]
    .map((hub) => {
      const rows = pred[hub] || [];
      const next = rows[0];
      return `<div class="row"><span>${hub}</span><strong>${
        next ? next.minutes + " min · " + next.stop_name : "no prediction"
      }</strong></div>`;
    })
    .join("");

  $("#hub-alerts").innerHTML = (t.alerts || [])
    .slice(0, 5)
    .map((a) => `<div class="row"><span>${a.header || a.id}</span><em>${(a.routes || []).slice(0, 3).join(" ")}</em></div>`)
    .join("") || "<p class='hint'>No current bulletins.</p>";

  drawMap(t);
}

function drawMap(t) {
  const map = $("#map");
  map.innerHTML = `
    <span class="place" style="left:18%;top:18%">PIT</span>
    <span class="place" style="left:62%;top:58%">Downtown</span>
    <span class="place" style="left:78%;top:52%">Oakland</span>
  `;
  const buses = [...(t.buses.vehicles || []), ...(t.trains.vehicles || []).map((v) => ({ ...v, train: true }))];
  buses.forEach((v) => {
    const { x, y } = project(v.lat, v.lon);
    if (x < 0 || x > 100 || y < 0 || y > 100) return;
    const el = document.createElement("div");
    el.className = "bus " + (v.train ? "train" : v.route_id === "28X" ? "flyer" : "other");
    el.style.left = x + "%";
    el.style.top = y + "%";
    el.title = `${v.route_id || "T"} · #${v.id}`;
    map.appendChild(el);
  });
}

function project(lat, lon) {
  const west = -80.32, east = -79.86, south = 40.36, north = 40.52;
  return {
    x: ((lon - west) / (east - west)) * 100,
    y: ((north - lat) / (north - south)) * 100,
  };
}

function renderFlights() {
  if (!state.flights) return;
  const rows = state.kind === "arrival" ? state.flights.arrivals : state.flights.departures;
  const q = state.search;
  const filtered = rows.filter((r) =>
    `${r.flight} ${r.city} ${r.airline} ${r.gate || ""}`.toLowerCase().includes(q)
  );
  $("#flight-body").innerHTML = filtered
    .slice(0, 80)
    .map((r) => {
      const time = r.estimated && r.estimated !== r.scheduled
        ? `${r.scheduled}<br><small>now ${r.estimated}</small>`
        : r.scheduled || "—";
      return `<tr>
        <td>${time}</td>
        <td>${r.city}</td>
        <td>${r.airline}<br><strong>${r.flight}</strong></td>
        <td>${r.gate || "—"}</td>
        <td>${r.baggage || "—"}</td>
        <td class="status ${r.status}">${r.remarks}</td>
      </tr>`;
    })
    .join("");
}

function renderVehicles() {
  if (!state.transit) return;
  const all = [...state.transit.buses.vehicles, ...state.transit.trains.vehicles];
  const rows = all.filter((v) => !state.route || (v.route_id || "").includes(state.route));
  $("#vehicle-list").innerHTML = rows
    .slice(0, 80)
    .map((v) => `<div class="row"><span>${v.route_id || "T"} · ${v.id}</span><span>${v.speed_mph ?? "—"} mph</span></div>`)
    .join("");
  const max = Math.max(...state.transit.active_routes.map((x) => x[1]), 1);
  $("#route-bars").innerHTML = state.transit.active_routes
    .map(([id, n]) => `<div><span>${id}</span><b style="width:${(n / max) * 100}%"></b><span>${n}</span></div>`)
    .join("");
}

function renderExtract(data) {
  $("#extract-out").innerHTML = data.xtract
    .map(
      (block) => `<div class="pipe">
        <div><h3>${block.page}</h3><p class="hint">${block.source}<br>${block.raw_form}</p></div>
        <div class="mark" style="width:42px;height:42px">→</div>
        <pre>${JSON.stringify(block.sample, null, 2)}</pre>
      </div>`
    )
    .join("");
}

async function runPlan(q) {
  const out = $("#plan-out");
  out.innerHTML = "<p class='hint'>Compiling live boards…</p>";
  const res = await fetch("/api/plan?q=" + encodeURIComponent(q));
  const data = await res.json();
  if (!data.itinerary) {
    out.innerHTML = `<p>Matched intent <code>${data.intent}</code>. Try a flight number plus downtown, Oakland, or CMU.</p>`;
    return;
  }
  out.innerHTML = `<h3>${data.itinerary.title}</h3>` +
    data.itinerary.steps
      .map(
        (s) => `<div class="step">
          <strong>${s.time} · ${s.label}</strong>
          <p>${s.detail}</p>
          <p class="hint">${s.meta} · ${s.source}</p>
        </div>`
      )
      .join("") +
    (data.itinerary.risk ? `<p class="error">${data.itinerary.risk}</p>` : "") +
    `<p class="hint">${data.method}</p>`;
}

boot();
