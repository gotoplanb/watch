// Honest degradation probe (ADR-005 / §4.4).
//
// The SPA on CloudFront stays up even when the app tier is down. That must never be
// mistaken for liveness. This polls the dependency-checked backend health endpoint
// (Postgres + Valkey) and reports a state the UI uses to show a loud read-only/stale
// banner + the "use ServiceNow" fallback when the backend can't actually serve.

export const Health = Object.freeze({
  OK: "ok",
  DEGRADED: "degraded",   // backend reachable but a dependency is down
  UNREACHABLE: "unreachable", // app tier unreachable (regional/AZ impact, deploy)
});

export async function probeHealth(signal) {
  try {
    const res = await fetch("/api/health", { signal, cache: "no-store" });
    if (res.ok) return Health.OK;
    // 503 from the health view => a dependency check failed.
    return Health.DEGRADED;
  } catch {
    return Health.UNREACHABLE;
  }
}

// Poll on an interval; callers render the banner from the returned state.
export function startHealthPolling(onChange, { intervalMs = 15000 } = {}) {
  let last = null;
  const tick = async () => {
    const state = await probeHealth();
    if (state !== last) {
      last = state;
      onChange(state);
    }
  };
  tick();
  const id = setInterval(tick, intervalMs);
  return () => clearInterval(id);
}
