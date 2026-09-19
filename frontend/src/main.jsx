import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

function timeAgo(value) {
  if (!value) return "Waiting for first check";
  return `Last checked ${new Date(value).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
}

function App() {
  const [state, setState] = useState({ itineraries: [], telemetry: [], alerts: [], provider_errors: [] });
  const [toast, setToast] = useState(null);
  const [polling, setPolling] = useState(false);

  useEffect(() => {
    fetch(`${API}/api/state`).then((response) => response.json()).then(setState).catch(() => {});
    const events = new EventSource(`${API}/api/events`);
    events.onmessage = ({ data }) => {
      const event = JSON.parse(data);
      if (event.state) setState(event.state);
      if (event.action) {
        setToast(event.action);
        setTimeout(() => setToast(null), 7000);
      }
    };
    return () => events.close();
  }, []);

  async function checkNow() {
    setPolling(true);
    try {
      const response = await fetch(`${API}/api/poll`, { method: "POST" });
      const result = await response.json();
      setState(result.state);
      if (result.action.action !== "none") setToast(result.action);
    } finally {
      setPolling(false);
    }
  }

  async function execute(alert, type) {
    await fetch(`${API}/api/actions/${alert.id}/execute`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ type })
    });
  }

  const active = state.alerts?.find((item) => item.action === "trigger_ui_alert");
  return <main>
    <header>
      <div className="eyebrow"><span className="pulse" /> DISPATCH / BACKGROUND MONITOR</div>
      <h1>Your schedule, defended.</h1>
      <p className="subtitle">A predictive logistics engine watching Pittsburgh transit before delays reach you.</p>
      <button className="check" onClick={checkNow} disabled={polling}>{polling ? "Checking..." : "Run check now"} <span>↗</span></button>
    </header>
    {toast && <div className="toast"><strong>Proactive alert</strong><span>{toast.message}</span></div>}
    <section className="status-row">
      <div><span className="label">SYSTEM STATUS</span><strong className="online">● Monitoring live telemetry</strong></div>
      <div className="last-check">{timeAgo(state.last_poll_at)}</div>
    </section>
    <section className="grid">
      <div className="panel itinerary"><div className="panel-title"><span>MONITORED ITINERARIES</span><span className="count">{state.itineraries?.length || 0}</span></div>
        {(state.itineraries || []).map((item) => <div className="itinerary-item" key={item.id}><div className={`mode ${item.mode}`}>{item.mode === "rail" ? "↠" : "✈"}</div><div><strong>{item.label}</strong><small>{item.mode === "rail" ? "Intercity rail · Pittsburgh" : "Air logistics · PIT"}</small></div><span className="monitoring">Monitoring</span></div>)}
      </div>
      <div className="panel signal"><div className="panel-title"><span>TELEMETRY SIGNALS</span><span className="live">LIVE</span></div>
        <div className="signal-number">{state.telemetry?.length || 0}</div><p>vehicles in the Pittsburgh bounding box</p>
        <div className="signal-bar"><span style={{ width: `${Math.min(100, (state.telemetry?.length || 0) / 2)}%` }} /></div>
        <small>PRT · AMTRAK · OPENSKY</small>
      </div>
    </section>
    <section className="panel alert-panel"><div className="panel-title"><span>PROACTIVE ACTION CENTER</span><span className="count">{state.alerts?.length || 0}</span></div>
      {active ? <div className="alert-card"><div className="warning">!</div><div className="alert-content"><span className="alert-label">ACTION REQUIRED · {active.status.toUpperCase()}</span><h2>{active.message}</h2><p>Dispatch detected an anomaly and prepared mitigations before an official service alert.</p><div className="actions"><button onClick={() => execute(active, "draft_email")}>Draft email</button><button onClick={() => execute(active, "reschedule_calendar")}>Reschedule calendar</button></div></div></div> : <div className="empty"><span>✓</span><div><strong>No action required</strong><p>Dispatch is watching. You’ll see an alert here when intervention can protect your schedule.</p></div></div>}
    </section>
    {!!state.provider_errors?.length && <div className="errors">Some live sources are unavailable. Fallback monitoring remains active.</div>}
    <footer>DISPATCH ENGINE <span>•</span> SILENT LOGIC <span>•</span> PITTSBURGH REGION</footer>
  </main>;
}

createRoot(document.getElementById("root")).render(<App />);
