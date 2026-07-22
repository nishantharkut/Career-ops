import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";

import { expect, test } from "@playwright/test";

import { sampleJdPath } from "./fixtures";

const API_ORIGIN = "http://127.0.0.1:8765";
const texPath = process.env.CAREEROPS_REAL_RESUME_TEX ?? "";
const pdfPath = process.env.CAREEROPS_REAL_RESUME_PDF ?? "";
const hasRealResume = Boolean(
  texPath && pdfPath && existsSync(texPath) && existsSync(pdfPath)
);

function sha256(value: Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

test("uploads, renders, and reviews the real general SWE resume", async ({ page }) => {
  test.skip(!hasRealResume, "Set CAREEROPS_REAL_RESUME_TEX and CAREEROPS_REAL_RESUME_PDF");
  test.setTimeout(180_000);
  const texBytes = readFileSync(texPath);
  const referencePdf = readFileSync(pdfPath);
  expect(texBytes.byteLength).toBeGreaterThan(1_000);
  expect(referencePdf.subarray(0, 4).toString("ascii")).toBe("%PDF");

  await page.goto("/");
  await page.getByRole("button", { name: "Add application" }).click();
  await page.getByLabel("Company").fill("CareerOps real resume verification");
  await page.getByLabel("Role title").fill("Software Engineer");
  await page.getByLabel("Location").fill("India");
  await page.getByLabel("Source URL").fill("https://example.com/careerops-real-resume");
  await page.getByLabel("JD text").fill(readFileSync(sampleJdPath, "utf-8"));
  await page.getByRole("button", { name: "Save application" }).click();
  await expect(page.getByRole("heading", { name: "CareerOps real resume verification" })).toBeVisible();

  await page.getByRole("tab", { name: "Resume" }).click();
  await page.getByLabel("Resume name").fill("General SWE backend real resume");
  await page.getByLabel("LaTeX file").setInputFiles(texPath);
  await page.getByRole("button", { name: "Upload resume" }).click();
  await expect(page.getByText("General SWE backend real resume", { exact: true })).toBeVisible();

  const applications = await page.request.get(`${API_ORIGIN}/applications`);
  expect(applications.ok()).toBe(true);
  const application = (await applications.json() as Array<{ id: string; company: string }>).find(
    (item) => item.company === "CareerOps real resume verification"
  );
  expect(application).toBeDefined();
  const uploadedPacket = await page.request.get(
    `${API_ORIGIN}/applications/${application!.id}/packet`
  );
  const uploaded = await uploadedPacket.json() as {
    selected_resume: { tex_sha256: string; pdf_sha256: string | null };
  };
  expect(uploaded.selected_resume.tex_sha256).toBe(sha256(texBytes));
  expect(uploaded.selected_resume.pdf_sha256).toBeNull();

  await page.getByRole("button", { name: "Render PDF" }).click();
  await expect(page.locator(".meta").getByText("rendered", { exact: true }).first()).toBeVisible({
    timeout: 60_000
  });

  const renderedPacketResponse = await page.request.get(
    `${API_ORIGIN}/applications/${application!.id}/packet`
  );
  const renderedPacket = await renderedPacketResponse.json() as {
    selected_resume: { pdf_sha256: string; page_count: number };
  };
  expect(renderedPacket.selected_resume.page_count).toBe(1);
  expect(renderedPacket.selected_resume.pdf_sha256).toMatch(/^[0-9a-f]{64}$/);

  const pdfResponse = await page.request.get(
    `${API_ORIGIN}/applications/${application!.id}/artifacts/resume-pdf`
  );
  expect(pdfResponse.ok()).toBe(true);
  const renderedPdf = Buffer.from(await pdfResponse.body());
  expect(renderedPdf.subarray(0, 4).toString("ascii")).toBe("%PDF");
  expect(sha256(renderedPdf)).toBe(renderedPacket.selected_resume.pdf_sha256);
  expect(renderedPdf.byteLength).toBeGreaterThan(referencePdf.byteLength / 2);

  await page.getByLabel("Provider").selectOption({ label: "E2E mock provider" });
  await page.getByRole("button", { name: "Run AI review" }).click();
  const taskStatus = page.locator("section[aria-label='Task status']");
  await expect(taskStatus.getByRole("status")).toHaveText("Completed", { timeout: 45_000 });
  await expect(page.getByText("90", { exact: true })).toBeVisible();
  await expect(page.getByText("0 blockers", { exact: true })).toBeVisible();
});
