import { readFileSync } from "node:fs";

import { expect, test } from "@playwright/test";

import { cleanResumePath, makeRiskyResume, sampleJdPath } from "./fixtures";

test("day-one application desk blocks risky resume and submits clean packet", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByText("No applications yet. Add a JD to start a packet.")).toBeVisible();

  await page.getByRole("button", { name: "Add application" }).click();
  await page.getByLabel("Company").fill("Swiggy");
  await page.getByLabel("Role title").fill("Data Science Intern");
  await page.getByLabel("Location").fill("Bangalore");
  await page.getByLabel("Source URL").fill("https://example.com/swiggy");
  await page.getByLabel("JD text").fill(readFileSync(sampleJdPath, "utf-8"));
  await page.getByRole("button", { name: "Save application" }).click();

  await expect(page.getByRole("heading", { name: "Swiggy" })).toBeVisible();

  await page.getByLabel("Resume name").fill("Risky resume");
  await page.getByLabel("LaTeX file").setInputFiles(makeRiskyResume());
  await page.getByRole("button", { name: "Upload resume" }).click();
  await expect(page.getByText("Risky resume")).toBeVisible();
  await page.getByRole("button", { name: "Render PDF" }).click();
  await expect(page.getByText("rendered")).toBeVisible({ timeout: 60000 });
  await page.getByRole("button", { name: "Analyze" }).click();
  await expect(page.getByText("NDA-sensitive wording")).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve packet" })).toBeDisabled();

  await page.getByLabel("Resume name").fill("Application resume");
  await page.getByLabel("LaTeX file").setInputFiles(cleanResumePath);
  await page.getByRole("button", { name: "Upload resume" }).click();
  await expect(page.getByText("Application resume")).toBeVisible();
  await page.getByRole("button", { name: "Render PDF" }).click();
  await expect(page.getByText("rendered")).toBeVisible({ timeout: 60000 });
  await page.getByRole("button", { name: "Analyze" }).click();
  await expect(page.getByText("0 blockers")).toBeVisible();
  await page.getByRole("button", { name: "Approve packet" }).click();
  await page.getByRole("button", { name: "Mark submitted manually" }).click();

  await expect(page.getByText("Submitted manually")).toBeVisible();
});

for (const width of [320, 375, 414, 768, 1280]) {
  test(`responsive shell has no horizontal overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await page.goto("/");

    await expect(page.getByRole("button", { name: "Add application" })).toBeVisible();
    const hasHorizontalOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth
    );
    expect(hasHorizontalOverflow).toBe(false);
  });
}
