import { readFileSync } from "node:fs";

import { expect, test, type Page, type TestInfo } from "@playwright/test";

import { sampleJdPath, writeCleanResume, writeRiskyResume } from "./fixtures";

const API_ORIGIN = "http://127.0.0.1:8765";
const NOW = "2026-07-22T08:00:00Z";

type DeskFixtureState = "unavailable" | "running" | "failed" | "safe" | "blocked";

function trackBrowserErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`console: ${message.text()}`);
  });
  return errors;
}

async function capturePage(page: Page, testInfo: TestInfo, name: string): Promise<void> {
  if (process.env.CAREEROPS_CAPTURE_SCREENSHOTS !== "1") return;
  await page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true });
}

async function captureChecks(page: Page, testInfo: TestInfo, name: string): Promise<void> {
  if (process.env.CAREEROPS_CAPTURE_SCREENSHOTS !== "1") return;
  await page.locator(".checks").screenshot({ path: testInfo.outputPath(`${name}.png`) });
}

function requirementsFixture() {
  const requiredQualifications = [
    "Strong data structures, algorithms, and problem-solving fundamentals",
    "Experience building and testing production software in a team",
    "Clear written communication for technical decisions"
  ];
  return {
    raw_ai: { required_qualifications: requiredQualifications },
    deterministic: { location: "Bengaluru, India" },
    user_corrected: { required_qualifications: requiredQualifications },
    uncertainty: { graduation_window: "Confirm against the official role posting" }
  };
}

function applicationFixture() {
  return {
    id: "app_fixture",
    company: "Goldman Sachs",
    role_title: "Software Engineering Analyst, India Engineering Campus Hiring",
    location: "Bengaluru, India",
    source_url: "https://example.com/roles/software-engineering-analyst",
    source_kind: "manual",
    jd_snapshot_id: "jd_fixture",
    status: "ready_for_review",
    selected_resume_version_id: "res_fixture",
    latest_report_id: "rep_fixture",
    approval_id: null,
    submission_state: "not_submitted",
    deadline: null,
    notes: "",
    role_type: "full_time",
    submitted_at: null,
    created_at: NOW,
    updated_at: NOW
  };
}

function providerFixture(state: DeskFixtureState) {
  return {
    id: "aip_codex2",
    provider_kind: "codex",
    display_name: "Codex account 2",
    identity_name: "codex2",
    enabled: true,
    privacy_mode: "cloud_external",
    endpoint: "codex2",
    model: "gpt-5.6-sol",
    available: state !== "unavailable",
    availability_detail: state === "unavailable" ? "codex2 account is not signed in" : "available",
    is_ai: true,
    data_leaves_machine: true,
    current_metadata: null,
    created_at: NOW,
    updated_at: NOW
  };
}

function taskFixture(state: DeskFixtureState) {
  const status = state === "running" ? "running" : state === "failed" ? "failed" : "completed";
  return {
    id: "ait_fixture",
    application_id: "app_fixture",
    task_type: "review_claims",
    provider_config_id: "aip_codex2",
    prompt_template_id: "review-claims-v1",
    prompt_sha256: "b8f1886d53775c57ff5f3b9c97872c37e55f90f1ae450a3e39b0a64ec9728e4a",
    input_artifact_ids: [],
    status,
    privacy_mode: "cloud_external",
    started_at: status === "running" ? NOW : null,
    completed_at: status === "running" ? null : NOW,
    error_summary: state === "failed" ? "Provider request failed before any findings were saved." : "",
    created_at: NOW,
    updated_at: NOW
  };
}

function findingFixtures(state: DeskFixtureState) {
  if (state !== "safe" && state !== "blocked") return [];
  const safe = state === "safe";
  return [{
    id: `aif_${state}`,
    task_id: "ait_fixture",
    finding_type: "claim_review",
    severity: safe ? "info" : "blocker",
    claim_text: safe
      ? "Implemented repository-backed application workflows"
      : "Improved platform performance by 70+ percent",
    evidence_item_id: safe ? "evi_fixture" : null,
    recommendation: safe
      ? "Evidence matches the public claim."
      : "Add repository evidence or remove the unsupported metric.",
    verifier_state: safe ? "verified" : "blocked",
    decision: "pending",
    payload: {},
    created_at: NOW,
    updated_at: NOW
  }];
}

