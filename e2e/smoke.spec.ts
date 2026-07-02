import { test, expect } from "@playwright/test";

// Minimal post-deploy functional smoke (platform#… E2E gate). Exercises the core path end to
// end — health, the status page, login, intake create, escalation — so RDS (read+write),
// Valkey (session), Step Functions + commit Lambda (escalate), the status SPA + CORS, and the
// app/ALB are each hit at least once. Runs locally (make dev) and against staging in the
// pipeline; parameterized by env. MUTATES data — staging/local only, never prod.
const BASE = process.env.BASE_URL || "http://localhost:8010";
const STATUS = process.env.STATUS_URL || BASE;
const SECRET = process.env.INTAKE_WEBHOOK_SECRET || "";
const USER = process.env.SMOKE_USER || "t1a";
const PASS = process.env.SMOKE_PASSWORD || "watch";

const tierT1 = (s: any) => s.incidents.by_tier.T1 as number;
const tierT2 = (s: any) => s.incidents.by_tier.T2 as number;

test("smoke: health → status → login → create → escalate → T2", async ({ page }) => {
  // 1. Health — app + basic reachability.
  const health = await page.request.get(`${BASE}/api/health`);
  expect(health.status(), "health 200").toBe(200);

  // 2. Status posture — RDS read + dependency checks; the seed leaves one OPEN T1 incident.
  const s0 = await (await page.request.get(`${BASE}/api/status`)).json();
  expect(s0.checks.postgres, "postgres check").toBe(true);
  expect(s0.checks.valkey, "valkey check").toBe(true);
  expect(tierT1(s0), "seed T1 incident present").toBeGreaterThanOrEqual(1);

  // ...and the status SPA renders (S3/CloudFront + cross-origin fetch).
  await page.goto(`${STATUS}/?smoke=${Date.now()}`);
  await expect(page.getByText(/operational|status/i).first()).toBeVisible();

  // 3. Log in as a T1 responder (session cookie → Valkey).
  await page.goto(`${BASE}/api-auth/login/?next=/ui/incidents/`);
  await page.fill("#id_username", USER);
  await page.fill("#id_password", PASS);
  await page
    .locator("form")
    .filter({ has: page.locator("#id_username") })
    .first()
    .evaluate((f: HTMLFormElement) => f.requestSubmit());
  await expect(page, "logged in → incidents").toHaveURL(/\/ui\/incidents\/?$/);

  // 4. Create an incident via intake (intake logic + RDS write + Step Functions start).
  const eid = `smoke-${Date.now()}`;
  const created = await page.request.post(`${BASE}/api/intake/webhook`, {
    headers: { "X-Watch-Webhook-Secret": SECRET },
    data: { source: "smoke", title: `Smoke ${eid}`, source_event_id: eid, payload: { smoke: true } },
  });
  expect([200, 201], "intake create").toContain(created.status());
  const id = (await created.json()).id;
  expect(id, "new incident id").toBeTruthy();

  // 5. Escalate it via the UI (HTMX → real Step Functions SendTaskSuccess → commit Lambda).
  page.on("dialog", (d) => d.accept()); // the escalate button has an hx-confirm
  await page.goto(`${BASE}/ui/incidents/${id}/`);
  await page.getByRole("button", { name: /escalate/i }).first().click();

  // 6. Escalation is async through Step Functions — poll the status posture until a T2 appears.
  await expect
    .poll(async () => tierT2(await (await page.request.get(`${BASE}/api/status`)).json()), {
      message: "incident escalated to T2",
      timeout: 90_000,
      intervals: [3_000],
    })
    .toBeGreaterThanOrEqual(1);
});
