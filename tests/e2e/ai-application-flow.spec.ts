import { expect, Page, test } from "@playwright/test";
import { readFileSync } from "node:fs";

import {
  safeResumeReplacement,
  sampleJdPath,
  unsafeMetricClaim,
  writeCleanResume,
  writeRiskyResume
} from "./fixtures";

const API_ORIGIN = "http://127.0.0.1:8765";
const MOCK_PROVIDER_LABEL = "E2E mock provider";
const E2E_CONTROL_HEADERS = { "X-CareerOps-E2E-Control": "careerops-e2e-control-v1" };

type JsonObject = Record<string, unknown>;

type ResumeRecord = {
  id: string;
  name: string;
  tex_sha256: string;
  pdf_sha256: string | null;
  parent_resume_version_id: string | null;
};

type AiTaskRecord = {
  id: string;
  task_type: string;
  provider_config_id: string;
  prompt_template_id: string;
  prompt_sha256: string;
  status: string;
};

type ApplicationVersionRecord = {
  id: string;
  version_number: number;
  event_type: string;
  snapshot: JsonObject;
};

type ApplicationPacket = {
  application: {
    id: string;
    status: string;
    submission_state: string;
    selected_resume_version_id: string | null;
  };
  selected_resume: ResumeRecord | null;
  latest_report: {
    id: string;
    application_id: string;
    resume_version_id: string;
    blocking_issue_count: number;
  } | null;
  approval: {
    id: string;
    resume_version_id: string;
    report_id: string;
    pdf_sha256: string;
  } | null;
  latest_ai_task: AiTaskRecord | null;
  evidence: Array<{
    id: string;
    claim_text: string;
    verification_state: string;
    privacy_level: string;
  }>;
  versions: ApplicationVersionRecord[];
};

type AiArtifactRecord = {
  id: string;
  task_id: string;
  artifact_type: string;
  sha256: string;
  schema_version: string;
  created_at: string;
  path?: unknown;
};

type AiFindingRecord = {
  id: string;
  task_id: string;
  finding_type: string;
  verifier_state: string;
  decision: string;
  payload: JsonObject;
};

function trackBrowserErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  return errors;
}

async function apiJson<T>(page: Page, path: string): Promise<T> {
  const response = await page.request.get(`${API_ORIGIN}${path}`);
  expect(response.ok(), `${path} returned ${response.status()}: ${await response.text()}`).toBe(true);
  return response.json() as Promise<T>;
}

async function packet(page: Page, applicationId: string): Promise<ApplicationPacket> {
  return apiJson<ApplicationPacket>(page, `/applications/${applicationId}/packet`);
}

async function actualResumeSourceSha256(page: Page, resumeId: string): Promise<string> {
  const response = await page.request.get(
    `${API_ORIGIN}/__e2e__/resumes/${resumeId}/source-sha256`,
    { headers: E2E_CONTROL_HEADERS }
  );
  expect(response.status(), await response.text()).toBe(200);
  const payload = await response.json() as { sha256: string };
  expect(payload.sha256).toMatch(/^[0-9a-f]{64}$/);
  return payload.sha256;
}

async function selectApplication(page: Page, applicationId: string): Promise<void> {
  const applications = await apiJson<Array<{ id: string }>>(page, "/applications");
  const applicationIndex = applications.findIndex((application) => application.id === applicationId);
  expect(applicationIndex).toBeGreaterThanOrEqual(0);
  const applicationButtons = page.locator("aside[aria-label='Applications'] ul li button");
  await expect(applicationButtons).toHaveCount(applications.length);
  await applicationButtons.nth(applicationIndex).click();
}

async function chooseMockProvider(page: Page): Promise<void> {
  const provider = page.getByLabel("Provider");
  await expect(provider.locator("option", { hasText: MOCK_PROVIDER_LABEL })).toHaveCount(1);
  await provider.selectOption({ label: MOCK_PROVIDER_LABEL });
}

