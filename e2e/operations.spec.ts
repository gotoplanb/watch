import { test, expect, Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const BASE = process.env.BASE_URL || "http://localhost:8010";
const PASSWORD = process.env.SMOKE_PASSWORD || "watch";
const SECRET = process.env.INTAKE_WEBHOOK_SECRET || "dev-webhook-secret";
const SHOTS = process.env.OPERATIONS_SCREENSHOT_DIR || "/tmp/watch-operations-screenshots";

async function login(page: Page, user = "t2a") {
  await page.goto(`${BASE}/api-auth/login/?next=/ui/assessments/`);
  await page.fill("#id_username", user);
  await page.fill("#id_password", PASSWORD);
  await page.locator("form").filter({ has: page.locator("#id_username") }).evaluate((f: HTMLFormElement) => f.requestSubmit());
  await page.waitForURL(u => u.pathname === "/ui/assessments/");
}

async function configure(page: Page, kind: string, values: Record<string, string | boolean>) {
  await page.goto(`${BASE}/ui/automation/?kind=${kind}`);
  for (const [key, value] of Object.entries(values)) {
    const field = page.locator(`#config-${key}`);
    if (typeof value === "boolean") await field.setChecked(value);
    else if (await field.evaluate(el => el.tagName) === "SELECT") await field.selectOption(value);
    else await field.fill(value);
  }
  await page.getByRole("button", { name: "Save configuration", exact: true }).click();
  await expect(page.locator(".errorlist")).toHaveCount(0);
  await expect(page.locator("#config-name")).toHaveValue("");
}

async function signal(page: Page, fields: Record<string, unknown>) {
  const result = await page.request.post(`${BASE}/api/signals/intake`, {
    headers: { "X-Watch-Webhook-Secret": SECRET },
    data: { title: "Synthetic browser signal", source: "monitor", signal: "Synthetic evidence-driven concern.", payload: { fixture: true }, source_event_id: `browser-${Date.now()}`, ...fields },
  });
  expect(result.status()).toBe(201);
  return (await result.json()).assessments;
}

async function checkLayout(page: Page, name: string) {
  await page.waitForLoadState("networkidle");
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
  const result = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(result.violations.map(v => ({ id: v.id, nodes: v.nodes.map(n => n.html) }))).toEqual([]);
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
}

test("policy-driven healthy assessment auto-delivers locally and closes without an incident", { tag: "@local" }, async ({ page }) => {
  await login(page);
  const stamp = Date.now();
  const customer = `Example Browser Customer ${stamp}`;
  const ruleName = `Example normal volume ${stamp}`;
  await configure(page, "rule", { name: ruleName, provider: "fixture", query: "Customer ${customer} checkout volume", minimum_samples: "100", comparator: "deviation", threshold: "20", lookback_minutes: "15", fixture_result: JSON.stringify({ value: 96.0, baseline: 100, samples: 1200 }) });
  await page.goto(`${BASE}/ui/automation/?kind=policy`);
  await page.locator("#config-name").fill(`Example policy ${stamp}`);
  await page.locator("#config-customer").fill(customer);
  await page.locator("#config-service").fill("Catalog");
  await page.locator("#config-active").check();
  await page.locator("#config-rules").selectOption({ label: ruleName });
  await page.locator("#config-recipients").fill("operations@example.test");
  await page.locator("#config-auto_close_no_issue").check();
  await page.locator("#config-auto_send").check();
  await page.locator("#config-review_seconds").fill("12");
  await page.getByRole("button", { name: "Save configuration", exact: true }).click();
  const [assessment] = await signal(page, { title: `Example volume concern ${stamp}`, source: "customer", customer, service: "Catalog" });
  const url = `${BASE}/ui/assessments/${assessment.id}/`;
  await page.goto(url);
  await expect(page.getByRole("link", { name: "Review communication", exact: true })).toBeVisible({ timeout: 20000 });
  await page.getByRole("link", { name: "Review communication", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Review communication", exact: true })).toBeVisible();
  await expect(page.getByText("The current revision is authorized", { exact: false })).toBeVisible();
  await expect(page.getByText("Delivered within SLA", { exact: false })).toBeVisible({ timeout: 25000 });
  await page.goto(url);
  await expect(page.getByRole("heading", { name: "Assessment complete · no incident" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Open incident →", exact: true })).toHaveCount(0);
  await expect(page.getByText("within configured bounds", { exact: false }).first()).toBeVisible();
  const history = await page.request.get(`${BASE}/api/assessments/${assessment.id}/history/`);
  const kinds = (await history.json()).map((e: { kind: string }) => e.kind);
  expect(kinds).toEqual(expect.arrayContaining(["query_started", "query_completed", "communication_drafted", "review_window_expired", "communication_auto_sent", "closed_no_issue"]));
  expect(kinds).not.toContain("incident_promoted");
  await checkLayout(page, "healthy-assessment-desktop");
});

test("investigation supports real telemetry, reviewed communications, SME handoff, source-linked RCA and reports", { tag: "@local" }, async ({ page, browser }) => {
  test.setTimeout(180000);
  await login(page);
  const [assessment] = await signal(page, { title: `Example regional recovery ${Date.now()}`, customer: "Example Browser Investigation", service: "Checkout" });
  const url = `${BASE}/ui/assessments/${assessment.id}/`;
  await page.goto(url + "?compose=evidence");
  await page.locator("#evidence-summary").fill("Regional errors confirmed");
  await page.locator("#evidence-result").fill("Synthetic 3.7% failures, 640 requests in 15 minutes; baseline 0.2%.");
  await page.getByRole("button", { name: "Save evidence", exact: true }).click();
  await page.getByText("Record conclusion", { exact: true }).click();
  await page.locator("#conclusion-verdict").selectOption("degraded");
  await page.locator("#conclusion-confidence").selectOption("high");
  await page.locator("#conclusion-reason").fill("Recorded customer-scoped failures establish impact.");
  await page.getByRole("button", { name: "Save conclusion", exact: true }).click();
  await page.getByText("Promote to incident", { exact: true }).click();
  await page.locator("#promote-reason").fill("Repeated checkout errors require incident response.");
  await page.getByRole("button", { name: "Create incident", exact: true }).click();
  await page.waitForURL(u => /^\/ui\/incidents\/[a-f0-9-]+\/$/.test(u.pathname));
  await page.getByRole("button", { name: "Acknowledge incident", exact: true }).click();
  await page.getByText("Escalate tier", { exact: true }).click();
  await page.getByLabel("Why is higher-tier authority needed?").fill("Tier 2 investigation needed.");
  await page.getByRole("button", { name: "Confirm escalation", exact: true }).click();
  await expect(page.getByText("Open · T2", { exact: true })).toBeVisible();

  await page.goto(url + "?compose=query");
  await page.locator("#query-rule").selectOption({ label: "Watchtower · Prometheus scrape availability" });
  await page.getByRole("button", { name: "Run saved check", exact: true }).click();
  await expect(page.getByText("Completed check: Watchtower · Prometheus scrape availability", { exact: true })).toBeVisible({ timeout: 20000 });
  await page.goto(url + "?compose=attachment");
  await page.locator("#attachment-description").fill("Regional observation attached");
  await page.locator("#attachment-file").setInputFiles({ name: "regional-observation.txt", mimeType: "text/plain", buffer: Buffer.from("Synthetic regional evidence; connection pool saturated.") });
  await page.getByRole("button", { name: "Upload evidence", exact: true }).click();
  await expect(page.getByRole("link", { name: "Download regional-observation.txt", exact: true })).toBeVisible();

  await page.goto(url);
  await page.getByLabel("Add to the history").fill("Unsaved operator note survives image paste.");
  await page.getByLabel("Add to the history").evaluate(node => {
    const data = new DataTransfer();
    const bytes = Uint8Array.from(atob("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="), c => c.charCodeAt(0));
    data.items.add(new File([bytes], "pasted-evidence.png", { type: "image/png" }));
    node.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
  });
  await expect(page.locator("[data-photo-status]")).toContainText("1 photo ready");
  await expect(page.getByRole("img", { name: "Pending photo: pasted-evidence.png", exact: true })).toBeVisible();
  await expect(page.getByLabel("Add to the history")).toHaveValue("Unsaved operator note survives image paste.");
  await page.getByRole("button", { name: "Add note", exact: true }).click();
  await expect(page.getByRole("img", { name: "pasted-evidence.png", exact: true })).toBeVisible();

  await page.goto(url + "?compose=email");
  await page.locator("#email-recipients").fill("operations@example.test");
  await page.getByRole("button", { name: "Create communication draft", exact: true }).click();
  await page.getByText("Edit draft", { exact: true }).click();
  await page.locator("#draft-subject").fill("Reviewed synthetic regional update");
  await page.locator("#draft-body").fill("Regional connection churn is under investigation. Customer impact confirmed.");
  await page.getByRole("button", { name: "Save revised draft", exact: true }).click();
  await expect(page.getByText("Revision 2 · Status update", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Send now to local outbox", exact: true }).click();
  await expect(page.getByRole("link", { name: "Download delivered message (.eml)", exact: true })).toBeVisible();
  await checkLayout(page, "communication-desktop");

  await page.goto(url + "?compose=email");
  await page.locator("#email-recipients").fill("operations@example.test");
  await page.getByRole("button", { name: "Create communication draft", exact: true }).click();
  await page.getByText("Suppress communication", { exact: true }).click();
  await page.getByLabel("Suppression reason", { exact: true }).fill("Duplicate update; reviewed message already delivered.");
  await page.getByRole("button", { name: "Confirm suppression", exact: true }).click();
  await expect(page.getByText("Suppressed", { exact: true })).toBeVisible();

  await page.goto(url + "?compose=engage");
  await page.locator("#engage-target").selectOption({ label: "t3a" });
  await page.locator("#engage-reason").fill("Inspect regional database connection saturation.");
  await page.locator("#engage-selected").selectOption({ label: (await page.locator("#engage-selected option").allTextContents()).find(t => t.includes("Regional errors confirmed"))! });
  await page.getByRole("button", { name: "Page specialist", exact: true }).click();
  await expect(page.getByText("Inspect regional database connection saturation.", { exact: true })).toBeVisible();
  const context = await browser.newContext(); const specialist = await context.newPage();
  await login(specialist, "t3a");
  await expect(specialist.getByRole("link", { name: /Inspect regional database connection saturation/ })).toBeVisible();
  await specialist.goto(url + "?tab=sme");
  await specialist.getByRole("button", { name: "Acknowledge handoff", exact: true }).click();
  await specialist.getByLabel("Specialist action and verification").fill("Reduced connection churn; error rate recovered in the example observation window.");
  await specialist.getByRole("button", { name: "Complete handoff", exact: true }).click();
  await expect(specialist.getByText("Completed", { exact: true })).toBeVisible();
  await context.close();

  await page.goto(url + "?compose=operation");
  await page.locator("#operation-kind").selectOption("recovery");
  await page.locator("#operation-body").fill("Order errors returned to baseline; customer journey recovered.");
  await page.getByRole("button", { name: "Record action", exact: true }).click();
  await page.getByText("Resolve incident", { exact: true }).click();
  await page.getByLabel("What restored service?").fill("Connection pool stabilized and journey recovery verified.");
  await page.getByRole("button", { name: "Confirm resolution", exact: true }).click();
  await page.getByRole("link", { name: "RCA", exact: true }).click();
  await page.getByText("Select or annotate RCA evidence", { exact: true }).click();
  const evidenceLabel = (await page.locator("#curate-event option").allTextContents()).find(t => t.includes("Regional errors confirmed"))!;
  await page.locator("#curate-event").selectOption({ label: evidenceLabel });
  await page.locator("#curate-section").selectOption("impact");
  await page.locator("#curate-note").fill("Customer-scoped evidence established impact before promotion.");
  await page.getByRole("button", { name: "Save selection", exact: true }).click();
  await page.locator("#rca-summary").fill("Example regional checkout degradation resolved by stabilizing database connections.");
  await page.locator("#rca-root_cause").fill("Regional connection churn exhausted the connection pool.");
  await page.locator("#rca-impact").fill("Example region; order failures rose to 3.7%.");
  await page.locator("#rca-state").selectOption("final");
  await page.getByRole("button", { name: "Save RCA", exact: true }).click();
  await page.getByText("Add corrective action", { exact: true }).click();
  await page.locator("#remediation-title").fill("Add regional pool saturation alert");
  await page.locator("#remediation-owner").selectOption({ label: "t2a" });
  await page.locator("#remediation-due_date").fill("2026-12-01");
  await page.getByRole("button", { name: "Assign action", exact: true }).click();
  await page.getByLabel("Action status").selectOption("done");
  await page.getByLabel("Completion verification").fill("Synthetic replay triggered the new alert.");
  await page.getByRole("button", { name: "Update action", exact: true }).click();
  const exported = await page.request.get(url + "rca.md");
  expect(await exported.text()).toContain("?event=");
  expect(await exported.text()).toContain("Synthetic replay triggered");
  await checkLayout(page, "rca-desktop");
  for (const viewport of [{ width: 390, height: 844 }, { width: 1440, height: 1000 }]) {
    await page.setViewportSize(viewport);
    for (const [path, name] of [[url, "workspace"], [url + "?tab=sme", "specialists"], [url + "?tab=rca", "rca"], [`${BASE}/ui/reports/?examples=1&customer=Example%20Browser%20Investigation`, "reports"], [`${BASE}/ui/outbox/`, "outbox"], [`${BASE}/ui/automation/`, "configuration"]]) {
      await page.goto(path);
      await checkLayout(page, `${name}-${viewport.width}`);
    }
  }
});


test("top composer posts dashboard notes and chosen/pasted photos together", { tag: "@local" }, async ({ page }) => {
  await login(page);
  const [assessment] = await signal(page, { title: `Example photo note ${Date.now()}`, customer: "Example Photo Review", service: "Catalog" });
  const url = `${BASE}/ui/assessments/${assessment.id}/`;
  await page.goto(url);
  const composer = page.locator("#composer");
  expect(await composer.evaluate(node => Boolean(node.compareDocumentPosition(document.querySelector('.case-tabs')!) & Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true);
  const input = page.getByLabel("Add to the history", { exact: true });
  for (const viewport of [{width:1280,height:800}, {width:390,height:844}]) {
    await page.setViewportSize(viewport);
    await page.evaluate(() => window.scrollTo(0, 0));
    const bounds = await input.boundingBox();
    expect(bounds!.y + bounds!.height).toBeLessThan(viewport.height);
    await checkLayout(page, `composer-${viewport.width}`);
  }
  await page.setViewportSize({width:1280,height:800});
  const note = "Orders dashboard https://dashboards.example.test/d/air?region=example — errors recovered.";
  await input.fill(note);
  const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=", "base64");
  await page.locator('#comment-photos').setInputFiles([{name:"dashboard.png",mimeType:"image/png",buffer:png},{name:"remove-me.png",mimeType:"image/png",buffer:png}]);
  await expect(page.locator('[data-photo-status]')).toContainText('2 photos ready');
  await page.getByRole('button', {name:'Remove remove-me.png',exact:true}).click();
  await expect(page.locator('[data-photo-status]')).toContainText('1 photo ready');
  await input.evaluate(node => {
    const clipboard = new DataTransfer();
    clipboard.items.add(new File([Uint8Array.from(atob("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="), c => c.charCodeAt(0))], 'pasted.png', {type:'image/png'}));
    node.dispatchEvent(new ClipboardEvent('paste', {clipboardData:clipboard,bubbles:true,cancelable:true}));
  });
  await expect(page.locator('[data-photo-status]')).toContainText('2 photos ready');
  await page.locator('[data-note-form]').evaluate(node => {
    const dropped = new DataTransfer();
    dropped.items.add(new File([Uint8Array.from(atob("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="), c => c.charCodeAt(0))], 'dropped.png', {type:'image/png'}));
    node.dispatchEvent(new DragEvent('drop', {dataTransfer:dropped,bubbles:true,cancelable:true}));
  });
  await expect(page.locator('[data-photo-status]')).toContainText('3 photos ready');
  await page.getByRole('button',{name:'Remove dropped.png',exact:true}).click();
  await expect(page.locator('[data-photo-status]')).toContainText('2 photos ready');
  await expect(input).toHaveValue(note);
  let history = await (await page.request.get(`${BASE}/api/assessments/${assessment.id}/history/`)).json();
  expect(history.filter((e: {kind:string}) => e.kind === 'comment')).toHaveLength(0);
  await page.getByRole('button', {name:'Add note',exact:true}).click();
  const entry = page.locator('.event-card').filter({hasText:note});
  await expect(entry.getByRole('link',{name:'https://dashboards.example.test/d/air?region=example',exact:true})).toHaveAttribute('href','https://dashboards.example.test/d/air?region=example');
  await expect(entry.getByRole('img')).toHaveCount(2);
  for (const image of await entry.getByRole('img').all()) await expect.poll(() => image.evaluate((node: HTMLImageElement) => node.naturalWidth)).toBeGreaterThan(0);
  await expect(entry.getByRole('link',{name:'Download dashboard.png',exact:true})).toBeVisible();
  history = await (await page.request.get(`${BASE}/api/assessments/${assessment.id}/history/`)).json();
  const notes = history.filter((e: {kind:string}) => e.kind === 'comment');
  expect(notes).toHaveLength(1); expect(notes[0].data.attachments).toHaveLength(2);
  await page.getByRole('link',{name:'Evidence',exact:true}).click();
  await expect(page.locator('.event-card').filter({hasText:note}).getByRole('img')).toHaveCount(2);
  await page.evaluate(() => window.scrollTo(0,document.body.scrollHeight));
  await page.getByRole('link',{name:'Add to history',exact:true}).click();
  await expect(input).toBeFocused();
  await checkLayout(page,'photo-note-evidence-desktop');
});