function packetFixture(state: DeskFixtureState) {
  const application = applicationFixture();
  return {
    application,
    job_description: {
      id: "jd_fixture",
      application_id: application.id,
      source_url: application.source_url,
      raw_text_sha256: "f".repeat(64),
      captured_at: NOW,
      parser_version: "fixture-v1",
      requirements: requirementsFixture()
    },
    selected_resume: {
      id: "res_fixture",
      name: "Goldman Sachs application resume",
      role_family: "software_engineering",
      tex_sha256: "a".repeat(64),
      pdf_sha256: "b".repeat(64),
      parent_resume_version_id: null,
      render_status: "rendered",
      parse_status: "parsed",
      page_count: 1,
      created_at: NOW,
      updated_at: NOW
    },
    latest_report: {
      id: "rep_fixture",
      application_id: application.id,
      resume_version_id: "res_fixture",
      jd_snapshot_id: "jd_fixture",
      ai_task_id: "ait_fixture",
      pdf_sha256: "b".repeat(64),
      blocking_issue_count: 0,
      warning_count: 1,
      fit: { score: 88 },
      risks: { blockers: [], warnings: ["Confirm the role deadline before submission."] },
      created_at: NOW
    },
    approval: null,
    latest_ai_task: state === "unavailable" ? null : taskFixture(state),
    approval_eligibility: {
      eligible: false,
      reason: "Run AI review for the current JD and resume before approval.",
      ai_task_id: null
    },
    evidence: [],
    versions: [{
      id: "apv_fixture",
      application_id: application.id,
      version_number: 1,
      event_type: "application.created",
      snapshot: {},
      created_at: NOW
    }]
  };
}

async function installDeskFixture(page: Page, state: DeskFixtureState): Promise<void> {
  const application = applicationFixture();
  const task = taskFixture(state);
  await page.route(`${API_ORIGIN}/**`, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path === "/applications") {
      await route.fulfill({ json: [application] });
      return;
    }
    if (request.method() === "GET" && path === `/applications/${application.id}/packet`) {
      await route.fulfill({ json: packetFixture(state) });
      return;
    }
    if (request.method() === "GET" && path === "/ai/providers") {
      await route.fulfill({ json: [providerFixture(state)] });
      return;
    }
    if (request.method() === "GET" && path === `/applications/${application.id}/requirements`) {
      await route.fulfill({ json: requirementsFixture() });
      return;
    }
    if (request.method() === "GET" && path === `/ai-tasks/${task.id}`) {
      await route.fulfill({ json: task });
      return;
    }
    if (request.method() === "GET" && path === `/ai-tasks/${task.id}/findings`) {
      await route.fulfill({ json: findingFixtures(state) });
      return;
    }
    await route.fulfill({ status: 404, json: { detail: `No fixture for ${request.method()} ${path}` } });
  });
}

async function openFixtureDesk(page: Page, state: DeskFixtureState): Promise<void> {
  await installDeskFixture(page, state);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Goldman Sachs" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "AI review" })).toBeVisible();
}

test("empty application desk shows the start state", async ({ page }) => {
  await page.route(`${API_ORIGIN}/applications`, async (route) => {
    await route.fulfill({ status: 200, json: [] });
  });

  await page.goto("/");

  await expect(page.getByText("No applications yet. Add a JD to start a packet.")).toBeVisible();
});