async function runAiTask(
  page: Page,
  taskType: string,
  commandLabel: string,
  terminalStatus: "Blocked" | "Completed"
): Promise<void> {
  await chooseMockProvider(page);
  await page.getByRole("combobox", { name: "Task", exact: true }).selectOption(taskType);
  const command = page.getByRole("button", { name: commandLabel, exact: true });
  const taskStatus = page.locator("section[aria-label='Task status']");
  const taskIdentity = taskStatus.locator(".meta > div").filter({ hasText: /^Task/ }).locator("dd");
  const previousIdentity = (await taskIdentity.count()) > 0 ? await taskIdentity.textContent() : "";
  await expect(command).toBeEnabled();
  await command.click();
  await expect.poll(async () => (await taskIdentity.textContent()) ?? "", { timeout: 45_000 })
    .not.toBe(previousIdentity);
  await expect(taskIdentity).toContainText(taskType);
  await expect(taskStatus.getByRole("status")).toHaveText(terminalStatus, { timeout: 45_000 });
}

async function uploadResume(page: Page, name: string, filePath: string): Promise<void> {
  await page.getByRole("tab", { name: "Resume" }).click();
  await page.getByLabel("Resume name").fill(name);
  await page.getByLabel("LaTeX file").setInputFiles(filePath);
  await page.getByRole("button", { name: "Upload resume" }).click();
  await expect(page.getByText(name, { exact: true })).toBeVisible();
}

async function renderSelectedResume(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Render PDF" }).click();
  await expect(page.locator(".meta").getByText("rendered", { exact: true }).first()).toBeVisible({
    timeout: 60_000
  });
}

async function downloadArtifact(
  page: Page,
  buttonLabel: string,
  artifactPath: string,
  expectedContentType: string
): Promise<Buffer> {
  const responsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "GET" && url.pathname === artifactPath;
  });
  const downloadPromise = page.waitForEvent("download");

  await page.getByRole("button", { name: buttonLabel, exact: true }).click();
  const [response, download] = await Promise.all([responsePromise, downloadPromise]);
  expect(response.ok()).toBe(true);
  expect((await response.headerValue("content-type"))?.split(";", 1)[0]).toBe(expectedContentType);
  const path = await download.path();
  expect(path).not.toBeNull();
  const content = readFileSync(path!);
  expect(content.byteLength).toBeGreaterThan(0);
  return content;
}

