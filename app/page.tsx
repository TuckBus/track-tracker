"use client";

import { useCallback, useEffect, useState } from "react";
import type { ActionPayload, DispatchState } from "../lib/types";

const emptyState: DispatchState = { itineraries: [], telemetry: [], alerts: [], provider_errors: [], last_poll_at: null };

function timeAgo(value: string | null) {
  return value ? `Last checked ${new Date(value).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "Waiting for first check";
}

export default function Home() {
  const [state, setState] = useState<DispatchState>(emptyState);
  const [toast, setToast] = useState<ActionPayload | null>(null);
  const [polling, setPolling] = useState(false);

  const checkNow = useCallback(async () => {
    setPolling(true);
    try {
      const result = await fetch("/api/poll", { method: "POST" }).then((response) => response.json()) as { action: ActionPayload; state: DispatchState };
      setState(result.state);
      if (result.action.action !== "none") setToast(result.action);
    } finally {
      setPolling(false);
    }
  }, []);

  useEffect(() => {
    fetch("/api/state").then((response) => response.json()).then(setState).catch(() => {});
    const interval = window.setInterval(checkNow, 60000);
    return () => window.clearInterval(interval);
  }, [checkNow]);

  async function execute(alert: ActionPayload, type: "draft_email" | "reschedule_calendar") {
    const result = await fetch(`/api/actions/${alert.id}/execute`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ type }),
    }).then((response) => response.json()) as { state: DispatchState };
    setState(result.state);
  }

  const active = state.alerts.find((item) => item.action === "trigger_ui_alert");
  return <main>
    <header>
      <div className="eyebrow"><span className="pulse" /> DISPATCH / VERCEL MONITOR</div>
      <h1>Your schedule, defended.</h1>
      <p className="subtitle">A predictive logistics engine watching Pittsburgh transit before delays reach you.</p>
      <button className="check" onClick={checkNow} disabled={polling}>{polling ? "Checking..." : "Run check now"} <span>↗</span></button>
    </header>
    {toast && <div className="toast"><strong>Proactive alert</strong><span>{toast.message}</span></div>}
    <section className="status-row"><div><span className="label">SYSTEM STATUS</span><strong className="online">● Monitoring live telemetry</strong></div><div className="last-check">{timeAgo(state.last_poll_at)}</div></section>
    <section className="grid">
      <div className="panel itinerary"><div className="panel-title"><span>MONITORED ITINERARIES</span><span className="count">{state.itineraries.length}</span></div>
        {state.itineraries.map((item) => <div className="itinerary-item" key={item.id}><div className={`mode ${item.mode}`}>{item.mode === "rail" ? "↠" : "✈"}</div><div><strong>{item.label}</strong><small>{item.mode === "rail" ? "Intercity rail · Pittsburgh" : "Air logistics · PIT"}</small></div><span className="monitoring">Monitoring</span></div>)}
      </div>
      <div className="panel signal"><div className="panel-title"><span>TELEMETRY SIGNALS</span><span className="live">LIVE</span></div><div className="signal-number">{state.telemetry.length}</div><p>vehicles in the Pittsburgh bounding box</p><div className="signal-bar"><span style={{ width: `${Math.min(100, state.telemetry.length / 2)}%` }} /></div><small>PRT · AMTRAK · OPENSKY</small></div>
    </section>
    <section className="panel alert-panel"><div className="panel-title"><span>PROACTIVE ACTION CENTER</span><span className="count">{state.alerts.length}</span></div>
      {active ? <div className="alert-card"><div className="warning">!</div><div className="alert-content"><span className="alert-label">ACTION REQUIRED · {active.status.toUpperCase()}</span><h2>{active.message}</h2><p>Dispatch detected an anomaly and prepared mitigations before an official service alert.</p><div className="actions"><button onClick={() => execute(active, "draft_email")}>Draft email</button><button onClick={() => execute(active, "reschedule_calendar")}>Reschedule calendar</button></div></div></div> : <div className="empty"><span>✓</span><div><strong>No action required</strong><p>Dispatch is watching. You’ll see an alert here when intervention can protect your schedule.</p></div></div>}
    </section>
    {state.provider_errors.length > 0 && <div className="errors">Some live sources are unavailable. Fallback monitoring remains active.</div>}
    <footer>DISPATCH ENGINE <span>•</span> VERCEL NATIVE <span>•</span> PITTSBURGH REGION</footer>
  </main>;
}
