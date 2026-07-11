import { test, expect, Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

// Accessibility gate (ADR-039): axe-core (the engine behind Lighthouse's accessibility
// category) scans every /ui/ page + the landing page against WCAG 2.1 A/AA, at BOTH a
// mobile viewport (mobile-first is the design doctrine) and desktop. Zero violations
// allowed. Untagged → runs everywhere (local `make e2e` / pre-commit AND the pipeline's
// staging Smoke stage) per the #30 suite tiering; `make a11y` greps the @a11y tag to run
// just this file. DRF's stock login template is not ours and is excluded.
const BASE = process.env.BASE_URL || "http://localhost:8010";
const USER = process.env.SMOKE_USER || "t1a";
const PASS = process.env.SMOKE_PASSWORD || "watch";

const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const VIEWPORTS = {
  mobile: { width: 390, height: 844 }, // iPhone-class — the primary experience
  desktop: { width: 1280, height: 800 },
};

// Fixed list (not a crawl) so a broken page can't silently drop out of the scan.
const PAGES = [
  "/",
  "/ui/incidents/",
  "/ui/schedule/",
  "/ui/checks/",
  "/ui/problems/",
  "/ui/rcas/",
  "/ui/webhooks/",
  "/ui/environments/",
  "/ui/settings/",
];

async function login(page: Page) {
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
}

async function scan(page: Page, path: string) {
  await page.goto(`${BASE}${path}`);
  await page.waitForLoadState("networkidle"); // let the Tailwind Play CDN apply styles
  // Mobile-first floor (ADR-039): no page may scroll horizontally at any supported viewport.
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow, `${path} must not scroll horizontally`).toBeLessThanOrEqual(0);
  const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze();
  const summary = results.violations.map((v) => ({
    id: v.id,
    impact: v.impact,
    help: v.help,
    nodes: v.nodes.slice(0, 5).map((n) => n.html.slice(0, 120)),
  }));
  expect(summary, `${path} must have no WCAG A/AA violations`).toEqual([]);
}

for (const [name, viewport] of Object.entries(VIEWPORTS)) {
  test(`a11y: all /ui/ pages pass WCAG 2.1 AA (${name})`, { tag: ["@local", "@a11y"] }, async ({ page }) => {
    await page.setViewportSize(viewport);
    await login(page);
    for (const path of PAGES) {
      await scan(page, path);
    }
  });

  test(`a11y: first incident detail passes WCAG 2.1 AA (${name})`, { tag: ["@local", "@a11y"] }, async ({ page }) => {
    // Detail pages carry the densest markup (timeline, actions, links) — scan one of each
    // detail type that exists in the environment; skip cleanly when the list is empty.
    await page.setViewportSize(viewport);
    await login(page);
    for (const list of ["/ui/incidents/", "/ui/checks/", "/ui/problems/", "/ui/rcas/"]) {
      await page.goto(`${BASE}${list}`);
      const first = page.locator(`a[href^="${list}"][href$="/"]:not([href="${list}"])`).first();
      if ((await first.count()) === 0) continue;
      const href = await first.getAttribute("href");
      await scan(page, href!);
    }
  });
}
