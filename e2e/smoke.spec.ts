import { test, expect } from "@playwright/test";

// Minimal post-deploy functional smoke (platform E2E gate). Exercises the core path end to end
// — health, status page, login, intake create, escalation — so RDS (read+write), Valkey
// (session), Step Functions + commit Lambda (escalate), the status SPA + CORS, and the app/ALB
// are each hit at least once. Runs locally (make dev) and against staging in the pipeline.
// MUTATES data — staging/local only, never prod. It tracks its OWN created incident (T1 -> T2)
// rather than the seed, because seeded incidents auto-escalate off T1 on their SLA timeout.
//
// Suite tiering (#30): tag by MINIMUM environment. Untagged / @local run everywhere; @staging marks
// tests that need AWS-managed behavior or long waits and run ONLY on staging. Local (`make e2e`,
// pre-commit) filters with --grep-invert=@staging; the pipeline Smoke stage runs the full superset,
// so local is always a subset of staging.
const BASE = process.env.BASE_URL || "http://localhost:8010";
const STATUS = process.env.STATUS_URL || BASE;
const SECRET = process.env.INTAKE_WEBHOOK_SECRET || "";
// The checks webhook validates its OWN secret (distinct from intake on real envs; equal locally).
// Fall back to SECRET so the local loop, where both are the same dev value, still works.
const CHECKS_SECRET = process.env.CHECKS_WEBHOOK_SECRET || SECRET;
const USER = process.env.SMOKE_USER || "t1a";
const PASS = process.env.SMOKE_PASSWORD || "watch";

const tier = async (page: any, id: string) =>
  (await (await page.request.get(`${BASE}/api/incidents/${id}/`)).json()).current_tier;

