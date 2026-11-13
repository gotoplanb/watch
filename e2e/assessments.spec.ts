import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const BASE = process.env.BASE_URL || "http://localhost:8010";
const PASS = process.env.SMOKE_PASSWORD || "watch";

test("assessment journey: no issue stays separate; promotion preserves evidence", { tag: "@local" }, async ({ page }) => {
  await page.goto(`${BASE}/api-auth/login/?next=/ui/assessments/`);
  await page.fill("#id_username", process.env.SMOKE_USER || "t1a");
  await page.fill("#id_password", PASS);
  await page.locator("form").filter({ has: page.locator("#id_username") }).evaluate((f: HTMLFormElement) => f.requestSubmit());
  await page.waitForURL((u) => u.pathname === "/ui/assessments/");

  async function create(title: string) {
    await page.goto(`${BASE}/ui/assessments/new/`);
    await page.getByLabel("What needs assessment?").fill(title);
    await page.getByLabel("Customer / partner").fill("Example Customer");
    await page.getByLabel("Service / line of business").fill("Checkout");
    await page.getByLabel("Original signal").fill("Synthetic browser-test concern; establish journey impact.");
    await page.getByRole("button", { name: "Create assessment", exact: true }).click();
    await page.waitForURL(/\/ui\/assessments\/[a-f0-9-]+\/$/);
  }
  async function recordEvidence() {
    await page.getByRole("link", { name: "Record evidence", exact: true }).click();
    await page.getByLabel("Evidence summary").fill("Representative journey checked");
    await page.getByLabel("Evidence source").fill("browser test");
    await page.getByLabel("Query or check performed").fill("Compare successful journeys in matching windows.");
    await page.getByLabel("Recorded result and observation window").fill("Synthetic 15-minute observation; source evidence retained.");
    await page.getByRole("button", { name: "Save evidence" }).click();
  }
  async function conclude(verdict: string) {
    await page.getByText("Record conclusion", { exact: true }).click();
    await page.getByLabel("Verdict").selectOption(verdict);
    await page.getByLabel("Confidence", { exact: false }).selectOption("high");
    await page.getByLabel("Reasoning and evidence limits").fill("Evidence supports the conclusion; this is a synthetic example.");
    await page.getByRole("button", { name: "Save conclusion" }).click();
  }

  await create(`Assessment no issue ${Date.now()}`);
  await recordEvidence();
  await conclude("no_issue");
  await page.getByText("Close · no issue", { exact: true }).click();
  await page.locator("#no_issue-reason").fill("Journey healthy in the recorded observation window.");
  await page.getByRole("button", { name: "Close assessment", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Assessment complete · no incident" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Open incident" })).toHaveCount(0);

  const title = `Assessment promotion ${Date.now()}`;
  await create(title);
  const assessmentUrl = page.url();
  await recordEvidence();
  await conclude("degraded");
  await page.getByText("Promote to incident", { exact: true }).click();
  await page.getByLabel("Why does this warrant an incident?").fill("Repeated failed journeys establish customer impact.");
  await page.getByRole("button", { name: "Create incident", exact: true }).click();
  await page.waitForURL(u => /^\/ui\/incidents\/[a-f0-9-]+\/$/.test(u.pathname));
  await expect(page.getByRole("heading", { name: title, exact: true })).toBeVisible();
  await expect(page.getByText("Representative journey checked", { exact: true })).toBeVisible();
  const ancestor = page.getByRole("link", { name: /^ASM-/ });
  expect(await ancestor.getAttribute("href")).toBe(new URL(assessmentUrl).pathname);
  await page.getByLabel("Add to the history").fill("Post-promotion investigation continues in the same history.");
  await page.getByRole("button", { name: "Add note", exact: true }).click();
  await expect(page.getByText("Post-promotion investigation continues in the same history.", { exact: true })).toBeVisible();

  for (const viewport of [{ width: 390, height: 844 }, { width: 1440, height: 1000 }]) {
    await page.setViewportSize(viewport);
    for (const url of [assessmentUrl, `${BASE}/ui/assessments/`, `${BASE}/ui/assessments/new/`]) {
      await page.goto(url);
      await page.waitForLoadState("networkidle");
      expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
      expect(results.violations.map(v => ({ id: v.id, help: v.help, nodes: v.nodes.map(n => n.html) }))).toEqual([]);
    }
  }
  if (process.env.ASSESSMENT_SCREENSHOT_DIR) {
    await page.goto(assessmentUrl);
    await page.waitForLoadState("networkidle");
    await page.screenshot({ path: `${process.env.ASSESSMENT_SCREENSHOT_DIR}/assessment-desktop.png`, fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: `${process.env.ASSESSMENT_SCREENSHOT_DIR}/assessment-mobile.png`, fullPage: true });
  }
});
