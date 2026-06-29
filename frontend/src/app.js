// Watch status page (ADR-011) — build-less React via ESM CDN, Tailwind via Play CDN
// (dark theme, matching the Django UI). Read-only posture: health + open-incident
// counts by tier, polled from /api/status. Honest degradation (ADR-005): a loud banner
// + ServiceNow fallback when the backend is unreachable; amber when a dependency degrades.
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

const BANNER = "rounded-lg p-4 mb-5 font-semibold";
const SMALL = "block font-normal text-sm mt-1 opacity-80";

function Banner({ reachable, ok, data }) {
  if (!reachable) {
    return html`<div class=${`${BANNER} bg-rose-500/15 text-rose-300`}>Backend unreachable — this page may be stale.
      <small class=${SMALL}>Use ServiceNow for live incident work until Watch is back.</small></div>`;
  }
  if (!ok || data?.status === "degraded") {
    const down = data ? Object.entries(data.checks).filter(([, v]) => !v).map(([k]) => k).join(", ") : "a dependency";
    return html`<div class=${`${BANNER} bg-amber-500/15 text-amber-300`}>Degraded — ${down} unavailable.
      <small class=${SMALL}>Intake still captures; the worked-incident view may lag.</small></div>`;
  }
  return html`<div class=${`${BANNER} bg-emerald-500/15 text-emerald-300`}>All systems operational</div>`;
}

const ACCENT = { warn: "text-amber-400", crit: "text-rose-400", good: "text-emerald-400" };

function Card({ label, value, accent }) {
  return html`<div class="bg-slate-900 border border-slate-800 rounded-lg p-4 text-center">
    <div class=${`text-3xl font-bold ${ACCENT[accent] || "text-slate-100"}`}>${value}</div>
    <div class="text-slate-500 text-sm mt-1">${label}</div>
  </div>`;
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
    <div class="max-w-3xl mx-auto p-8">
      <header class="flex items-baseline gap-3 mb-5">
        <h1 class="text-xl font-semibold text-slate-100">Watch · Status</h1>
        <span class="text-slate-500 text-sm">incident posture</span>
      </header>
      <${Banner} reachable=${s.reachable} ok=${s.ok} data=${s.data} />
      ${inc
        ? html`<div class="grid gap-3 [grid-template-columns:repeat(auto-fit,minmax(120px,1fr))]">
            <${Card} label="Open incidents" value=${inc.open} />
            <${Card} label="T1" value=${inc.by_tier.T1} />
            <${Card} label="T2" value=${inc.by_tier.T2} accent="warn" />
            <${Card} label="T3" value=${inc.by_tier.T3} accent="crit" />
            <${Card} label="Resolved (24h)" value=${inc.resolved_24h} accent="good" />
          </div>`
        : html`<p class="text-slate-500">${s.loading ? "Loading…" : "No data."}</p>`}
      <footer class="text-slate-600 text-xs mt-4">${s.data?.generated_at ? `updated ${new Date(s.data.generated_at).toLocaleTimeString()}` : ""}</footer>
    </div>`;
}

createRoot(document.getElementById("root")).render(html`<${StatusPage} />`);
