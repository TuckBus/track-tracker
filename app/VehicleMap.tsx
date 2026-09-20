"use client";

import { CircleMarker, MapContainer, Pane, Popup, TileLayer } from "react-leaflet";
import type { TelemetryRecord } from "../lib/types";

const center: [number, number] = [40.4406, -79.9959];
const points = [
  { name: "Downtown bus hub", kind: "Bus stop", position: [40.4406, -79.9959] as [number, number] },
  { name: "Steel Plaza", kind: "PRT bus stop", position: [40.4397, -79.9973] as [number, number] },
];

type Stop = { id: string; name: string; latitude: number; longitude: number };

export default function VehicleMap({ vehicles, stops, delayedRoutes, nemotronRoutes }: { vehicles: TelemetryRecord[]; stops: Stop[]; delayedRoutes: string[]; nemotronRoutes: string[] }) {
  const positioned = vehicles.filter((vehicle) => vehicle.latitude !== null && vehicle.longitude !== null);
  return <MapContainer center={center} zoom={11} scrollWheelZoom preferCanvas className="leaflet-map" aria-label="Live map of tracked vehicles">
    <TileLayer attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" />
    {points.map((point) => <CircleMarker center={point.position} radius={6} pathOptions={{ color: "#f4c95d", fillColor: "#18534a", fillOpacity: 0.9 }} key={point.name}>
      <Popup><strong>{point.name}</strong><br />{point.kind}</Popup>
    </CircleMarker>)}
    <Pane name="stops" style={{ zIndex: 300 }}>{stops.map((stop) => <CircleMarker center={[stop.latitude, stop.longitude]} radius={2.5} pathOptions={{ color: "#355b67", fillColor: "#8dd6d2", fillOpacity: 0.65, weight: 1 }} key={`stop-${stop.id}`}>
      <Popup><strong>{stop.name}</strong><br />PRT bus stop<br />Stop ID: {stop.id}</Popup>
    </CircleMarker>)}</Pane>
    <Pane name="vehicles" style={{ zIndex: 500 }}>{positioned.map((vehicle) => {
      const route = vehicle.route?.toLowerCase();
      const prtIssue = vehicle.source === "prt" && route !== undefined && delayedRoutes.some((item) => item.toLowerCase() === route);
      const nemotronIssue = route !== undefined && nemotronRoutes.some((item) => item.toLowerCase() === route);
      const issueColor = nemotronIssue ? "#ff8066" : prtIssue ? "#f4c95d" : "#5ee0ad";
      return <CircleMarker center={[vehicle.latitude as number, vehicle.longitude as number]} radius={prtIssue || nemotronIssue ? 10 : 8} pathOptions={{ color: issueColor, fillColor: "#18534a", fillOpacity: 1 }} key={`${vehicle.source}-${vehicle.vehicle_id}`}>
      <Popup pane="popupPane"><strong>{vehicle.source.toUpperCase()} {vehicle.vehicle_id}</strong><br />Line: {vehicle.route || "—"}<br />Speed: {vehicle.speed_mph === null ? "—" : `${vehicle.speed_mph.toFixed(1)} mph`}<br />Observed: {new Date(vehicle.observed_at).toLocaleTimeString()}</Popup>
    </CircleMarker>;
    })}</Pane>
  </MapContainer>;
}
