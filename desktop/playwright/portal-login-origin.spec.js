// Check the delivered portal before sign-in. The default Playwright page has a fresh context for each host.
const { test, expect } = require("@playwright/test");

const base = process.env.AGENT_SERVICES_WEB_URL;
const target = base && new URL("/", base);
test.skip(!target || !["127.0.0.1", "localhost", "[::1]"].includes(target.hostname),
  "Set AGENT_SERVICES_WEB_URL to a running loopback portal with an owner passphrase.");
for (const host of ["127.0.0.1", "localhost"]) {
  test(`the login page on ${host} checks authentication on its own origin`, async ({ page }) => {
    const root = new URL(target);
    root.hostname = host;
    const statusResponse = page.waitForResponse(
      response => new URL(response.url()).pathname === "/api/v1/auth/status",
      { timeout: 10_000 },
    );
    const [documentResponse, status] = await Promise.all([page.goto(root.href), statusResponse]);
    expect(documentResponse.status()).toBe(200);
    expect(status.url()).toBe(new URL("/api/v1/auth/status", root).href);
    expect(status.status()).toBe(200);
    await expect(page.locator('input[autocomplete="current-password"]')).toBeVisible();
  });
}