test("real mock provider completes the day-one application workflow with durable provenance", async ({ page }, testInfo) => {
  test.setTimeout(240_000);
  const browserErrors = trackBrowserErrors(page);
  let applicationId: string;
  let profileFactId: string;
  const profileFactKey = `production_software_delivery_e2e_${testInfo.repeatEachIndex}`;
  const riskyResumePath = writeRiskyResume(testInfo.outputPath("risky_resume.tex"));
  const cleanResumePath = writeCleanResume(testInfo.outputPath("clean_resume.tex"));

  await test.step("create an application and analyze the JD through the persisted mock task runner", async () => {
    await page.goto("/");
    await page.getByRole("button", { name: "Add application" }).click();
    await page.getByLabel("Company").fill("Swiggy");
    await page.getByLabel("Role title").fill("Data Science Intern");
    await page.getByLabel("Location").fill("Bangalore");
    await page.getByLabel("Source URL").fill("https://example.com/swiggy-data-science-intern");
    await page.getByLabel("JD text").fill(readFileSync(sampleJdPath, "utf-8"));
    const createdResponse = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return response.request().method() === "POST" && url.pathname === "/applications";
    });
    await page.getByRole("button", { name: "Save application" }).click();
    const created = await createdResponse;
    expect(created.status()).toBe(201);
    applicationId = (await created.json() as { id: string }).id;
    await expect(page.getByRole("heading", { name: "Swiggy" })).toBeVisible();

    await runAiTask(page, "analyze_jd", "Analyze JD", "Completed");
    await page.getByRole("tab", { name: "Requirements" }).click();
    const rawAi = page.locator("section[aria-labelledby='requirements-raw_ai']");
    await expect(rawAi.getByRole("heading", { name: "Raw AI" })).toBeVisible();
    await expect(rawAi.getByText("normalized_location", { exact: true })).toBeVisible();
    await expect(rawAi.getByText("Bangalore", { exact: true })).toBeVisible();
  });

  await test.step("correct one extracted requirement and persist verified public evidence", async () => {
    await page.getByRole("button", { name: "Add correction" }).click();
    await page.getByLabel("Correction key 1").fill("normalized_location");
    await page.getByLabel("Correction value for normalized_location").fill("Bengaluru, India");
    await page.getByRole("button", { name: "Save corrections" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Requirements saved." })).toBeVisible();
    const corrections = page.locator("section[aria-labelledby='requirements-user_corrected']");
    await expect(corrections.getByText("Bengaluru, India", { exact: true })).toBeVisible();

    await page.getByRole("tab", { name: "Evidence" }).click();
    await page.getByLabel("Evidence claim").fill(safeResumeReplacement);
    await page.getByLabel("Evidence type").fill("Repository inspection");
    await page.getByLabel("Source URL").fill("https://example.com/evidence/repository-inspection");
    await page.getByLabel("Evidence verification").selectOption("verified");
    await page.getByLabel("Evidence privacy").selectOption("public");
    await page.getByRole("button", { name: "Add evidence" }).click();
    await expect(page.getByText("Verified public evidence", { exact: true })).toBeVisible();

    await page.getByLabel("Fact key").fill(profileFactKey);
    await page.getByLabel("Fact value").fill(safeResumeReplacement);
    await page.getByLabel("Fact verification").selectOption("verified");
    await page.getByLabel("Fact privacy").selectOption("public");
    const factResponse = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return response.request().method() === "POST" && url.pathname === "/profile-facts";
    });
    await page.getByRole("button", { name: "Add profile fact" }).click();
    const createdFact = await factResponse;
    expect(createdFact.status()).toBe(201);
    profileFactId = (await createdFact.json() as { id: string }).id;
    await expect(page.getByText(profileFactKey, { exact: true })).toBeVisible();
  });

  await test.step("block the unsafe 70+ output and reject the risky resume", async () => {
    await uploadResume(page, "Risky 70+ resume", riskyResumePath);
    await renderSelectedResume(page);
    await runAiTask(page, "analyze_fit", "Run AI review", "Blocked");
    let unsupportedMetric = page
      .locator("section[aria-label='AI findings'] li")
      .filter({ hasText: "unsupported_metric in provider output" });
    await expect(unsupportedMetric.getByText("Blocked by verifier", { exact: true })).toBeVisible();
    await expect(page.getByText(unsafeMetricClaim, { exact: true })).toHaveCount(0);
    await expect(unsupportedMetric.getByRole("button", { name: /Accept/ })).toHaveCount(0);
    await unsupportedMetric.getByRole("button", { name: "Reject suggestion" }).click();
    await expect(unsupportedMetric.getByText("Decision: rejected", { exact: true })).toBeVisible();

    const blockedTask = (await packet(page, applicationId)).latest_ai_task!;
    const blockedFindings = await apiJson<AiFindingRecord[]>(page, `/ai-tasks/${blockedTask.id}/findings`);
    expect(blockedFindings).toContainEqual(expect.objectContaining({
      claim_text: "unsupported_metric in provider output",
      verifier_state: "blocked",
      decision: "rejected"
    }));

    await page.reload();
    await selectApplication(page, applicationId);
    await expect(page.getByRole("heading", { name: "Swiggy" })).toBeVisible();
    unsupportedMetric = page
      .locator("section[aria-label='AI findings'] li")
      .filter({ hasText: "unsupported_metric in provider output" });
    await expect(unsupportedMetric.getByText("Blocked by verifier", { exact: true })).toBeVisible();
    await expect(unsupportedMetric.getByText("Decision: rejected", { exact: true })).toBeVisible();
    await expect(unsupportedMetric.getByRole("button", { name: /Accept|Reject/ })).toHaveCount(0);
  });

  let parentResume: ResumeRecord;
  let parentSourceSha256: string;
  await test.step("replace the risky resume with a clean rendered parent and complete fit review", async () => {
    await uploadResume(page, "Clean application resume", cleanResumePath);
    parentResume = (await packet(page, applicationId)).selected_resume!;
    expect(parentResume.parent_resume_version_id).toBeNull();
    parentSourceSha256 = await actualResumeSourceSha256(page, parentResume.id);
    const parentById = await apiJson<ResumeRecord>(page, `/resumes/${parentResume.id}`);
    expect(parentById.tex_sha256).toBe(parentResume.tex_sha256);
    expect(parentSourceSha256).toBe(parentResume.tex_sha256);

    await renderSelectedResume(page);
    await runAiTask(page, "analyze_fit", "Run AI review", "Completed");
    await expect(page.getByText("0 blockers", { exact: true })).toBeVisible();
  });

  let childResume: ResumeRecord;
  await test.step("accept one exact verified edit and create an immutable child resume", async () => {
    await runAiTask(page, "suggest_resume_edits", "Suggest resume edits", "Completed");
    const verifiedSuggestion = page
      .locator("section[aria-label='AI findings'] li")
      .filter({ hasText: "Verified suggestion" });
    await expect(verifiedSuggestion.getByText(safeResumeReplacement, { exact: true }).first()).toBeVisible();
    await expect(verifiedSuggestion.getByText("Verified suggestion", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Accept suggestion" }).click();
    await expect(page.getByText("Decision: accepted", { exact: true })).toBeVisible();
    await page.getByLabel("Draft name").fill("Swiggy verified draft");
    await page.getByRole("button", { name: "Create resume draft" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Draft created. Render it before review." })).toBeVisible();
    await expect(page.getByText("Swiggy verified draft", { exact: true })).toBeVisible();

    const draftPacket = await packet(page, applicationId);
    childResume = draftPacket.selected_resume!;
    expect(childResume.id).not.toBe(parentResume.id);
    expect(childResume.parent_resume_version_id).toBe(parentResume.id);
    expect(childResume.tex_sha256).not.toBe(parentResume.tex_sha256);
    const parentAfterDraft = await apiJson<ResumeRecord>(page, `/resumes/${parentResume.id}`);
    expect(parentAfterDraft).toMatchObject({ id: parentResume.id, tex_sha256: parentResume.tex_sha256 });
    expect(await actualResumeSourceSha256(page, parentResume.id)).toBe(parentSourceSha256);
    expect(draftPacket.versions).toEqual(expect.arrayContaining([
      expect.objectContaining({
        event_type: "resume_draft.created",
        snapshot: expect.objectContaining({
          parent_resume_version_id: parentResume.id,
          resume_version_id: childResume.id,
          tex_sha256: childResume.tex_sha256
        })
      })
    ]));
  });

  let reviewedPacket: ApplicationPacket;
  await test.step("render and review the child, then download the real JD, PDF, and report", async () => {
    await renderSelectedResume(page);
    await runAiTask(page, "analyze_fit", "Run AI review", "Completed");
    await expect(page.getByText("0 blockers", { exact: true })).toBeVisible();
    reviewedPacket = await packet(page, applicationId);
    expect(reviewedPacket.selected_resume?.id).toBe(childResume.id);
    expect(reviewedPacket.latest_report).toMatchObject({
      application_id: applicationId,
      resume_version_id: childResume.id,
      blocking_issue_count: 0
    });

    const jd = await downloadArtifact(
      page,
      "Export JD",
      `/applications/${applicationId}/artifacts/jd`,
      "text/plain"
    );
    expect(jd.toString("utf-8")).toContain("Company: Swiggy");

    const pdf = await downloadArtifact(
      page,
      "Export resume PDF",
      `/applications/${applicationId}/artifacts/resume-pdf`,
      "application/pdf"
    );
    expect(pdf.subarray(0, 5).toString("ascii")).toBe("%PDF-");

    const report = await downloadArtifact(
      page,
      "Export report",
      `/applications/${applicationId}/artifacts/report`,
      "application/json"
    );
    expect(JSON.parse(report.toString("utf-8"))).toMatchObject({
      id: reviewedPacket.latest_report!.id,
      application_id: applicationId,
      resume_version_id: childResume.id,
      blocking_issue_count: 0
    });
  });

  await test.step("persist a cover-letter provenance artifact", async () => {
    await runAiTask(page, "draft_cover_letter", "Draft cover letter", "Completed");
    const coverTask = (await packet(page, applicationId)).latest_ai_task!;
    const artifacts = await apiJson<AiArtifactRecord[]>(page, `/ai-tasks/${coverTask.id}/artifacts`);
    const draft = artifacts.find((artifact) => artifact.artifact_type === "draft_text");
    expect(draft).toMatchObject({ task_id: coverTask.id, artifact_type: "draft_text", schema_version: "1" });
    expect(draft?.sha256).toMatch(/^[0-9a-f]{64}$/);
    expect(draft).not.toHaveProperty("path");
  });

  let claimTask: AiTaskRecord;
  let acceptedClaimFindingId: string;
  await test.step("review the profile claim against its linked evidence and persist the decision", async () => {
    await runAiTask(page, "review_claims", "Review claims", "Completed");
    claimTask = (await packet(page, applicationId)).latest_ai_task!;
    const findings = await apiJson<AiFindingRecord[]>(page, `/ai-tasks/${claimTask.id}/findings`);
    const finding = findings.find((candidate) => candidate.payload.claim_id === profileFactId);
    expect(finding).toBeDefined();
    if (!finding) throw new Error(`No claim review finding for ${profileFactId}`);
    acceptedClaimFindingId = finding.id;
    expect(finding).toMatchObject({
      task_id: claimTask.id,
      finding_type: "claim_review_decision",
      verifier_state: "verified",
      decision: "pending",
      payload: expect.objectContaining({
        decision: "supported",
        evidence_refs: [(await packet(page, applicationId)).evidence[0].id]
      })
    });
    const ownClaim = page
      .locator("section[aria-label='AI findings'] li")
      .filter({ hasText: `${profileFactId}: supported` });
    await expect(ownClaim.getByText("Matches the supplied evidence", { exact: true })).toBeVisible();
    await ownClaim.getByRole("button", { name: "Accept", exact: true }).click();
    await expect(ownClaim.getByText("Decision: accepted", { exact: true })).toBeVisible();
  });

  let approvalId: string;
  await test.step("approve the exact current report and PDF checksums, then submit manually", async () => {
    const current = await packet(page, applicationId);
    const expectedPdfSha256 = current.selected_resume!.pdf_sha256!;
    const expectedReportId = current.latest_report!.id;
    await page.getByRole("button", { name: "Approve packet" }).click();
    await expect(page.getByText("Approved", { exact: true }).first()).toBeVisible();

    const approval = await downloadArtifact(
      page,
      "Export approval",
      `/applications/${applicationId}/artifacts/approval`,
      "application/json"
    );
    const approvalRecord = JSON.parse(approval.toString("utf-8"));
    approvalId = approvalRecord.id;
    expect(approvalRecord).toMatchObject({
      application_id: applicationId,
      resume_version_id: childResume.id,
      report_id: expectedReportId,
      pdf_sha256: expectedPdfSha256
    });

    await page.getByRole("button", { name: "Mark submitted manually" }).click();
    await expect(page.getByText("Submitted manually", { exact: true }).first()).toBeVisible();
  });

  await test.step("reload from the API and prove submission, provenance, and histories remain", async () => {
    const beforeReload = await packet(page, applicationId);
    await page.reload();
    await selectApplication(page, applicationId);
    await expect(page.getByRole("heading", { name: "Swiggy" })).toBeVisible();
    await expect(page.getByText("Submitted manually", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("Swiggy verified draft", { exact: true })).toBeVisible();
    await expect(page.locator("section[aria-label='Task status']")).toContainText("review_claims");
    await expect(page.locator("section[aria-label='Task status']")).toContainText(MOCK_PROVIDER_LABEL);
    await expect(page.locator("section[aria-label='Task status']")).toContainText("fixture-v1");
    await expect(page.locator("section[aria-label='Task status']")).toContainText("review_claims_v1");
    await expect(page.getByText("Decision: accepted", { exact: true })).toBeVisible();

    await page.getByText("History", { exact: true }).click();
    await expect(page.getByText(new RegExp(`Resume ${childResume.id} / Parent ${parentResume.id}`))).toBeVisible();
    const history = page.locator("details").filter({ hasText: "History" });
    await expect(history.locator("li").filter({ hasText: "resume_draft.created" })).toBeVisible();
    await expect(history.locator("li").filter({ hasText: "ai_task.completed" }).first()).toBeVisible();

    const restored = await packet(page, applicationId);
    expect(restored.application).toMatchObject({
      status: "submitted_manual",
      submission_state: "submitted_manual",
      selected_resume_version_id: childResume.id
    });
    expect(restored.selected_resume).toMatchObject({
      id: childResume.id,
      parent_resume_version_id: parentResume.id,
      tex_sha256: childResume.tex_sha256
    });
    expect(restored.approval).toMatchObject({ id: approvalId, resume_version_id: childResume.id });
    expect(restored.latest_ai_task).toMatchObject({
      id: claimTask.id,
      task_type: "review_claims",
      status: "completed",
      prompt_template_id: "review_claims_v1",
      prompt_sha256: expect.stringMatching(/^[0-9a-f]{64}$/)
    });
    expect(restored.versions).toEqual(beforeReload.versions);

    const restoredFindings = await apiJson<AiFindingRecord[]>(page, `/ai-tasks/${claimTask.id}/findings`);
    expect(restoredFindings).toContainEqual(expect.objectContaining({
      id: acceptedClaimFindingId,
      decision: "accepted",
      verifier_state: "verified"
    }));
  });

  expect(browserErrors).toEqual([]);
});