test("deterministic compatibility verifier reports risks but cannot approve or submit", async ({ page }, testInfo) => {
  const riskyResumePath = writeRiskyResume(testInfo.outputPath("risky_resume.tex"));
  const cleanResumePath = writeCleanResume(testInfo.outputPath("clean_resume.tex"));
  await page.goto("/");

  await page.getByRole("button", { name: "Add application" }).click();
  await page.getByLabel("Company").fill("Swiggy");
  await page.getByLabel("Role title").fill("Data Science Intern");
  await page.getByLabel("Location").fill("Bangalore");
  await page.getByLabel("Source URL").fill("https://example.com/swiggy");
  await page.getByLabel("JD text").fill(readFileSync(sampleJdPath, "utf-8"));
  await page.getByRole("button", { name: "Save application" }).click();

  await expect(page.getByRole("heading", { name: "Swiggy" })).toBeVisible();

  await page.getByLabel("Resume name").fill("Risky resume");
  await page.getByLabel("LaTeX file").setInputFiles(riskyResumePath);
  await page.getByRole("button", { name: "Upload resume" }).click();
  await expect(page.getByText("Risky resume")).toBeVisible();
  await page.getByRole("button", { name: "Render PDF" }).click();
  await expect(page.getByText("rendered")).toBeVisible({ timeout: 60000 });
  await page.getByLabel("Application and resume").getByRole("button", { name: "Run verifier" }).click();
  await expect(page.getByText("NDA-sensitive wording")).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve packet" })).toBeDisabled();

  await page.getByLabel("Resume name").fill("Application resume");
  await page.getByLabel("LaTeX file").setInputFiles(cleanResumePath);
  await page.getByRole("button", { name: "Upload resume" }).click();
  await expect(page.getByText("Application resume")).toBeVisible();
  await page.getByRole("button", { name: "Render PDF" }).click();
  await expect(page.getByText("rendered")).toBeVisible({ timeout: 60000 });
  await page.getByLabel("Application and resume").getByRole("button", { name: "Run verifier" }).click();
  await expect(page.locator(".report p").filter({ hasText: "blockers" }).first()).toContainText("0 blockers");
  await expect(page.getByText("Run AI review for the current JD and resume before approval.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve packet" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Mark submitted manually" })).toBeDisabled();

  const applications = await page.request.get(`${API_ORIGIN}/applications`);
  const [application] = await applications.json() as Array<{ id: string }>;
  const packet = await page.request.get(`${API_ORIGIN}/applications/${application.id}/packet`);
  expect(await packet.json()).toMatchObject({
    approval: null,
    approval_eligibility: { eligible: false, ai_task_id: null }
  });
});

for (const width of [320, 375, 414, 768, 1280]) {
  test(`populated workbench remains operable at ${width}px`, async ({ page }, testInfo) => {
    const browserErrors = trackBrowserErrors(page);
    await page.setViewportSize({ width, height: 900 });
    await openFixtureDesk(page, "safe");
    await expect(page.getByText("Verified finding")).toBeVisible();

    const audit = await page.evaluate(() => {
      const visible = (element: Element): element is HTMLElement => {
        const htmlElement = element as HTMLElement;
        const style = getComputedStyle(htmlElement);
        const rect = htmlElement.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
      };
      const regionElements = [...document.querySelectorAll<HTMLElement>(".rail, .workspace, .checks")]
        .filter(visible);
      const regions = regionElements
        .map((element) => ({ name: element.className, rect: element.getBoundingClientRect().toJSON() }));
      const regionOverlaps: string[] = [];
      for (let index = 0; index < regions.length; index += 1) {
        for (let other = index + 1; other < regions.length; other += 1) {
          const a = regions[index];
          const b = regions[other];
          const overlaps = !(
            a.rect.right <= b.rect.left || b.rect.right <= a.rect.left ||
            a.rect.bottom <= b.rect.top || b.rect.bottom <= a.rect.top
          );
          if (overlaps) regionOverlaps.push(`${a.name}/${b.name}`);
        }
      }

      const controls = [...document.querySelectorAll<HTMLElement>(
        "button:not(.application-list__item), select, input:not([type='checkbox'])"
      )].filter(visible);
      const undersized = controls
        .filter((element) => {
          const rect = element.getBoundingClientRect();
          return rect.width < 44 || rect.height < 44;
        })
        .map((element) => element.getAttribute("aria-label") || element.textContent?.trim() || element.tagName);
      const occluded = controls
        .filter((element) => {
          const rect = element.getBoundingClientRect();
          if (rect.left < 0 || rect.right > document.documentElement.clientWidth) return true;
          const x = Math.min(Math.max(rect.left + rect.width / 2, 0), innerWidth - 1);
          const y = Math.min(Math.max(rect.top + Math.min(rect.height / 2, 20), 0), innerHeight - 1);
          if (rect.bottom < 0 || rect.top > innerHeight) return false;
          return !document.elementsFromPoint(x, y).some((hit) => element === hit || element.contains(hit));
        })
        .map((element) => element.getAttribute("aria-label") || element.textContent?.trim() || element.tagName);
      const wrappedCommands = [...document.querySelectorAll<HTMLButtonElement>("button:not(.application-list__item)")]
        .filter(visible)
        .filter((button) => {
          const walker = document.createTreeWalker(button, NodeFilter.SHOW_TEXT);
          const lineTops = new Set<number>();
          let node = walker.nextNode();
          while (node) {
            if (node.textContent?.trim()) {
              const range = document.createRange();
              range.selectNodeContents(node);
              for (const rect of range.getClientRects()) lineTops.add(Math.round(rect.top));
            }
            node = walker.nextNode();
          }
          return lineTops.size > 1;
        })
        .map((button) => button.textContent?.trim() || "unnamed button");
      const escapedContent = regionElements.flatMap((region) => {
        const regionRect = region.getBoundingClientRect();
        return [...region.querySelectorAll<HTMLElement>("*")]
          .filter(visible)
          .filter((element) => {
            const rect = element.getBoundingClientRect();
            return rect.left < regionRect.left - 0.5 || rect.right > regionRect.right + 0.5;
          })
          .map((element) => `${region.className}:${element.tagName}:${element.textContent?.trim().slice(0, 48)}`);
      });

      return {
        clientWidth: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
        regions,
        regionOverlaps,
        undersized,
        occluded,
        wrappedCommands,
        escapedContent,
        mojibake: /Â|Ã|â/.test(document.body.innerText)
      };
    });

    expect(audit.scrollWidth, JSON.stringify(audit)).toBe(audit.clientWidth);
    expect(audit.regionOverlaps, JSON.stringify(audit)).toEqual([]);
    expect(audit.undersized, JSON.stringify(audit)).toEqual([]);
    expect(audit.occluded, JSON.stringify(audit)).toEqual([]);
    expect(audit.wrappedCommands, JSON.stringify(audit)).toEqual([]);
    expect(audit.escapedContent, JSON.stringify(audit)).toEqual([]);
    expect(audit.mojibake, JSON.stringify(audit)).toBe(false);

    if (width === 1280) {
      expect(audit.regions.map((region) => region.name)).toEqual(["rail", "workspace", "checks"]);
      const [rail, workspace, checks] = audit.regions;
      expect(rail.rect.left).toBe(0);
      expect(rail.rect.right).toBeLessThanOrEqual(workspace.rect.left + 0.5);
      expect(workspace.rect.right).toBeLessThanOrEqual(checks.rect.left + 0.5);
      expect(checks.rect.right).toBeLessThanOrEqual(audit.clientWidth + 0.5);
      expect(rail.rect.width).toBeGreaterThanOrEqual(256);
      expect(rail.rect.width).toBeLessThanOrEqual(340);
      expect(workspace.rect.width).toBeGreaterThanOrEqual(480);
      expect(workspace.rect.width).toBeLessThanOrEqual(700);
      expect(checks.rect.width).toBeGreaterThanOrEqual(288);
      expect(checks.rect.width).toBeLessThanOrEqual(410);
    }

    const provider = page.getByLabel("Provider");
    for (let tabIndex = 0; tabIndex < 30; tabIndex += 1) {
      if (await provider.evaluate((element) => document.activeElement === element)) break;
      await page.keyboard.press("Tab");
    }
    await expect(provider).toBeFocused();
    const focus = await provider.evaluate((element) => {
      const style = getComputedStyle(element);
      return { color: style.outlineColor, style: style.outlineStyle, width: parseFloat(style.outlineWidth) };
    });
    expect(focus.style).toBe("solid");
    expect(focus.width).toBeGreaterThanOrEqual(2);
    expect(focus.color).not.toBe("rgba(0, 0, 0, 0)");
    await capturePage(page, testInfo, `workbench-${width}`);
    expect(browserErrors).toEqual([]);
  });
}

test("requirements commands stay contained and preserve primary hierarchy at 320px", async ({ page }, testInfo) => {
  const browserErrors = trackBrowserErrors(page);
  await page.setViewportSize({ width: 320, height: 900 });
  await openFixtureDesk(page, "safe");
  await page.getByRole("tab", { name: "Requirements" }).click();
  await expect(page.getByRole("heading", { name: "Edit user corrections" })).toBeVisible();

  const addListItem = page.getByRole("button", { name: "Add list item for required_qualifications" });
  await expect(addListItem).toBeVisible();
  await expect(addListItem).toHaveText("Add list item");

  const audit = await page.evaluate(() => {
    const isVisible = (element: HTMLElement) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    };
    const controls = [...document.querySelectorAll<HTMLElement>("button, input, select, textarea, summary")]
      .filter(isVisible);
    const outOfBounds = controls.flatMap((control) => {
      const rect = control.getBoundingClientRect();
      const region = control.closest<HTMLElement>(".rail, .workspace, .checks");
      const regionRect = region?.getBoundingClientRect();
      const escaped = rect.left < -0.5 || rect.right > document.documentElement.clientWidth + 0.5 ||
        !regionRect || rect.left < regionRect.left - 0.5 || rect.right > regionRect.right + 0.5 ||
        rect.top < regionRect.top - 0.5 || rect.bottom > regionRect.bottom + 0.5;
      return escaped ? [control.getAttribute("aria-label") || control.textContent?.trim() || control.tagName] : [];
    });
    const commandAudit = [...document.querySelectorAll<HTMLButtonElement>("button:not(.application-list__item)")]
      .filter(isVisible)
      .map((button) => {
        const walker = document.createTreeWalker(button, NodeFilter.SHOW_TEXT);
        const lineTops = new Set<number>();
        let node = walker.nextNode();
        while (node) {
          if (node.textContent?.trim()) {
            const range = document.createRange();
            range.selectNodeContents(node);
            for (const rect of range.getClientRects()) lineTops.add(Math.round(rect.top));
          }
          node = walker.nextNode();
        }
        return {
          label: button.getAttribute("aria-label") || button.textContent?.trim() || "unnamed button",
          lines: lineTops.size,
          clipped: button.scrollWidth > button.clientWidth + 1 || button.scrollHeight > button.clientHeight + 1
        };
      });
    return {
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
      outOfBounds,
      wrapped: commandAudit.filter((command) => command.lines > 1).map((command) => command.label),
      clipped: commandAudit.filter((command) => command.clipped).map((command) => command.label)
    };
  });

  expect(audit.scrollWidth, JSON.stringify(audit)).toBe(audit.clientWidth);
  expect(audit.outOfBounds, JSON.stringify(audit)).toEqual([]);
  expect(audit.wrapped, JSON.stringify(audit)).toEqual([]);
  expect(audit.clipped, JSON.stringify(audit)).toEqual([]);

  const hierarchy = await page.evaluate(() => {
    const add = [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent?.trim() === "Add correction");
    const save = [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent?.trim() === "Save corrections");
    const probe = document.createElement("span");
    probe.style.backgroundColor = "var(--color-accent)";
    document.body.append(probe);
    const accent = getComputedStyle(probe).backgroundColor;
    probe.remove();
    if (!add || !save) throw new Error("Expected correction commands");
    return {
      accent,
      add: getComputedStyle(add).backgroundColor,
      save: getComputedStyle(save).backgroundColor
    };
  });
  expect(hierarchy.save).toBe(hierarchy.accent);
  expect(hierarchy.add).not.toBe(hierarchy.accent);
  await capturePage(page, testInfo, "requirements-320");
  expect(browserErrors).toEqual([]);
});

test("representative commands reflow without clipping at 200 percent text size", async ({ page }, testInfo) => {
  const browserErrors = trackBrowserErrors(page);
  await page.setViewportSize({ width: 320, height: 900 });
  await openFixtureDesk(page, "safe");
  await page.getByRole("combobox", { name: "Task", exact: true }).selectOption("suggest_resume_edits");
  await page.getByRole("tab", { name: "Requirements" }).click();
  await expect(page.getByRole("heading", { name: "Edit user corrections" })).toBeVisible();
  await page.evaluate(() => {
    document.documentElement.style.fontSize = "200%";
  });

  const commands = [
    page.getByRole("button", { name: "Add application" }),
    page.getByRole("button", { name: "Suggest resume edits" }),
    page.getByRole("button", { name: "Save corrections" }),
    page.getByRole("button", { name: "Mark submitted manually" })
  ];
  for (const command of commands) await expect(command).toBeVisible();

  const audit = await page.evaluate(() => {
    const labels = ["Add application", "Suggest resume edits", "Save corrections", "Mark submitted manually"];
    const buttons = [...document.querySelectorAll<HTMLButtonElement>("button")]
      .filter((button) => labels.includes(button.textContent?.trim() || ""));
    const commandGeometry = buttons.map((button) => {
      const rect = button.getBoundingClientRect();
      const region = button.closest<HTMLElement>(".rail, .workspace, .checks");
      const regionRect = region?.getBoundingClientRect();
      const walker = document.createTreeWalker(button, NodeFilter.SHOW_TEXT);
      const textRects: DOMRect[] = [];
      let node = walker.nextNode();
      while (node) {
        if (node.textContent?.trim()) {
          const range = document.createRange();
          range.selectNodeContents(node);
          textRects.push(...range.getClientRects());
        }
        node = walker.nextNode();
      }
      return {
        label: button.textContent?.trim() || "",
        contained: Boolean(regionRect) && rect.left >= regionRect!.left - 0.5 &&
          rect.right <= regionRect!.right + 0.5 && rect.top >= regionRect!.top - 0.5 &&
          rect.bottom <= regionRect!.bottom + 0.5 && rect.left >= -0.5 &&
          rect.right <= document.documentElement.clientWidth + 0.5,
        clipped: button.scrollWidth > button.clientWidth + 1 || button.scrollHeight > button.clientHeight + 1 ||
          textRects.some((textRect) => textRect.left < rect.left - 0.5 || textRect.right > rect.right + 0.5 ||
            textRect.top < rect.top - 0.5 || textRect.bottom > rect.bottom + 0.5),
        rect: rect.toJSON()
      };
    });
    const overlaps: string[] = [];
    for (let index = 0; index < commandGeometry.length; index += 1) {
      for (let other = index + 1; other < commandGeometry.length; other += 1) {
        const a = commandGeometry[index];
        const b = commandGeometry[other];
        const overlapsArea = !(a.rect.right <= b.rect.left || b.rect.right <= a.rect.left ||
          a.rect.bottom <= b.rect.top || b.rect.bottom <= a.rect.top);
        if (overlapsArea) overlaps.push(`${a.label}/${b.label}`);
      }
    }
    return {
      rootFontSize: parseFloat(getComputedStyle(document.documentElement).fontSize),
      commandGeometry,
      overlaps
    };
  });

  expect(audit.rootFontSize).toBe(32);
  expect(audit.commandGeometry.map((command) => command.label).sort()).toEqual([
    "Add application",
    "Mark submitted manually",
    "Save corrections",
    "Suggest resume edits"
  ]);
  expect(audit.commandGeometry.filter((command) => !command.contained), JSON.stringify(audit)).toEqual([]);
  expect(audit.commandGeometry.filter((command) => command.clipped), JSON.stringify(audit)).toEqual([]);
  expect(audit.overlaps, JSON.stringify(audit)).toEqual([]);
  await capturePage(page, testInfo, "text-resize-200-320");
  expect(browserErrors).toEqual([]);
});

test("provider unavailable state is explicit and disables its command", async ({ page }, testInfo) => {
  const browserErrors = trackBrowserErrors(page);
  await openFixtureDesk(page, "unavailable");
  const unavailable = page.getByText("Provider unavailable: codex2 account is not signed in");
  await expect(unavailable).toBeVisible();
  await expect(page.getByRole("button", { name: "Run AI review" })).toBeDisabled();
  expect(await unavailable.evaluate((element) => parseFloat(getComputedStyle(element).borderInlineStartWidth)))
    .toBe(1);
  await captureChecks(page, testInfo, "state-provider-unavailable");
  expect(browserErrors).toEqual([]);
});

test("running state keeps command geometry stable and visible", async ({ page }, testInfo) => {
  const browserErrors = trackBrowserErrors(page);
  await openFixtureDesk(page, "running");
  await expect(page.getByRole("status", { name: "" }).filter({ hasText: "Running" })).toBeVisible();
  const command = page.getByRole("button", { name: "Run AI review" });
  await expect(command).toBeDisabled();
  const geometry = await command.evaluate((element) => {
    const icon = element.querySelector("svg")?.getBoundingClientRect();
    const rect = element.getBoundingClientRect();
    return { height: rect.height, iconHeight: icon?.height, iconWidth: icon?.width };
  });
  expect(geometry.height).toBeGreaterThanOrEqual(44);
  expect(geometry.iconHeight).toBe(16);
  expect(geometry.iconWidth).toBe(16);
  await captureChecks(page, testInfo, "state-running");
  expect(browserErrors).toEqual([]);
});

test("failed task state uses a persistent blocker treatment", async ({ page }, testInfo) => {
  const browserErrors = trackBrowserErrors(page);
  await openFixtureDesk(page, "failed");
  const failure = page.getByText("Provider request failed before any findings were saved.");
  await expect(failure).toBeVisible();
  const colors = await failure.evaluate((element) => {
    const style = getComputedStyle(element);
    const probe = document.createElement("span");
    probe.style.color = "var(--color-danger)";
    document.body.append(probe);
    const expected = getComputedStyle(probe).color;
    probe.remove();
    return { actual: style.color, expected };
  });
  expect(colors.actual).toBe(colors.expected);
  expect(await failure.evaluate((element) => parseFloat(getComputedStyle(element).borderInlineStartWidth)))
    .toBe(1);
  await captureChecks(page, testInfo, "state-failed");
  expect(browserErrors).toEqual([]);
});

for (const state of ["safe", "blocked"] as const) {
  test(`${state} finding has a distinct evidence state`, async ({ page }, testInfo) => {
    const browserErrors = trackBrowserErrors(page);
    await openFixtureDesk(page, state);
    const finding = page.locator("section[aria-label='AI findings'] .issue-list > li");
    await expect(finding).toHaveCount(1);
    await expect(finding).toContainText(state === "safe" ? "Verified finding" : "Blocked by verifier");
    const colors = await finding.evaluate((element, fixtureState) => {
      const probe = document.createElement("span");
      probe.style.color = `var(${fixtureState === "safe" ? "--color-success" : "--color-danger"})`;
      document.body.append(probe);
      const expected = getComputedStyle(probe).color;
      probe.remove();
      return {
        actual: getComputedStyle(element).borderInlineStartColor,
        expected,
        width: parseFloat(getComputedStyle(element).borderInlineStartWidth)
      };
    }, state);
    expect(colors.actual).toBe(colors.expected);
    expect(colors.width).toBe(1);
    await captureChecks(page, testInfo, `state-finding-${state}`);
    expect(browserErrors).toEqual([]);
  });
}

test("supporting exports stay neutral beside the primary approval command", async ({ page }) => {
  await openFixtureDesk(page, "safe");
  const colors = await page.evaluate(() => {
    const exportButton = [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent?.includes("Export JD"));
    const approvalButton = [...document.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent?.includes("Approve packet"));
    if (!exportButton || !approvalButton) throw new Error("Expected export and approval commands");
    return {
      approval: getComputedStyle(approvalButton).backgroundColor,
      supporting: getComputedStyle(exportButton).backgroundColor
    };
  });
  expect(colors.supporting).not.toBe(colors.approval);
});