test("smoke: health → status → login → create → escalate → T2", { tag: "@local" }, async ({ page }) => {
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
  // path-anchored: the old regex also matched the login page's ?next=… query
  await page.waitForURL((u) => u.pathname === "/ui/incidents/");

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
  // wait for T2. Escalating is now TWO steps (ADR-041): the button opens a sheet carrying the
  // optional reason textarea, and the sheet's submit posts. We leave the reason blank here —
  // optional must stay optional, so the smoke asserts the empty path still escalates.
  // The task token is registered a beat after creation, so a too-early click no-ops — retry the
  // escalate WHILE still at T1 (the guard prevents a second escalate to T3).
  await page.goto(`${BASE}/ui/incidents/${id}/`);
  await expect
    .poll(
      async () => {
        if ((await tier(page, id)) === "T1") {
          // open the escalate sheet (first match = the action button, not the sheet's submit)
          await page
            .getByRole("button", { name: /escalate to/i })
            .first()
            .click({ timeout: 5_000 })
            .catch(() => {});
          // submit it with no reason — optional must stay optional (the sheet's own confirm)
          await page
            .getByTestId("escalate-confirm")
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

// The responder's journey through the two sheets (ADR-041/042): escalating states WHY, resolving
// states WHAT FIXED IT, and both sentences must survive all the way into the RCA document — which is
// the entire reason we ask for them. Deliberately env-agnostic: it asserts the human words land, not
// which provider wrote the handoff brief or whether it was queued (that varies by env, and is
// covered by units). Runs as t2a, who holds T2 and so can act on a T1 incident (tier-or-higher).
test("responder journey: escalation reason + resolve reason reach the RCA", { tag: "@local" }, async ({ page }) => {
  const t2user = process.env.SMOKE_T2_USER || "t2a";
  await page.goto(`${BASE}/api-auth/login/?next=/ui/incidents/`);
  await page.fill("#id_username", t2user);
  await page.fill("#id_password", PASS);
  await page
    .locator("form")
    .filter({ has: page.locator("#id_username") })
    .first()
    .evaluate((f: HTMLFormElement) => f.requestSubmit());
  await page.waitForURL((u) => u.pathname === "/ui/incidents/");

  const eid = `journey-${Date.now()}`;
  const created = await page.request.post(`${BASE}/api/intake/webhook`, {
    headers: { "X-Watch-Webhook-Secret": SECRET },
    data: { source: "smoke", title: `Journey ${eid}`, source_event_id: eid, payload: {} },
  });
  const id = (await created.json()).id;
  await page.goto(`${BASE}/ui/incidents/${id}/`);

  const why = "Replayed the failing session; the errors are inside the vendor SDK, not our code.";
  const fix = "Pinned the vendor SDK to 4.2.1 and redeployed; error rate back to baseline.";

  // Escalate — the sheet IS the confirm, and it carries the optional reason. Retry while still at
  // T1: the task token is registered a beat after creation, so a too-early click no-ops.
  await expect
    .poll(
      async () => {
        if ((await tier(page, id)) === "T1") {
          await page.getByRole("button", { name: /escalate to/i }).first().click({ timeout: 5_000 }).catch(() => {});
          await page.getByLabel(/why are you escalating/i).fill(why).catch(() => {});
          await page.getByTestId("escalate-confirm").click({ timeout: 5_000 }).catch(() => {});
          await page.waitForTimeout(3_000);
        }
        return tier(page, id);
      },
      { message: "escalated to T2 with a reason", timeout: 120_000, intervals: [5_000] }
    )
    .toBe("T2");
  // The reason lands in TWO legitimate places once the handoff brief is working: the T1→T2
  // transition line AND the handoff card, which folds the escalation reason into the incoming
  // tier's context (ADR-042). `.first()` asserts "it surfaced" without a strict-mode violation.
  await expect(page.getByText(why, { exact: false }).first()).toBeVisible();

  // Resolve — same pattern, asking what actually fixed it.
  await page.getByRole("button", { name: /^resolve$/i }).first().click();
  await page.getByLabel(/what fixed it/i).fill(fix);
  await page.getByTestId("resolve-confirm").click();
  await expect
    .poll(async () => (await (await page.request.get(`${BASE}/api/incidents/${id}/`)).json()).status, {
      message: "incident resolved",
      timeout: 30_000,
    })
    .toBe("RESOLVED");

  // Both human sentences reach the RCA assembly — the point of asking.
  const rca = await (await page.request.get(`${BASE}/ui/incidents/${id}/rca.md`)).text();
  expect(rca, "escalation reason in the RCA").toContain(why);
  expect(rca, "resolve reason in the RCA").toContain(fix);
});

// Session Check dogfood (ADR-022/023): a passing browser session self-reports to the inbound
// Session Check webhook — proving the outbound(from test)->inbound(check) round-trip AND, since the
// check emits `check.completed` to any subscription, the outbound webhook path. Runs everywhere.
test("session check dogfood: report the session for an error-span check", { tag: "@local" }, async ({ page }) => {
  await page.goto(`${BASE}/api-auth/login/?next=/ui/incidents/`);
  await page.fill("#id_username", USER);
  await page.fill("#id_password", PASS);
  await page
    .locator("form")
    .filter({ has: page.locator("#id_username") })
    .first()
    .evaluate((f: HTMLFormElement) => f.requestSubmit());
  // path-anchored: the old regex also matched the login page's ?next=… query
  await page.waitForURL((u) => u.pathname === "/ui/incidents/");

  // the middleware minted a non-secret session correlation id, exposed in the header to self-report
  const sessionId = await page.locator("[data-session-id]").first().getAttribute("data-session-id");
  expect(sessionId, "session correlation id present").toBeTruthy();

  // fire it at the inbound Session Check webhook (source=e2e) — the round-trip health check
  const resp = await page.request.post(`${BASE}/api/checks/webhook`, {
    headers: { "X-Watch-Webhook-Secret": CHECKS_SECRET },
    data: { subject_kind: "session", subject: sessionId, source: "e2e" },
  });
  expect(resp.status(), "session check accepted").toBe(201);
});

// STAGING-ONLY (#30): real SLA-timeout auto-escalation through the tiers, driven by the Step
// Functions timers + commit Lambda — minutes-to-many-minutes of waiting and AWS-managed behavior
// we can't (and don't want to) reproduce in `make dev`. Locally we cover MANUAL escalation (above);
// this proves the engine actually auto-escalates on its own. Skipped locally via --grep-invert=@staging.
// TODO(#30): implement against staging — create an incident, wait out each tier SLA, assert
// current_tier advances T1 → T2 → T3 without human action, and that a Transition/system event lands.
test.fixme("auto-escalation walks T1 → T2 → T3 on SLA timeout", { tag: "@staging" }, async ({ page }) => {
  void page;
});

// The async worker actually RUNS (platform#61). Everything else in this suite can pass while the
// queue seam is dead: the API answers, the page renders, the escalation commits. The handoff brief
// is the one artifact NOTHING but the worker can produce — the card is reserved PENDING on the
// escalate request and filled off the hot path (ADR-042) — so this asserts it reaches `ready`.
//
// This is the test that was missing. In the cloud the worker service ran the `bootstrap` placeholder
// image, `manage.py` exited 0 on the unknown `run_sqs_worker` command, ECS read exit-0 as a clean
// completion and reported "steady state" between restarts — so every count-and-health check we had
// went green while the brief was never written. Only asking the UI for the worker's OUTPUT catches
// that. Env-agnostic on purpose: whoever writes the brief (thread locally, SQS worker on staging),
// it must appear — a brief stuck at PENDING is a dead worker, and that is exactly the failure.
test("the handoff brief lands — proof the async worker is alive", { tag: "@local" }, async ({ page }) => {
  const t2user = process.env.SMOKE_T2_USER || "t2a";
  await page.goto(`${BASE}/api-auth/login/?next=/ui/incidents/`);
  await page.fill("#id_username", t2user);
  await page.fill("#id_password", PASS);
  await page
    .locator("form")
    .filter({ has: page.locator("#id_username") })
    .first()
    .evaluate((f: HTMLFormElement) => f.requestSubmit());
  await page.waitForURL((u) => u.pathname === "/ui/incidents/");

  const eid = `brief-${Date.now()}`;
  const created = await page.request.post(`${BASE}/api/intake/webhook`, {
    headers: { "X-Watch-Webhook-Secret": SECRET },
    data: { source: "smoke", title: `Brief ${eid}`, source_event_id: eid, payload: {} },
  });
  const id = (await created.json()).id;
  await page.goto(`${BASE}/ui/incidents/${id}/`);

  // Escalate to engage T2 — that is what reserves the handoff card.
  await expect
    .poll(
      async () => {
        if ((await tier(page, id)) === "T1") {
          await page.getByRole("button", { name: /escalate to/i }).first().click({ timeout: 5_000 }).catch(() => {});
          await page.getByTestId("escalate-confirm").click({ timeout: 5_000 }).catch(() => {});
          await page.waitForTimeout(3_000);
        }
        return tier(page, id);
      },
      { message: "escalated to T2", timeout: 120_000, intervals: [5_000] }
    )
    .toBe("T2");

  // The card must EXIST — the escalate path always reserves it, so its absence is a different bug.
  await expect(
    page.getByTestId("handoff-pending").or(page.getByTestId("handoff-ready")).first(),
    "the handoff card is reserved on escalate"
  ).toBeVisible({ timeout: 15_000 });

  // ...and it must get FILLED. The page polls itself every 2s while pending (htmx), so a live worker
  // flips this without a reload. Still pending when the clock runs out = the worker is not consuming.
  await expect(
    page.getByTestId("handoff-ready"),
    "the brief was written — a worker consumed the job"
  ).toBeVisible({ timeout: 90_000 });

  await expect(page.getByTestId("handoff-failed"), "the brief did not fail").toHaveCount(0);
  expect((await page.getByTestId("handoff-ready").innerText()).trim().length, "the brief has content").toBeGreaterThan(0);
});
