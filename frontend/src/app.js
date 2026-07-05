// Watch status page (ADR-011) — build-less React via ESM CDN, Tailwind via Play CDN
// (dark theme, matching the Django UI). Read-only posture: health + open-incident counts by tier,
// pushed live over SSE from /api/status/stream (ADR-024), with a polling fallback to /api/status
// where EventSource is unavailable. Honest degradation (ADR-005): a loud banner + ServiceNow
// fallback when the backend is unreachable; amber when a dependency degrades.
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

const FIELD =
  "w-full bg-slate-950 border border-slate-800 rounded-md px-3 py-2 text-slate-100 text-sm " +
  "placeholder-slate-600 focus:outline-none focus:border-slate-600";

function TabButton({ active, onClick, children }) {
  const on = "bg-slate-800 text-slate-100";
  const off = "text-slate-400 hover:text-slate-200";
  return html`<button type="button" onClick=${onClick}
    class=${`px-3 py-1.5 rounded-md text-sm font-medium ${active ? on : off}`}>${children}</button>`;
}

// Public, unauthenticated self-service report (ADR-027): report an incident, or submit a session id
// for a trace check. Posts to the throttled anonymous endpoints; shows an honest ack/error, no verdict.
function ReportForm() {
  const [mode, setMode] = useState("incident"); // "incident" | "check"
  const [title, setTitle] = useState("");
  const [detail, setDetail] = useState("");
  const [session, setSession] = useState("");
  const [st, setSt] = useState({ status: "idle" }); // idle | sending | ok | error

  const submit = async (e) => {
    e.preventDefault();
    setSt({ status: "sending" });
    const incident = mode === "incident";
    const path = incident ? "/api/report/incident" : "/api/report/check";
    const body = incident ? { title, detail } : { session: session.trim() };
    try {
      const res = await fetch(`${API}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => null);
      if (res.status === 429) {
        setSt({ status: "error", msg: "Too many submissions — please wait a moment and retry." });
      } else if (!res.ok) {
        const msg = data ? Object.values(data).flat().join(" ") : "Submission failed.";
        setSt({ status: "error", msg });
      } else {
        setSt({ status: "ok", ref: (data?.id || "").slice(0, 8) });
        setTitle(""); setDetail(""); setSession("");
      }
    } catch {
      setSt({ status: "error", msg: "Could not reach Watch." });
    }
  };

  const busy = st.status === "sending";
  return html`
    <section class="mt-6 bg-slate-900 border border-slate-800 rounded-lg p-4">
      <h2 class="text-slate-200 font-semibold text-sm mb-1">Something wrong? Let us know</h2>
      <p class="text-slate-500 text-xs mb-3">No sign-in needed. Report an issue, or submit a session id for a trace check.</p>
      <div class="flex gap-1 mb-3">
        <${TabButton} active=${mode === "incident"} onClick=${() => { setMode("incident"); setSt({ status: "idle" }); }}>Report an incident<//>
        <${TabButton} active=${mode === "check"} onClick=${() => { setMode("check"); setSt({ status: "idle" }); }}>Check a session<//>
      </div>
      <form onSubmit=${submit} class="grid gap-2">
        ${mode === "incident"
          ? html`
            <input class=${FIELD} placeholder="What's broken? (short summary)" maxlength="200"
              required value=${title} onInput=${(e) => setTitle(e.target.value)} />
            <textarea class=${FIELD} rows="3" maxlength="2000" placeholder="Any details (optional)"
              value=${detail} onInput=${(e) => setDetail(e.target.value)}></textarea>`
          : html`
            <input class=${FIELD} placeholder="32-character session id from the app" pattern="[0-9a-fA-F]{32}"
              required value=${session} onInput=${(e) => setSession(e.target.value)} />`}
        <div class="flex items-center gap-3">
          <button type="submit" disabled=${busy}
            class="bg-slate-700 hover:bg-slate-600 disabled:opacity-50 text-slate-100 text-sm font-medium rounded-md px-4 py-2">
            ${busy ? "Sending…" : "Submit"}</button>
          ${st.status === "ok" && html`<span class="text-emerald-400 text-sm">Thanks — received${st.ref ? html` (ref ${st.ref})` : ""}.</span>`}
          ${st.status === "error" && html`<span class="text-rose-400 text-sm">${st.msg}</span>`}
        </div>
      </form>
    </section>`;
}

function StatusPage() {
  const [s, setS] = useState({ reachable: true, ok: true, data: null, loading: true });

  useEffect(() => {
    let alive = true;
    let es = null;
    let pollId = null;
    const apply = (r) => { if (alive) setS((prev) => ({ ...prev, ...r, loading: false })); };

    const startPolling = () => {
      if (pollId) return;
      const tick = async () => apply(await fetchStatus());
      tick();
      pollId = setInterval(tick, POLL_MS);
    };

    // Primary: one long-lived SSE connection; EventSource auto-reconnects on drop (ADR-024).
    if (typeof EventSource === "undefined") {
      startPolling();
    } else {
      es = new EventSource(`${API}/api/status/stream`);
      es.addEventListener("status", (ev) => {
        try {
          const data = JSON.parse(ev.data);
          apply({ reachable: true, ok: data.status !== "degraded", data });
        } catch { /* ignore malformed frame */ }
      });
      // On drop the browser retries automatically; flag stale meanwhile (keep last-known posture).
      es.onerror = () => apply({ reachable: false, ok: false });
    }

    return () => { alive = false; if (es) es.close(); if (pollId) clearInterval(pollId); };
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
      <${ReportForm} />
      <footer class="text-slate-600 text-xs mt-4">${s.data?.generated_at ? `updated ${new Date(s.data.generated_at).toLocaleTimeString()}` : ""}</footer>
    </div>`;
}

createRoot(document.getElementById("root")).render(html`<${StatusPage} />`);
