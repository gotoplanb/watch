import { test, expect, Page } from "@playwright/test";

// Browser coverage for the /ui/ sub-flows that otherwise only had Django unit coverage (#36):
// the HTMX list filter, add-note, explicit ACK (ADR-007), live timeline swaps, the status page's
// honest-degradation banners (ADR-005), settings key rotation, and the on-call schedule page.
//
// Every incident-backed test creates its OWN incident through the intake webhook with a unique
// source_event_id, so tests are independent, re-runnable, and never lean on seeded data state.
// Untagged-equivalent (@local) → runs locally AND on staging per the #30 tiering. MUTATES data —
// staging/local only, never prod.
const BASE = process.env.BASE_URL || "http://localhost:8010";
const STATUS = process.env.STATUS_URL || BASE;
const SECRET = process.env.INTAKE_WEBHOOK_SECRET || "";
const USER = process.env.SMOKE_USER || "t1a";
const PASS = process.env.SMOKE_PASSWORD || "watch";

async function login(page: Page, user = USER) {
  await page.goto(`${BASE}/api-auth/login/?next=/ui/incidents/`);
  await page.fill("#id_username", user);
  await page.fill("#id_password", PASS);
  await page
    .locator("form")
    .filter({ has: page.locator("#id_username") })
    .first()
    .evaluate((f: HTMLFormElement) => f.requestSubmit());
  // path-anchored: a regex would also match the login page's ?next=… query
  await page.waitForURL((u) => u.pathname === "/ui/incidents/");
}

