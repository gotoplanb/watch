// Watch status page (ADR-011) — build-less React via ESM CDN, no npm/Vite.
// Read-only posture: health + open-incident counts by tier, polled from /api/status.
// Honest degradation (ADR-005): a loud banner + ServiceNow fallback when the backend
// is unreachable; an amber banner when a dependency is degraded.
import React, { useEffect, useState } from "https://esm.sh/react@18.3.1";
import { createRoot } from "https://esm.sh/react-dom@18.3.1/client";
import htm from "https://esm.sh/htm@3.1.1";

const html = htm.bind(React.createElement);
const API = (window.WATCH_API || "http://localhost:8010").replace(/\/$/, "");
const POLL_MS = 10000;

async function fetchStatus() {
  try {
    const res = await fetch(`${API}/api/status`, { cache: "no-store" });
    const data = await res.json().catch(() => null);
    return { reachable: true, ok: res.ok, data };
  } catch {
    return { reachable: false, ok: false, data: null };
  }
}

function Banner({ reachable, ok, data }) {
  if (!reachable) {
    return html`<div class="banner unreachable">Backend unreachable — this page may be stale.
      <small>Use ServiceNow for live incident work until Watch is back.</small></div>`;
  }
  if (!ok || data?.status === "degraded") {
    const down = data ? Object.entries(data.checks).filter(([, v]) => !v).map(([k]) => k).join(", ") : "a dependency";
    return html`<div class="banner degraded">Degraded — ${down} unavailable.
      <small>Intake still captures; the worked-incident view may lag.</small></div>`;
  }
  return html`<div class="banner ok">All systems operational</div>`;
}

function Card({ label, value, accent }) {
  return html`<div class="card"><div class=${`value ${accent || ""}`}>${value}</div><div class="label">${label}</div></div>`;
}

function StatusPage() {
  const [s, setS] = useState({ reachable: true, ok: true, data: null, loading: true });

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      const r = await fetchStatus();
      if (alive) setS({ ...r, loading: false });
    };
    tick();
    const id = setInterval(tick, POLL_MS);
    return () => { alive = false; clearInterval(id); };
  }, []);

  const inc = s.data?.incidents;
  return html`
    <div class="wrap">
      <header><h1>Watch · Status</h1><span class="sub">incident posture</span></header>
      <${Banner} reachable=${s.reachable} ok=${s.ok} data=${s.data} />
      ${inc
        ? html`<div class="cards">
            <${Card} label="Open incidents" value=${inc.open} />
            <${Card} label="T1" value=${inc.by_tier.T1} />
            <${Card} label="T2" value=${inc.by_tier.T2} accent="warn" />
            <${Card} label="T3" value=${inc.by_tier.T3} accent="crit" />
            <${Card} label="Resolved (24h)" value=${inc.resolved_24h} accent="good" />
          </div>`
        : html`<p class="muted">${s.loading ? "Loading…" : "No data."}</p>`}
      <footer>${s.data?.generated_at ? `updated ${new Date(s.data.generated_at).toLocaleTimeString()}` : ""}</footer>
    </div>`;
}

createRoot(document.getElementById("root")).render(html`<${StatusPage} />`);
