import { expect, test } from "@playwright/test";

test("home loads and counts (6)", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("h1")).toBeVisible();
  const button = page.getByRole("button").first();
  for (let k = 0; k < 6; k++) await button.click();
  await expect(button).toContainText("6");
});