/** Create a fresh T1/OPEN incident via intake; returns its id + unique title. */
async function createIncident(page: Page, prefix: string) {
  const eid = `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const title = `E2E ${eid}`;
  const created = await page.request.post(`${BASE}/api/intake/webhook`, {
    headers: { "X-Watch-Webhook-Secret": SECRET },
    data: { source: "e2e", title, source_event_id: eid, payload: { e2e: true } },
  });
  expect([200, 201], "intake create").toContain(created.status());
  const id = (await created.json()).id as string;
  expect(id, "new incident id").toBeTruthy();
  return { id, eid, title };
}

const api = async (page: Page, id: string) =>
  (await (await page.request.get(`${BASE}/api/incidents/${id}/`)).json()) as {
    status: string;
    current_tier: string;
    acknowledged_at: string | null;
    sla_deadline_at: string | null;
  };

/** Plant a marker on `window`; a full page load wipes it, an HTMX swap doesn't. */
async function markDocument(page: Page) {
  const token = `e2e-${Date.now()}`;
  await page.evaluate((t) => ((window as any).__e2eDoc = t), token);
  return async () =>
    expect(
      await page.evaluate(() => (window as any).__e2eDoc),
      "same document — updated by an HTMX swap, not a full reload"
    ).toBe(token);
}

// The timeline is the <ol> under the "Timeline" heading in the swappable #incident-body.
const timeline = (page: Page) => page.locator("#incident-body ol").first();

// --- 1. HTMX list filter ------------------------------------------------------------------------

test("incident list: HTMX status/tier filter swaps in the matching rows", { tag: "@local" }, async ({ page }) => {
  await login(page);
  const { title, eid } = await createIncident(page, "filter");
  await page.goto(`${BASE}/ui/incidents/`);
  const sameDoc = await markDocument(page);

  // The desktop table is the visible rendering at Playwright's default 1280px viewport (cards are md:hidden).
  const results = page.locator("#incident-results table");
  const ourRow = results.getByRole("link", { name: title });
  const empty = results.getByText("No incidents match.");

  // Each control change fires an hx-get carrying HX-Request → the server answers with the partial.
  const swap = () =>
    page.waitForResponse(
      (r) => r.url().includes("/ui/incidents/?") && r.request().headers()["hx-request"] === "true"
    );

  // Narrow to our incident first so the filter assertions are about OUR row, not the seed.
  let resp = swap();
  await page.getByLabel("Search incidents").fill(eid);
  await resp;
  await expect(ourRow).toBeVisible();

  // status=RESOLVED → our OPEN incident drops out.
  resp = swap();
  await page.getByLabel("Filter by status").selectOption("RESOLVED");
  expect((await resp).url()).toContain("status=RESOLVED");
  await expect(ourRow).toHaveCount(0);
  await expect(empty).toBeVisible();

  // status=OPEN → back.
  resp = swap();
  await page.getByLabel("Filter by status").selectOption("OPEN");
  await resp;
  await expect(ourRow).toBeVisible();

  // tier=T2 → a fresh incident is at T1, so it drops out again.
  resp = swap();
  await page.getByLabel("Filter by tier").selectOption("T2");
  expect((await resp).url()).toContain("tier=T2");
  await expect(ourRow).toHaveCount(0);
  await expect(empty).toBeVisible();

  // tier=T1 → our row, showing Open + T1.
  resp = swap();
  await page.getByLabel("Filter by tier").selectOption("T1");
  await resp;
  await expect(ourRow).toBeVisible();
  const row = results.locator("tr").filter({ has: page.getByRole("link", { name: title }) });
  await expect(row).toContainText("Open");
  await expect(row).toContainText("T1");

  // The swap replaced the results block in place — the filter form itself (outside it) persists.
  await sameDoc();
  expect(new URL(page.url()).pathname).toBe("/ui/incidents/");
});

// --- 2. Add a note ------------------------------------------------------------------------------

test("incident detail: add a note → it lands in the timeline", { tag: "@local" }, async ({ page }) => {
  await login(page);
  const { id } = await createIncident(page, "note");
  await page.goto(`${BASE}/ui/incidents/${id}/`);

  const note = `Checked the dashboards at ${Date.now()}; p99 latency is flat.`;
  const box = page.getByPlaceholder("Add a note…");
  const add = page.getByRole("button", { name: "Add note" });
  await expect(add, "empty note can't be submitted").toBeDisabled();
  await box.fill(note);
  await add.click();

  const entry = timeline(page).locator("li").filter({ hasText: note });
  await expect(entry).toBeVisible();
  await expect(entry, "note is attributed to the author").toContainText(USER);

  // Survives a reload — it was persisted, not just rendered.
  await page.reload();
  await expect(timeline(page).getByText(note)).toBeVisible();
});

// --- 3. Explicit ACK (ADR-007) ------------------------------------------------------------------

test("incident detail: ACK marks acknowledged but keeps the tier and the incident actionable", { tag: "@local" }, async ({ page }) => {
  await login(page);
  const { id } = await createIncident(page, "ack");
  await page.goto(`${BASE}/ui/incidents/${id}/`);

  const body = page.locator("#incident-body");
  await expect(body.getByText(/^Acknowledged /)).toHaveCount(0);
  const before = await api(page, id);
  await page.getByRole("button", { name: "Acknowledge" }).click();

  // Acknowledged state shows…
  await expect(body.getByText(/^Acknowledged /)).toBeVisible();
  // …ACK is a one-shot: the button is gone (re-acking is a no-op server-side anyway)…
  await expect(page.getByRole("button", { name: "Acknowledge" })).toHaveCount(0);
  // …the tier is UNCHANGED and the incident is still OPEN — ACK doesn't consume the token…
  await expect(body.getByText("Open", { exact: true }).first()).toBeVisible();
  await expect(body.getByText("T1", { exact: true }).first()).toBeVisible();
  // …and it's still actionable: escalate + resolve remain on offer.
  await expect(page.getByRole("button", { name: /escalate to t2/i }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: /^resolve$/i }).first()).toBeVisible();

  // The API agrees.
  const inc = await api(page, id);
  expect(inc.acknowledged_at, "acknowledged_at set").toBeTruthy();
  expect(inc.current_tier, "tier unchanged by ACK").toBe("T1");
  expect(inc.status, "still open after ACK").toBe("OPEN");

  // ACK does not stop the SLA clock (ADR-007). The deadline only exists once Step Functions has
  // registered the tier's token (cloud engine; `make dev`'s local mode has none), so this is
  // env-agnostic: whatever deadline existed before the ACK is still there after it, and still shown.
  if (before.sla_deadline_at) {
    expect(inc.sla_deadline_at, "SLA deadline untouched by ACK").toBe(before.sla_deadline_at);
  }
  if (inc.sla_deadline_at) {
    await page.reload();
    await expect(body.getByText(/^SLA deadline /)).toBeVisible();
  }
});

// --- 4. Live timeline update (HTMX swap, no reload) ---------------------------------------------

test("incident detail: actions update the timeline live, without a full page reload", { tag: "@local" }, async ({ page }) => {
  await login(page);
  const { id } = await createIncident(page, "live");
  await page.goto(`${BASE}/ui/incidents/${id}/`);

  const sameDoc = await markDocument(page);
  let navigations = 0;
  page.on("framenavigated", (f) => {
    if (f === page.mainFrame()) navigations++;
  });

  // ACK → its transition line ("T1 → T1 … — acknowledged") appears in the timeline.
  await page.getByRole("button", { name: "Acknowledge" }).click();
  await expect(timeline(page).locator("li").filter({ hasText: "— acknowledged" })).toBeVisible();

  // Note → it appears, newest-first, above the ACK line.
  const note = `live-update ${Date.now()}`;
  await page.getByPlaceholder("Add a note…").fill(note);
  await page.getByRole("button", { name: "Add note" }).click();
  const items = timeline(page).locator("li");
  await expect(items.first(), "newest entry first (ADR-040)").toContainText(note);
  await expect(items.filter({ hasText: "— acknowledged" })).toBeVisible();

  await sameDoc();
  expect(navigations, "no main-frame navigation happened").toBe(0);
});

// --- 5. Status page honest degradation (ADR-005) ------------------------------------------------

async function statusPageUp(page: Page) {
  try {
    return (await page.request.get(`${STATUS}/`, { timeout: 5_000 })).ok();
  } catch {
    return false;
  }
}

test("status page: a degraded posture shows the amber Degraded banner", { tag: "@local" }, async ({ page }) => {
  test.skip(!(await statusPageUp(page)), `status page unreachable at ${STATUS} (run \`make status-page\`)`);
  // Force the polling fallback (no EventSource) so the stubbed /api/status is the one source of truth;
  // an SSE stream stub would close after one frame and flip to the red unreachable banner.
  await page.addInitScript(() => {
    (window as any).EventSource = undefined;
  });
  await page.route("**/api/status", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify({
        status: "degraded",
        checks: { postgres: true, valkey: false },
        generated_at: new Date().toISOString(),
      }),
    })
  );
  await page.goto(`${STATUS}/?e2e=${Date.now()}`);

  const banner = page.getByText(/^Degraded — /);
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("valkey unavailable");
  await expect(banner).toHaveClass(/amber/);
  await expect(page.getByText("All systems operational")).toHaveCount(0);
  await expect(page.getByText(/Backend unreachable/)).toHaveCount(0);
});

