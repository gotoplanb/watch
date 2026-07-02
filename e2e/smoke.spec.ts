import { test, expect } from "@playwright/test";

// Minimal post-deploy functional smoke (platform E2E gate). Exercises the core path end to end
// — health, status page, login, intake create, escalation — so RDS (read+write), Valkey
// (session), Step Functions + commit Lambda (escalate), the status SPA + CORS, and the app/ALB
// are each hit at least once. Runs locally (make dev) and against staging in the pipeline.
// MUTATES data — staging/local only, never prod. It tracks its OWN created incident (T1 -> T2)
// rather than the seed, because seeded incidents auto-escalate off T1 on their SLA timeout.
const BASE = process.env.BASE_URL || "http://localhost:8010";
const STATUS = process.env.STATUS_URL || BASE;
const SECRET = process.env.INTAKE_WEBHOOK_SECRET || "";
const USER = process.env.SMOKE_USER || "t1a";
const PASS = process.env.SMOKE_PASSWORD || "watch";

const tier = async (page: any, id: string) =>
  (await (await page.request.get(`${BASE}/api/incidents/${id}/`)).json()).current_tier;

test("smoke: health → status → login → create → escalate → T2", async ({ page }) => {
  // 1. Health — app + basic reachability.
  const health = await page.request.get(`${BASE}/api/health`);
  expect(health.status(), "health 200").toBe(200);

  // 2. Status posture — dependency checks green (RDS + Valkey).
  const s0 = await (await page.request.get(`${BASE}/api/status`)).json();
  expect(s0.checks.postgres, "postgres check").toBe(true);
  expect(s0.checks.valkey, "valkey check").toBe(true);

  // ...and the status SPA renders (S3/CloudFront + cross-origin fetch of the posture).
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
  expect(await tier(page, id), "new incident starts at T1").toBe("T1");

  // 5–6. Escalate via the UI (HTMX → real Step Functions SendTaskSuccess → commit Lambda), then
  // wait for T2. The task token is registered a beat after creation, so a too-early click
  // no-ops — retry the escalate WHILE still at T1 (the guard prevents a second escalate to T3).
  page.on("dialog", (d) => d.accept()); // the escalate button has an hx-confirm
  await page.goto(`${BASE}/ui/incidents/${id}/`);
  await expect
    .poll(
      async () => {
        if ((await tier(page, id)) === "T1") {
          await page
            .getByRole("button", { name: /escalate/i })
            .first()
            .click({ timeout: 5_000 })
            .catch(() => {});
          await page.waitForTimeout(3_000); // let the async escalate commit before re-checking
        }
        return tier(page, id);
      },
      { message: "our incident escalated to T2", timeout: 120_000, intervals: [5_000] }
    )
    .toBe("T2");

  // ...and the public posture reflects at least one T2 (our incident).
  const s1 = await (await page.request.get(`${BASE}/api/status`)).json();
  expect(s1.incidents.by_tier.T2, "posture shows a T2").toBeGreaterThanOrEqual(1);
});
