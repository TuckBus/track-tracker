"use client";

import dynamic from "next/dynamic";
import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import type { ActionPayload, DispatchState, TelemetryRecord } from "../lib/types";

const emptyState: DispatchState = { itineraries: [], telemetry: [], alerts: [], provider_errors: [], last_poll_at: null, nemotron: { status: "not_configured", checked_at: null }, service_alerts: [] };
const VehicleMap = dynamic(() => import("./VehicleMap"), { ssr: false, loading: () => <div className="map-loading">Loading map tiles...</div> });
type PrtStop = { id: string; name: string; latitude: number; longitude: number; routes?: string[] };

function timeAgo(value: string | null) {
  return value ? `Last checked ${new Date(value).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "Waiting for first check";
}

export default function Home() {
  const [state, setState] = useState<DispatchState>(emptyState);
  const [toast, setToast] = useState<ActionPayload | null>(null);
  const [polling, setPolling] = useState(false);
  const [stop, setStop] = useState("");
  const [stopId, setStopId] = useState("");
  const [line, setLine] = useState("");
  const [lookup, setLookup] = useState<{ status: string; message: string; vehicle_count: number; eta_minutes: number | null; eta_message: string; eta_confidence: string | null } | null>(null);
  const [lookupLoading, setLookupLoading] = useState(false);
  const [lookupError, setLookupError] = useState("");
  const [initialLoading, setInitialLoading] = useState(true);
  const [initialLoadStatus, setInitialLoadStatus] = useState("Preparing the dashboard...");
  const [initialLoadError, setInitialLoadError] = useState("");
  const [vehicleSearch, setVehicleSearch] = useState("");
  const [showAllVehicles, setShowAllVehicles] = useState(false);
  const [destination, setDestination] = useState("");
  const [destinationSearch, setDestinationSearch] = useState("");
  const [destinationMenuOpen, setDestinationMenuOpen] = useState(false);
  const [showAllDelays, setShowAllDelays] = useState(false);
  const [navigation, setNavigation] = useState<{ distance: string; url: string } | null>(null);
  const [showNavigationExplanation, setShowNavigationExplanation] = useState(false);
  const [stops, setStops] = useState<PrtStop[]>([]);
  const [stopSearch, setStopSearch] = useState("");
  const [stopMenuOpen, setStopMenuOpen] = useState(false);
  const [stopsLoading, setStopsLoading] = useState(true);
  const [actionError, setActionError] = useState("");
  const pollingRef = useRef(false);
  const alertSoundRef = useRef<AudioContext | null>(null);

  const checkNow = useCallback(async (options: { clearAlerts?: boolean } = {}) => {
    if (pollingRef.current) return;
    pollingRef.current = true;
    setPolling(true);
    setActionError("");
    setInitialLoading(true);
    setInitialLoadStatus("Refreshing live telemetry and generating the latest AI analysis...");
    try {
      const clearAlerts = options.clearAlerts === true;
      const response = await fetch("/api/poll", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ clear_alerts: clearAlerts }), signal: AbortSignal.timeout(30000) });
      const result = await response.json() as { action: ActionPayload; state: DispatchState };
      if (!response.ok) throw new Error("Telemetry check failed");
      setState(result.state);
      if (clearAlerts) setToast(null);
      if (result.action.action !== "none") {
        setToast(result.action);
        if (result.action.status === "anomalous") {
          try {
            const audio = alertSoundRef.current || new AudioContext();
            alertSoundRef.current = audio;
            if (audio.state === "suspended") {
              void audio.resume().catch(() => undefined);
            }
            const oscillator = audio.createOscillator();
            const gain = audio.createGain();
            oscillator.type = "square";
            oscillator.frequency.setValueAtTime(740, audio.currentTime);
            oscillator.frequency.setValueAtTime(520, audio.currentTime + 0.12);
            gain.gain.setValueAtTime(0.0001, audio.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.18, audio.currentTime + 0.01);
            gain.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + 0.25);
            oscillator.connect(gain).connect(audio.destination);
            oscillator.start();
            oscillator.stop(audio.currentTime + 0.26);
          } catch {
            // Browser autoplay policy may prevent sound until the user interacts.
          }
        }
      }
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Telemetry check failed");
    } finally {
      setPolling(false);
      pollingRef.current = false;
      setInitialLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function loadDashboard() {
      setInitialLoadStatus("Loading the PRT stop catalog and saved bus state...");
      setInitialLoadError("");
      const requestOptions = { signal: AbortSignal.timeout(15000) };
      const stopsRequest = fetch("/api/stops", requestOptions).then(async (response) => {
        if (!response.ok) throw new Error(`Stop catalog request failed (${response.status})`);
        return response.json() as Promise<{ stops?: PrtStop[] }>;
      });
      const stateRequest = fetch("/api/state", requestOptions).then(async (response) => {
        if (!response.ok) throw new Error(`Dispatch state request failed (${response.status})`);
        return response.json() as Promise<DispatchState>;
      });
      const [stopsResult, stateResult] = await Promise.allSettled([stopsRequest, stateRequest]);
      if (cancelled) return;
      if (stopsResult.status === "fulfilled") {
        setStops(stopsResult.value.stops || []);
      } else {
        setInitialLoadError("The stop catalog could not be loaded; live telemetry is still available.");
      }
      setStopsLoading(false);
      if (stateResult.status === "fulfilled") {
        setState(stateResult.value);
        if (stateResult.value.telemetry.length === 0) {
          setInitialLoadStatus("No saved telemetry found. Running the first live provider check...");
          setInitialLoading(false);
          void checkNow().catch(() => {
            if (!cancelled) setInitialLoadError("The first live check did not finish. You can run it again with Run check now.");
          });
        } else {
          setInitialLoadStatus("Restored recent telemetry. Finishing dashboard setup...");
          setInitialLoading(false);
        }
      } else {
        setInitialLoadError("Saved dispatch state could not be loaded. Running a fresh live check...");
        setInitialLoadStatus("Saved state unavailable. Running the first live provider check...");
        setInitialLoading(false);
        void checkNow().catch(() => {
          if (!cancelled) setInitialLoadError("The first live check did not finish. You can run it again with Run check now.");
        });
      }
    }
    void loadDashboard().catch(() => {
      if (!cancelled) {
        setInitialLoadError("Dashboard startup took too long. You can continue and retry the live check.");
        setInitialLoading(false);
        setStopsLoading(false);
      }
    });
    const interval = window.setInterval(checkNow, 300000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [checkNow]);

  useEffect(() => {
    if (state.telemetry.length > 0) {
      setInitialLoading(false);
      setInitialLoadStatus("Live telemetry loaded. Finishing dashboard setup...");
    }
  }, [state.telemetry.length]);

  useEffect(() => {
    if (stops.length > 0 && !stopId) {
      setStop(stops[0].name);
      setStopId(stops[0].id);
    }
    else if (!stop && stops.length === 0) setStop("Fifth Avenue & Bigelow Boulevard");
    if (!line) setLine((stops.length > 0 && !stopId ? stops[0].routes?.[0] : undefined) || state.telemetry.find((item) => item.source === "prt" && item.route)?.route || "61A");
  }, [line, state.telemetry, stop, stopId, stops]);

  async function checkStop(event?: React.FormEvent) {
    event?.preventDefault();
    setLookupLoading(true);
    setLookupError("");
    try {
      const response = await fetch("/api/anomaly", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ stop, stop_id: stopId, stop_routes: selectedStop?.routes, line }) });
      const result = await response.json() as { status?: string; message?: string; vehicle_count?: number; eta_minutes?: number | null; eta_message?: string; eta_confidence?: string | null; error?: string };
      if (!response.ok) throw new Error(result.error || "Lookup failed");
      setLookup({ status: result.status || "unknown", message: result.message || "", vehicle_count: result.vehicle_count || 0, eta_minutes: result.eta_minutes ?? null, eta_message: result.eta_message || "", eta_confidence: result.eta_confidence ?? null });
    } catch (error) {
      setLookupError(error instanceof Error ? error.message : "Lookup failed");
    } finally {
      setLookupLoading(false);
    }
  }

  async function execute(alert: ActionPayload, type: "draft_email" | "reschedule_calendar") {
    setActionError("");
    try {
      const response = await fetch(`/api/actions/${alert.id}/execute`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ type }),
      });
      const result = await response.json() as { state?: DispatchState; error?: string };
      if (!response.ok || !result.state) throw new Error(result.error || "Could not prepare that action.");
      setState(result.state);
      setToast(result.state.alerts[0] || null);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not prepare that action.");
    }
  }

  const vehicles = useMemo(() => state.telemetry.filter((item) => item.latitude !== null && item.longitude !== null), [state.telemetry]);
  const deferredVehicleSearch = useDeferredValue(vehicleSearch);
  const filteredVehicles = useMemo(() => state.telemetry.filter((vehicle) => `${vehicle.vehicle_id} ${vehicle.source} ${vehicle.route || ""}`.toLowerCase().includes(deferredVehicleSearch.toLowerCase())), [state.telemetry, deferredVehicleSearch]);
  const matchingStops = useMemo(() => stops.filter((candidate) => `${candidate.name} ${candidate.id}`.toLowerCase().includes(stopSearch.toLowerCase())).slice(0, 40), [stops, stopSearch]);
  const selectedStop = useMemo(() => stops.find((candidate) => candidate.id === stopId) || stops.find((candidate) => candidate.name === stop), [stops, stop, stopId]);
  const displayedVehicles = showAllVehicles ? filteredVehicles : filteredVehicles.slice(0, 10);
  const active = state.alerts.find((item) => item.source === "nemotron" && item.action === "trigger_ui_alert");
  const delayedRoutes = state.service_alerts.flatMap((alert) => alert.routes);
  const nemotronDelayedRoutes = [...new Set(state.alerts.filter((item) => item.source === "nemotron").flatMap((item) => item.affected_routes || []))];
  useEffect(() => {
    if (!selectedStop || !line || !selectedStop.routes?.some((route) => route.toLowerCase().replace(/[\s-]/g, "") === line.toLowerCase().replace(/[\s-]/g, ""))) return;
    void checkStop();
  }, [selectedStop, line]);
  const destinations = stops.map((item) => ({ id: `stop-${item.id}`, name: item.name, latitude: item.latitude, longitude: item.longitude }));
  const matchingDestinations = destinations.filter((item) => item.name.toLowerCase().includes(destinationSearch.toLowerCase()));
  function navigateToPlace() {
    const place = destinations.find((item) => item.name === destination);
    if (!place) return;
    navigator.geolocation.getCurrentPosition((position) => {
      const distance = Math.sqrt((position.coords.latitude - place.latitude) ** 2 + (position.coords.longitude - place.longitude) ** 2) * 69;
      setNavigation({ distance: `${distance.toFixed(1)} miles`, url: `https://www.google.com/maps/dir/?api=1&destination=${place.latitude},${place.longitude}` });
    }, () => setNavigation({ distance: "Location unavailable", url: `https://www.google.com/maps/dir/?api=1&destination=${place.latitude},${place.longitude}` }));
  }
  return <main className={initialLoading ? "dashboard-loading" : ""}>
    <header>
      <div className="eyebrow"><span className="pulse" /> TRACK TRACKER / PRT BUS WATCH</div>
      <div className="brand-lockup"><span className="parrot-mark" aria-hidden="true">🦜</span><span className="brand-name">Track Tracker</span></div>
      <h1>Never miss your bus.</h1>
      <p className="subtitle">A bright, predictive watch over Pittsburgh Regional Transit buses, stops, and delays.</p>
      <button className="check" onClick={() => void checkNow()} disabled={polling}>{polling ? "Checking..." : "Run check now"} <span>↗</span></button>
    </header>
    {toast && <div className="toast"><strong>Proactive alert</strong><span>{toast.message}</span></div>}
    <section className="status-row"><div><span className="label">SYSTEM STATUS</span><strong className="online">● Monitoring live telemetry</strong></div><div className="status-right"><span className={`nemotron-status ${state.nemotron.status}`} title={state.nemotron.error || undefined}><span className="status-dot" /> Nemotron {state.nemotron.status === "connected" ? "connected" : state.nemotron.status === "failed" ? "unavailable · fallback active" : "not configured"}</span><div className="last-check">{timeAgo(state.last_poll_at)}</div></div></section>
    {state.nemotron.status === "failed" && <div className="nemotron-help"><strong>AI prediction is temporarily unavailable.</strong><span>Track Tracker is still monitoring live PRT bus telemetry. Refresh the NVIDIA key or enable hosted chat inference for this account; see the deployment troubleshooting guide.</span></div>}
    {initialLoading && <section className="loading-panel" aria-live="polite"><span className="loading-spinner" /><div><strong>Starting Track Tracker</strong><span>{initialLoadStatus}</span><small>Connecting to local state, loading all PRT stops, then polling bus positions and service alerts. Nemotron will analyze the latest PRT snapshot after telemetry arrives.</small></div></section>}
    {initialLoadError && <div className="errors startup-error">{initialLoadError}</div>}
    <section className="grid">
      <div className="panel itinerary"><div className="panel-title"><span>MONITORED ITINERARIES</span><span className="count">{state.itineraries.length}</span></div>
        {state.itineraries.map((item) => <div className="itinerary-item" key={item.id}><div className="mode bus">🚌</div><div><strong>{item.label}</strong><small>Live PRT bus positions and service alerts</small></div><span className="monitoring">Monitoring</span></div>)}
      </div>
      <div className="panel signal"><div className="panel-title"><span>BUS SIGNALS</span><span className="live">LIVE</span></div><div className="signal-number">{state.telemetry.length}</div><p>PRT buses tracked in the Pittsburgh region</p><div className="signal-bar"><span style={{ width: `${Math.min(100, state.telemetry.length / 2)}%` }} /></div><small>ONE FEED · PRT GTFS-RT</small></div>
    </section>
    <section className="panel map-panel"><div className="panel-title"><span>LIVE VEHICLE MAP</span><span className="count">{vehicles.length} vehicles · {stops.length} stops</span></div><VehicleMap vehicles={vehicles} stops={stops} delayedRoutes={delayedRoutes} nemotronRoutes={nemotronDelayedRoutes} /></section>
    {state.service_alerts.length > 0 && <section className="panel service-alerts"><div className="panel-title"><span>POSTED PRT DELAYS</span><span className="count">{state.service_alerts.length}</span></div>{(showAllDelays ? state.service_alerts : state.service_alerts.slice(0, 5)).map((alert) => <div className="service-alert" key={alert.id}><strong>{alert.header}</strong><span>{alert.description || "Check the map and allow extra travel time."}</span></div>)}{state.service_alerts.length > 5 && <button className="expand-button" onClick={() => setShowAllDelays((value) => !value)}>{showAllDelays ? "Show fewer delays" : `Delays expected · see all ${state.service_alerts.length}`}</button>}</section>}
    <section className="panel navigation-panel"><div className="panel-title"><span>NAVIGATE WITH DELAY AWARENESS</span><span className="live">PITTSBURGH</span></div><p className="section-copy">Choose a PRT stop to get directions from your current location and see whether Nemotron has predicted a bus delay not yet posted by PRT.</p><div className="navigation-form"><label className="destination-combobox" onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDestinationMenuOpen(false); }}><input role="combobox" aria-expanded={destinationMenuOpen} value={destinationSearch || destination} onFocus={() => setDestinationMenuOpen(true)} onChange={(event) => { setDestinationSearch(event.target.value); setDestination(""); setDestinationMenuOpen(true); }} placeholder="Search PRT stop" />{destinationMenuOpen && <div className="destination-options">{matchingDestinations.map((item) => <button type="button" key={item.id} onClick={() => { setDestination(item.name); setDestinationSearch(""); setDestinationMenuOpen(false); }}>{item.name}</button>)}{matchingDestinations.length === 0 && <span className="no-options">No matching PRT stops</span>}</div>}</label><button onClick={navigateToPlace} disabled={!destination}>Use my location</button></div>{navigation && <div className="navigation-result"><strong>{navigation.distance} away</strong><a href={navigation.url} target="_blank" rel="noreferrer">Open directions ↗</a>{nemotronDelayedRoutes.length > 0 ? <span className="delay-note">Nemotron-predicted routes: {nemotronDelayedRoutes.join(", ")}</span> : <span className="delay-note">No Nemotron-only bus delay prediction is currently active.</span>}<button className="explain-button" onClick={() => setShowNavigationExplanation(true)}>Why?</button></div>}{showNavigationExplanation && <div className="explanation-modal" role="dialog" aria-modal="true"><div className="explanation-card"><button className="modal-close" onClick={() => setShowNavigationExplanation(false)} aria-label="Close explanation">×</button><span className="label">NEMOTRON DECISION</span><h3>{nemotronDelayedRoutes.length > 0 ? "Potential delay detected" : "No delay detected"}</h3><p>{state.nemotron.explanation || "No explanation is available for the latest Nemotron decision."}</p><small>Based on the latest PRT bus telemetry and posted service data.</small></div></div>}</section>
    <section className="panel vehicle-list"><div className="panel-title"><span>TRACKED VEHICLES</span><span className="count">{filteredVehicles.length} matches</span></div>
      <input className="vehicle-search" value={vehicleSearch} onChange={(event) => setVehicleSearch(event.target.value)} placeholder="Search bus, route, or line..." aria-label="Search tracked buses" />
      {state.telemetry.length > 0 ? <><div className="vehicle-table">{displayedVehicles.map((vehicle) => <div className="vehicle-row" key={`${vehicle.source}-${vehicle.vehicle_id}`}><span className="vehicle-dot" /><strong>{vehicle.vehicle_id}</strong><span>PRT BUS</span><span>{vehicle.route || "—"}</span><span>{vehicle.speed_mph === null ? "—" : `${vehicle.speed_mph.toFixed(1)} mph`}</span><span>{vehicle.latitude === null ? "No position" : `${vehicle.latitude.toFixed(4)}, ${vehicle.longitude?.toFixed(4)}`}</span></div>)}</div>{filteredVehicles.length > 10 && <button className="expand-button" onClick={() => setShowAllVehicles((value) => !value)}>{showAllVehicles ? "Show fewer buses" : `Show all ${filteredVehicles.length} buses`}</button>}</> : <div className="empty"><span>—</span><div><strong>No tracked buses yet</strong><p>Run a live check to populate the bus list.</p></div></div>}
    </section>
    <section className="panel stop-panel"><div className="panel-title"><span>STOP ANOMALY CHECK</span><span className="live">LIVE TELEMETRY</span></div><p className="section-copy">Stop and line are prefilled from the monitored Pittsburgh service; adjust them before checking current telemetry.</p>
      <form className="stop-form" onSubmit={checkStop}><label className="stop-combobox" onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setStopMenuOpen(false); }}><span className="label">BUS STOP</span><input role="combobox" aria-expanded={stopMenuOpen} value={stopSearch || stop} onFocus={() => setStopMenuOpen(true)} onChange={(event) => { setStopSearch(event.target.value); setStop(event.target.value); setStopId(""); setStopMenuOpen(true); }} placeholder={stopsLoading ? "Loading PRT stops..." : "Search PRT stops"} />{stopMenuOpen && !stopsLoading && <div className="stop-options">{matchingStops.map((candidate) => <button type="button" key={candidate.id} onClick={() => { setStop(candidate.name); setStopId(candidate.id); setLine(candidate.routes?.[0] || ""); setStopSearch(""); setStopMenuOpen(false); }}>{candidate.name}<small>{candidate.id}{candidate.routes?.length ? ` · ${candidate.routes.slice(0, 4).join(", ")}` : ""}</small></button>)}{matchingStops.length === 0 && <span className="no-options">No matching PRT stops</span>}</div>}</label><label><span className="label">LINE</span><select value={line} onChange={(event) => setLine(event.target.value)} disabled={!selectedStop?.routes?.length}><option value="">{selectedStop?.routes?.length ? "Choose a line" : "Select a stop first"}</option>{selectedStop?.routes?.map((route) => <option value={route} key={route}>{route}</option>)}</select></label><button type="submit" disabled={lookupLoading || !stop.trim() || !line.trim()}>{lookupLoading ? "Checking..." : "Check stop"}</button></form>
      {lookup && <div className={`lookup-result ${lookup.status}`}><strong>{lookup.status === "anomalous" ? "Anomaly detected" : lookup.status === "on_time" ? "No anomaly detected" : "Live estimate"}</strong><span>{lookup.message} · {lookup.vehicle_count} matching vehicle{lookup.vehicle_count === 1 ? "" : "s"}.</span><div className="eta-result"><b>NEXT {line} BUS</b><strong>{lookup.eta_minutes === null ? "No estimate" : `${lookup.eta_minutes} min`}</strong><span>{lookup.eta_message}{lookup.eta_confidence ? ` · ${lookup.eta_confidence} confidence` : ""}</span></div></div>}
      {lookupError && <div className="errors">{lookupError}</div>}
    </section>
    <section className="panel alert-panel"><div className="panel-title"><span>PROACTIVE ACTION CENTER</span><span className="count">{state.alerts.filter((item) => item.source === "nemotron").length}</span></div>
      {active ? <div className="alert-card"><div className="warning">!</div><div className="alert-content"><span className="alert-label">NEMOTRON EARLY WARNING · {active.status.toUpperCase()}</span><h2>{active.message}</h2><p>{active.reasoning || "Live vehicle data indicates a developing service risk."}</p><p><strong>Routes:</strong> {active.affected_routes?.length ? active.affected_routes.join(", ") : "Route still being determined"} · <strong>Recommended:</strong> allow extra travel time and check again before departing.</p><div className="actions"><button onClick={() => execute(active, "draft_email")}>Draft email</button><button onClick={() => execute(active, "reschedule_calendar")}>Reschedule calendar</button></div>{actionError && <div className="errors">{actionError}</div>}</div></div> : <div className="empty"><span>✓</span><div><strong>No new AI-predicted delay</strong><p>Nemotron is watching for developing issues that PRT has not already posted.</p></div></div>}
    </section>
    {state.provider_errors.length > 0 && <div className="errors">Some live sources are unavailable. Fallback monitoring remains active: {state.provider_errors.join(" · ")}</div>}
    <footer>TRACK TRACKER <span>•</span> PRT BUS WATCH <span>•</span> PITTSBURGH REGION</footer>
  </main>;
}