test("status page: an unreachable backend shows the red banner + the team's fallback", { tag: "@local" }, async ({ page }) => {
  test.skip(!(await statusPageUp(page)), `status page unreachable at ${STATUS} (run \`make status-page\`)`);
  // Both feeds fail: the SSE stream (primary) and the polling endpoint (fallback).
  await page.route("**/api/status**", (route) => route.abort("connectionrefused"));
  await page.goto(`${STATUS}/?e2e=${Date.now()}`);

  const banner = page.getByText(/^Backend unreachable/);
  await expect(banner).toBeVisible();
  await expect(banner).toHaveClass(/rose/);
  await expect(page.getByText("Follow your team's documented fallback for live incident work until Watch is back.")).toBeVisible();
  // ADR-049: Watch is a peer system — the fallback copy names no external system.
  expect(await page.locator("body").innerText()).not.toMatch(/servicenow/i);
  await expect(page.getByText("All systems operational")).toHaveCount(0);
});

// --- 6. Settings + schedule ---------------------------------------------------------------------

// Rotation rolls the user's API key, MCP key AND ntfy topic — run it as a low-traffic seeded user
// (default t3b), never the primary smoke user, so it doesn't churn credentials anyone relies on.
// Against a deployed env it only runs when SMOKE_ROTATE_USER names a throwaway user explicitly —
// otherwise every pipeline Smoke would rotate a real person's keys + paging topic.
test("settings: rotating keys replaces the shown API + MCP keys", { tag: "@local" }, async ({ page }) => {
  const local = /\/\/(localhost|127\.0\.0\.1|host\.docker\.internal)(:|\/|$)/.test(BASE);
  test.skip(!local && !process.env.SMOKE_ROTATE_USER, "deployed env: set SMOKE_ROTATE_USER to a throwaway user to rotate");
  await login(page, process.env.SMOKE_ROTATE_USER || "t3b");
  await page.goto(`${BASE}/ui/settings/`);
  const rotate = page.getByRole("button", { name: "Rotate my keys" });
  test.skip((await rotate.count()) === 0, "API keys not configured (no API_KEY_SECRET) — nothing to rotate");

  const keyAfterLabel = (label: string) =>
    page.getByText(label, { exact: true }).locator("xpath=following-sibling::div[1]");
  const apiKey = keyAfterLabel("API key");
  const mcpKey = keyAfterLabel("MCP key (bearer)");
  const before = { api: (await apiKey.innerText()).trim(), mcp: (await mcpKey.innerText()).trim() };
  expect(before.api, "an API key is shown").not.toBe("");
  expect(before.mcp, "an MCP key is shown").not.toBe("");

  page.once("dialog", (d) => d.accept()); // the "Rotate your keys?" confirm
  await Promise.all([page.waitForURL((u) => u.pathname === "/ui/settings/"), rotate.click()]);
  await page.waitForLoadState("load");

  await expect(apiKey).not.toHaveText(before.api);
  await expect(mcpKey).not.toHaveText(before.mcp);
  expect((await apiKey.innerText()).trim(), "a new API key is shown").not.toBe("");
});

test("schedule: the on-call page renders the per-tier now-cards, the add-shift form and the rota", { tag: "@local" }, async ({ page }) => {
  await login(page);
  await page.goto(`${BASE}/ui/schedule/`);
  await expect(page.getByRole("heading", { name: "On-call schedule" })).toBeVisible();
  for (const tier of ["T1", "T2", "T3"]) {
    await expect(page.getByText(`${tier} on-call now`)).toBeVisible();
  }
  await expect(page.getByLabel("Shift tier")).toBeVisible();
  await expect(page.getByLabel("Shift user")).toBeVisible();
  await expect(page.getByRole("button", { name: "Add shift" })).toBeVisible();
  const table = page.locator("#schedule-body table");
  await expect(table.getByRole("columnheader", { name: "Tier" })).toBeVisible();
  await expect(table.getByRole("columnheader", { name: "User" })).toBeVisible();
});
