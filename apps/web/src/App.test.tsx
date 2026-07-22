import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";

import App, { statusLabel } from "./App";
import type { ApplicationPacket, ApplicationRecord, EvidenceRecord, JsonValue, ReportRecord, RequirementsRecord } from "./api";
import ReviewPanel from "./components/ReviewPanel";
import { useApplicationPacket } from "./hooks/useApplicationPacket";

const application = (id: string, company: string): ApplicationRecord => ({
  id,
  company,
  role_title: "Software Engineer",
  location: "Remote",
  source_url: "https://example.com/role",
  source_kind: "manual",
  jd_snapshot_id: `jd_${id}`,
  status: "draft",
  selected_resume_version_id: null,
  latest_report_id: null,
  approval_id: null,
  submission_state: "not_submitted",
  deadline: null,
  notes: "",
  role_type: "",
  submitted_at: null,
  created_at: "2026-07-21T00:00:00Z",
  updated_at: "2026-07-21T00:00:00Z"
});

const packet = (
  record: ApplicationRecord,
  overrides: Partial<ApplicationPacket> = {},
  approvalEligibility = {
    eligible: false,
    reason: "Run AI review for the current JD and resume before approval.",
    ai_task_id: null as string | null
  }
): ApplicationPacket => {
  const result = {
    application: record,
    job_description: {
      id: record.jd_snapshot_id,
      application_id: record.id,
      source_url: record.source_url,
      raw_text_sha256: "jd-checksum",
      captured_at: "2026-07-21T00:00:00Z",
      parser_version: "v1",
      requirements: { raw_ai: {}, deterministic: {}, user_corrected: {}, uncertainty: {} }
    },
    selected_resume: null,
    latest_report: null,
    approval: null,
    latest_ai_task: null,
    approval_eligibility: approvalEligibility,
    evidence: [],
    versions: [],
    ...overrides
  };
  return result;
};

function jsonResponse(body: unknown, ok = true, status = 200, statusText = "OK") {
  return { ok, status, statusText, json: async () => body };
}

function installDefaultFetch(applications: ApplicationRecord[] = []) {
  const packets = new Map(applications.map((item) => [item.id, packet(item)]));
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/applications")) return jsonResponse(applications);
    if (url.endsWith("/ai/providers")) return jsonResponse([]);
    const packetMatch = url.match(/\/applications\/(app_[^/]+)\/packet$/);
    if (packetMatch) return jsonResponse(packets.get(packetMatch[1]));
    throw new Error(`Unhandled request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("Application Desk UI", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  test("shows the approved empty state and validates the add form", async () => {
    installDefaultFetch();
    render(<App />);

    expect(await screen.findByText("No applications yet. Add a JD to start a packet.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add application" }));
    await userEvent.click(screen.getByRole("button", { name: "Save application" }));

    expect(screen.getByRole("alert")).toHaveTextContent("Company, role title, and JD text are required.");
  });

  test("reload restores the selected resume, report, latest task, and history", async () => {
    const record = { ...application("app_1", "Swiggy"), status: "ready_for_review", selected_resume_version_id: "res_1", latest_report_id: "rpt_1" };
    const restored = packet(record, {
      selected_resume: {
        id: "res_1",
        name: "Reviewed resume",
        role_family: "software",
        tex_sha256: "tex-checksum",
        pdf_sha256: "pdf-checksum",
        parent_resume_version_id: "res_parent",
        render_status: "rendered",
        parse_status: "parsed",
        page_count: 1,
        created_at: "2026-07-21T00:00:00Z",
        updated_at: "2026-07-21T00:00:00Z"
      },
      latest_report: {
        id: "rpt_1",
        application_id: "app_1",
        resume_version_id: "res_1",
        jd_snapshot_id: "jd_app_1",
        ai_task_id: "ait_1",
        pdf_sha256: "pdf-checksum",
        blocking_issue_count: 1,
        warning_count: 0,
        fit: { matched_requirements: [], missing_requirements: [], exact_keyword_gaps: [], summary_score: 0 },
        risks: { blockers: [{ severity: "blocking", label: "Missing evidence", source: "resume", why: "Claim is unverified", next_action: "Add evidence" }], warnings: [] },
        created_at: "2026-07-21T00:00:00Z"
      },
      latest_ai_task: {
        id: "ait_1",
        application_id: "app_1",
        task_type: "analyze_fit",
        provider_config_id: "provider_codex2",
        prompt_template_id: "prompt_fit_v1",
        prompt_sha256: "prompt-checksum",
        input_artifact_ids: [],
        status: "completed",
        privacy_mode: "cloud_external",
        started_at: "2026-07-21T00:00:00Z",
        completed_at: "2026-07-21T00:01:00Z",
        error_summary: "",
        created_at: "2026-07-21T00:00:00Z",
        updated_at: "2026-07-21T00:01:00Z"
      },
      versions: [{ id: "ver_1", application_id: "app_1", version_number: 2, event_type: "packet.reviewed", snapshot: {}, created_at: "2026-07-21T00:00:00Z" }]
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/applications")) return jsonResponse([record]);
        if (url.endsWith("/applications/app_1/packet")) return jsonResponse(restored);
        if (url.endsWith("/ai/providers")) return jsonResponse([{ id: "provider_codex2", provider_kind: "codex", display_name: "Codex codex2", identity_name: "codex2", enabled: true, privacy_mode: "cloud_external", endpoint: "", model: "gpt-5.6-sol", available: true, availability_detail: "available", is_ai: true, data_leaves_machine: true, current_metadata: null, created_at: "", updated_at: "" }]);
        if (url.endsWith("/ai-tasks/ait_1/findings")) return jsonResponse([]);
        throw new Error(`Unhandled request: ${url}`);
      })
    );

    render(<App />);

    expect(await screen.findByText("Reviewed resume")).toBeInTheDocument();
    expect(screen.getByText("Missing evidence")).toBeInTheDocument();
    expect(screen.getByText("Completed")).toBeInTheDocument();
    await userEvent.click(screen.getByText("History"));
    expect(screen.getByText("packet.reviewed")).toBeInTheDocument();
    expect(screen.getByText(/Parent res_parent/)).toBeInTheDocument();
    expect(screen.queryByText(/tex_path|pdf_path/i)).not.toBeInTheDocument();
  });

  test("keeps verifier-only analysis inspectable but blocks approval and submission", async () => {
    let record = application("app_1", "Swiggy");
    let currentPacket = packet(record);
    const uploaded = { id: "res_1", name: "Application resume", role_family: "general", tex_sha256: "tex", pdf_sha256: null, parent_resume_version_id: null, render_status: "not_rendered", parse_status: "not_parsed", page_count: null, created_at: "", updated_at: "" };
    const rendered = { ...uploaded, pdf_sha256: "pdf", render_status: "rendered", parse_status: "parsed", page_count: 1 };
    const report = { id: "rpt_1", application_id: "app_1", resume_version_id: "res_1", jd_snapshot_id: "jd_app_1", ai_task_id: null, pdf_sha256: "pdf", blocking_issue_count: 0, warning_count: 0, fit: { matched_requirements: [], missing_requirements: [], exact_keyword_gaps: [], summary_score: 100 }, risks: { blockers: [], warnings: [] }, created_at: "" };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/applications") && !init?.method) return jsonResponse([record]);
      if (url.endsWith("/applications/app_1/packet")) return jsonResponse(currentPacket);
      if (url.endsWith("/ai/providers")) return jsonResponse([]);
      if (url.endsWith("/applications/app_1/resume")) {
        record = { ...record, selected_resume_version_id: "res_1" };
        currentPacket = packet(record, { selected_resume: uploaded });
        return jsonResponse(uploaded, true, 201);
      }
      if (url.endsWith("/resumes/res_1/render")) {
        currentPacket = packet(record, { selected_resume: rendered });
        return jsonResponse(rendered);
      }
      if (url.endsWith("/applications/app_1/analyze")) {
        record = { ...record, status: "ready_for_review", latest_report_id: "rpt_1" };
        currentPacket = packet(record, { selected_resume: rendered, latest_report: report });
        return jsonResponse(report);
      }
      throw new Error(`Unhandled request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    await screen.findByRole("heading", { name: "Swiggy" });
    await userEvent.upload(screen.getByLabelText("LaTeX file"), new File(["resume"], "resume.tex", { type: "text/plain" }));
    await userEvent.click(screen.getByRole("button", { name: "Upload resume" }));
    expect(await screen.findByText("Application resume")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Render PDF" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Run verifier" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Run verifier" }));
    expect(await screen.findByText("blockers")).toHaveTextContent("0 blockers");
    expect(screen.getByText("Run AI review for the current JD and resume before approval.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve packet" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Mark submitted manually" })).toBeDisabled();
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/approve"))).toBe(false);
  });

  test("enables approval after an injected AI mock completes analyze_fit", async () => {
    let record = {
      ...application("app_1", "Swiggy"),
      status: "ready_for_review",
      selected_resume_version_id: "res_1",
      latest_report_id: "rep_verifier"
    };
    const resume = { id: "res_1", name: "Application resume", role_family: "general", tex_sha256: "tex", pdf_sha256: "pdf", parent_resume_version_id: null, render_status: "rendered", parse_status: "parsed", page_count: 1, created_at: "", updated_at: "" };
    const verifierReport = { id: "rep_verifier", application_id: "app_1", resume_version_id: "res_1", jd_snapshot_id: "jd_app_1", ai_task_id: null, pdf_sha256: "pdf", blocking_issue_count: 0, warning_count: 0, fit: { score: 100 }, risks: { blockers: [], warnings: [] }, created_at: "" };
    const aiReport = { ...verifierReport, id: "rep_ai", ai_task_id: "ait_1" };
    const completedTask = { id: "ait_1", application_id: "app_1", task_type: "analyze_fit" as const, provider_config_id: "aip_mock", prompt_template_id: "analyze_fit_v1", prompt_sha256: "a".repeat(64), input_artifact_ids: [], status: "completed" as const, privacy_mode: "local_private", started_at: "", completed_at: "", error_summary: "", created_at: "", updated_at: "" };
    let currentPacket = packet(record, { selected_resume: resume, latest_report: verifierReport });
    let approved = false;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/applications") && !init?.method) return jsonResponse([record]);
      if (url.endsWith("/applications/app_1/packet")) return jsonResponse(currentPacket);
      if (url.endsWith("/ai/providers")) return jsonResponse([{ id: "aip_mock", provider_kind: "mock", display_name: "AI fixture", identity_name: "", enabled: true, privacy_mode: "local_private", endpoint: "", model: "fixture-v1", available: true, availability_detail: "available", is_ai: true, data_leaves_machine: false, current_metadata: null, created_at: "", updated_at: "" }]);
      if (url.endsWith("/applications/app_1/ai-tasks")) {
        record = { ...record, latest_report_id: "rep_ai" };
        currentPacket = packet(
          record,
          { selected_resume: resume, latest_report: aiReport, latest_ai_task: completedTask },
          { eligible: true, reason: "AI review is current and complete.", ai_task_id: "ait_1" }
        );
        return jsonResponse(completedTask, true, 202);
      }
      if (url.endsWith("/ai-tasks/ait_1/findings")) return jsonResponse([]);
      if (url.endsWith("/applications/app_1/approve")) {
        approved = true;
        return jsonResponse({ id: "apr_1", application_id: "app_1", resume_version_id: "res_1", report_id: "rep_ai", pdf_sha256: "pdf", approved_at: "", approval_note: "Reviewed PDF and report" });
      }
      throw new Error(`Unhandled request: ${url}`);
    }));

    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Run AI review" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve packet" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Approve packet" }));
    await waitFor(() => expect(approved).toBe(true));
  });

  test("uses typed requirement corrections and preserves untouched structured values", async () => {
    const record = application("app_1", "Swiggy");
    let requirements: RequirementsRecord = {
      raw_ai: { location: "unknown" },
      deterministic: { required_skills: ["Python"] },
      user_corrected: {
        location: "Unknown",
        compensation: { currency: "INR", bands: [10, 20] }
      },
      uncertainty: { location: "not stated" }
    };
    let savedBody: unknown;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/applications")) return jsonResponse([record]);
        if (url.endsWith("/applications/app_1/packet")) return jsonResponse(packet(record));
        if (url.endsWith("/ai/providers")) return jsonResponse([]);
        if (url.endsWith("/applications/app_1/requirements") && init?.method === "PUT") {
          savedBody = JSON.parse(String(init.body));
          requirements = { ...requirements, user_corrected: (savedBody as { user_corrected: Record<string, JsonValue> }).user_corrected };
          return jsonResponse(requirements);
        }
        if (url.endsWith("/applications/app_1/requirements")) return jsonResponse(requirements);
        throw new Error(`Unhandled request: ${url}`);
      })
    );
    render(<App />);

    await userEvent.click(await screen.findByRole("tab", { name: "Requirements" }));
    expect(await screen.findByRole("heading", { name: "Unknown and uncertain" })).toBeInTheDocument();
    expect(await screen.findByText("not stated")).toBeInTheDocument();
    expect(screen.queryByText("Structured value")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /json/i })).not.toBeInTheDocument();
    expect(screen.getByText("currency")).toBeInTheDocument();
    expect(screen.getByText("INR")).toBeInTheDocument();
    expect(screen.getByText("bands")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Correction kind for location"), "number");
    const location = screen.getByLabelText("Correction value for location");
    await userEvent.clear(location);
    await userEvent.click(screen.getByRole("button", { name: "Save corrections" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Enter a valid number for location.");
    expect(savedBody).toBeUndefined();

    await userEvent.type(location, "3");
    await userEvent.click(screen.getByRole("button", { name: "Add correction" }));
    await userEvent.type(screen.getByLabelText("Correction key 3"), "skills");
    await userEvent.selectOptions(screen.getByLabelText("Correction kind for skills"), "list");
    await userEvent.click(screen.getByRole("button", { name: "Save corrections" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Enter a value for list item 1 in skills.");

    await userEvent.type(screen.getByLabelText("List item 1 value for skills"), "Python");
    await userEvent.click(screen.getByRole("button", { name: "Add list item for skills" }));
    await userEvent.type(screen.getByLabelText("List item 2 value for skills"), "Rust");
    await userEvent.click(screen.getByRole("button", { name: "Save corrections" }));

    expect(await screen.findByText("Requirements saved.")).toBeInTheDocument();
    expect(savedBody).toEqual({
      user_corrected: {
        location: 3,
        compensation: { currency: "INR", bands: [10, 20] },
        skills: ["Python", "Rust"]
      }
    });
  });

  test("edits and renames mixed typed lists without losing commas or unknown values", async () => {
    const record = application("app_1", "Swiggy");
    let requirements: RequirementsRecord = {
      raw_ai: {},
      deterministic: {},
      user_corrected: {
        mixed: [1, true, null, "a,b"],
        opaque_key: { nested: ["leave", 7] }
      },
      uncertainty: { compensation: null }
    };
    let savedBody: unknown;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/applications")) return jsonResponse([record]);
        if (url.endsWith("/applications/app_1/packet")) return jsonResponse(packet(record));
        if (url.endsWith("/ai/providers")) return jsonResponse([]);
        if (url.endsWith("/applications/app_1/requirements") && init?.method === "PUT") {
          savedBody = JSON.parse(String(init.body));
          requirements = { ...requirements, ...(savedBody as { user_corrected: Record<string, JsonValue> }) };
          return jsonResponse(requirements);
        }
        if (url.endsWith("/applications/app_1/requirements")) return jsonResponse(requirements);
        throw new Error(`Unhandled request: ${url}`);
      })
    );
    render(<App />);

    await userEvent.click(await screen.findByRole("tab", { name: "Requirements" }));
    const key = await screen.findByLabelText("Correction key 1");
    await userEvent.clear(key);
    await userEvent.type(key, "renamed_mixed");
    expect(screen.getByLabelText("List item 1 kind for renamed_mixed")).toHaveValue("number");
    expect(screen.getByLabelText("List item 2 kind for renamed_mixed")).toHaveValue("boolean");
    expect(screen.getByLabelText("List item 3 kind for renamed_mixed")).toHaveValue("unknown");
    expect(screen.getByLabelText("List item 4 value for renamed_mixed")).toHaveValue("a,b");
    await userEvent.clear(screen.getByLabelText("List item 1 value for renamed_mixed"));
    await userEvent.type(screen.getByLabelText("List item 1 value for renamed_mixed"), "2");
    await userEvent.selectOptions(screen.getByLabelText("List item 2 value for renamed_mixed"), "no");
    await userEvent.type(screen.getByLabelText("List item 4 value for renamed_mixed"), " updated");
    await userEvent.click(screen.getByRole("button", { name: "Save corrections" }));

    expect(await screen.findByText("Requirements saved.")).toBeInTheDocument();
    expect(savedBody).toEqual({
      user_corrected: {
        renamed_mixed: [2, false, null, "a,b updated"],
        opaque_key: { nested: ["leave", 7] }
      }
    });
  });

  test("evidence, links, and profile facts preserve explicit privacy and verification", async () => {
    const record = application("app_1", "Swiggy");
    const posted: Array<{ url: string; body: unknown }> = [];
    let packetLoads = 0;
    const nullableEvidence: EvidenceRecord = {
      id: "evd_null_source",
      claim_text: "Public portfolio unavailable",
      evidence_type: "project",
      source_url: null,
      source_sha256: null,
      verification_state: "needs_verification",
      privacy_level: "private",
      publishable: false,
      notes: "",
      created_at: "",
      updated_at: ""
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/applications")) return jsonResponse([record]);
        if (url.endsWith("/applications/app_1/packet")) {
          packetLoads += 1;
          return jsonResponse(packet(record));
        }
        if (url.endsWith("/ai/providers")) return jsonResponse([]);
        if (url.endsWith("/applications/app_1/evidence") && init?.method === "POST") {
          const body = JSON.parse(String(init.body));
          posted.push({ url, body });
          return jsonResponse({ id: body.evidence_id ?? `evd_${posted.length}`, ...body }, true, body.evidence_id ? 200 : 201);
        }
        if (url.endsWith("/applications/app_1/evidence")) return jsonResponse([nullableEvidence]);
        if (url.endsWith("/profile-facts") && init?.method === "POST") {
          const body = JSON.parse(String(init.body));
          posted.push({ url, body });
          return jsonResponse({ id: "fact_1", ...body }, true, 201);
        }
        if (url.endsWith("/profile-facts")) return jsonResponse([]);
        throw new Error(`Unhandled request: ${url}`);
      })
    );
    render(<App />);

    await userEvent.click(await screen.findByRole("tab", { name: "Evidence" }));
    await userEvent.type(await screen.findByLabelText("Evidence claim"), "Built a production service");
    await userEvent.type(screen.getByLabelText("Evidence type"), "project");
    await userEvent.selectOptions(screen.getByLabelText("Evidence verification"), "verified");
    await userEvent.selectOptions(screen.getByLabelText("Evidence privacy"), "sensitive");
    await userEvent.click(screen.getByRole("button", { name: "Add evidence" }));

    await userEvent.type(screen.getByLabelText("Evidence ID"), "evd_existing");
    await userEvent.click(screen.getByRole("button", { name: "Link evidence" }));

    await userEvent.type(screen.getByLabelText("Fact key"), "skills.python");
    await userEvent.type(screen.getByLabelText("Fact value"), "Advanced");
    await userEvent.selectOptions(screen.getByLabelText("Fact verification"), "user_stated");
    await userEvent.selectOptions(screen.getByLabelText("Fact privacy"), "private");
    await userEvent.click(screen.getByRole("button", { name: "Add profile fact" }));

    expect(posted.map((item) => item.body)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ claim_text: "Built a production service", verification_state: "verified", privacy_level: "sensitive" }),
        { evidence_id: "evd_existing" },
        expect.objectContaining({ fact_key: "skills.python", fact_value: "Advanced", verification_state: "user_stated", privacy_level: "private" })
      ])
    );
    expect(screen.getByText("Public portfolio unavailable")).toBeInTheDocument();
    expect(packetLoads).toBeGreaterThanOrEqual(4);
  });

  test("downloads exports through the API client and revokes the object URL", async () => {
    const createObjectURL = vi.fn(() => "blob:careerops-export");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });
    const clickedDownloads: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clickedDownloads.push(this.download);
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(new Blob(["job description"]), {
          status: 200,
          headers: {
            "Content-Disposition": 'attachment; filename="swiggy-jd.txt"',
            "Content-Type": "text/plain"
          }
        })
      )
    );

    render(
      <ReviewPanel
        applicationId="app_1"
        resume={null}
        report={null}
        approval={null}
        approvalEligibility={{ eligible: false, reason: "Run AI review before approval.", ai_task_id: null }}
        submitted={false}
        busy=""
        error=""
        onApprove={vi.fn()}
        onSubmitted={vi.fn()}
      />
    );
    await userEvent.click(screen.getByRole("button", { name: "Export JD" }));

    await waitFor(() => expect(clickedDownloads).toEqual(["swiggy-jd.txt"]));
    expect(createObjectURL).toHaveBeenCalledWith(expect.any(Blob));
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:careerops-export");
  });

  test("shows a safe in-app error when a stale export fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ detail: "Approval artifact is stale." }), {
          status: 409,
          statusText: "Conflict",
          headers: { "Content-Type": "application/json" }
        })
      )
    );
    render(
      <ReviewPanel
        applicationId="app_1"
        resume={null}
        report={null}
        approval={null}
        approvalEligibility={{ eligible: false, reason: "Run AI review before approval.", ai_task_id: null }}
        submitted={false}
        busy=""
        error=""
        onApprove={vi.fn()}
        onSubmitted={vi.fn()}
      />
    );

    await userEvent.click(screen.getByRole("button", { name: "Export JD" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Approval artifact is stale.");
  });

  test("renders deterministic warning objects and AI warning strings without blank rows", () => {
    const report: ReportRecord = {
      id: "rpt_1",
      application_id: "app_1",
      resume_version_id: "res_1",
      jd_snapshot_id: "jd_1",
      ai_task_id: "ait_1",
      pdf_sha256: "pdf",
      blocking_issue_count: 0,
      warning_count: 2,
      fit: {},
      risks: {
        blockers: [],
        warnings: [
          { severity: "warning", label: "Keyword gap", source: "resume", why: "Kubernetes is absent", next_action: "Add it only with evidence" },
          "AI review: clarify ownership of the migration"
        ]
      },
      created_at: ""
    };
    render(
      <ReviewPanel
        applicationId="app_1"
        resume={null}
        report={report}
        approval={null}
        approvalEligibility={{ eligible: true, reason: "AI review is current and complete.", ai_task_id: "ait_1" }}
        submitted={false}
        busy=""
        error=""
        onApprove={vi.fn()}
        onSubmitted={vi.fn()}
      />
    );

    const warningList = screen.getByRole("list");
    expect(within(warningList).getAllByRole("listitem")).toHaveLength(2);
    expect(within(warningList).getByText("Keyword gap")).toBeInTheDocument();
    expect(within(warningList).getByText("Kubernetes is absent")).toBeInTheDocument();
    expect(within(warningList).getByText("Add it only with evidence")).toBeInTheDocument();
    expect(within(warningList).getByText("AI review: clarify ownership of the migration")).toBeInTheDocument();
  });

  test.each([
    ["approved", "not_submitted"],
    ["submitted_manual", "submitted_manual"]
  ])("keeps AI and finding mutations read-only for %s applications", async (status, submissionState) => {
    const record = {
      ...application("app_1", "Swiggy"),
      status,
      submission_state: submissionState,
      approval_id: status === "approved" ? "apr_1" : null
    };
    const completedTask = {
      id: "ait_1",
      application_id: "app_1",
      task_type: "review_claims" as const,
      provider_config_id: "provider_1",
      prompt_template_id: "claims_v1",
      prompt_sha256: "checksum",
      input_artifact_ids: [],
      status: "completed" as const,
      privacy_mode: "local_private",
      started_at: "",
      completed_at: "",
      error_summary: "",
      created_at: "",
      updated_at: ""
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/applications")) return jsonResponse([record]);
        if (url.endsWith("/applications/app_1/packet")) return jsonResponse(packet(record, { latest_ai_task: completedTask }));
        if (url.endsWith("/ai/providers")) return jsonResponse([]);
        if (url.endsWith("/ai-tasks/ait_1/findings")) {
          return jsonResponse([{ id: "aif_1", task_id: "ait_1", finding_type: "claim_review_decision", severity: "warning", claim_text: "Supported claim", evidence_item_id: "evd_1", recommendation: "Keep", verifier_state: "verified", decision: "pending", payload: {}, created_at: "", updated_at: "" }]);
        }
        throw new Error(`Unhandled request: ${url}`);
      })
    );
    render(<App />);

    expect(await screen.findByRole("button", { name: "Run AI review" })).toBeDisabled();
    expect(await screen.findByRole("button", { name: "Accept" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
  });

  test("ignores an older application list that resolves after post-create refresh", async () => {
    const stale = application("app_old", "Old Co");
    const created = application("app_new", "New Co");
    let resolveInitial!: (value: ReturnType<typeof jsonResponse>) => void;
    const initial = new Promise<ReturnType<typeof jsonResponse>>((resolve) => {
      resolveInitial = resolve;
    });
    let listCalls = 0;
    let initialSignal: AbortSignal | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/applications") && init?.method === "POST") return jsonResponse(created, true, 201);
        if (url.endsWith("/applications")) {
          listCalls += 1;
          if (listCalls === 1) {
            initialSignal = init?.signal ?? undefined;
            return initial;
          }
          return jsonResponse([created]);
        }
        if (url.endsWith("/applications/app_new/packet")) return jsonResponse(packet(created));
        if (url.endsWith("/applications/app_old/packet")) return jsonResponse(packet(stale));
        if (url.endsWith("/ai/providers")) return jsonResponse([]);
        throw new Error(`Unhandled request: ${url}`);
      })
    );
    render(<App />);

    await userEvent.click(screen.getByRole("button", { name: "Add application" }));
    await userEvent.type(screen.getByLabelText("Company"), "New Co");
    await userEvent.type(screen.getByLabelText("Role title"), "Engineer");
    await userEvent.type(screen.getByLabelText("JD text"), "Build systems");
    await userEvent.click(screen.getByRole("button", { name: "Save application" }));
    expect(await screen.findByRole("heading", { name: "New Co" })).toBeInTheDocument();
    expect(initialSignal?.aborted).toBe(true);

    await act(async () => resolveInitial(jsonResponse([stale])));
    expect(screen.getByRole("heading", { name: "New Co" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Old Co/ })).not.toBeInTheDocument();
  });

  test("reports post-create list refresh failure separately from create failure", async () => {
    const created = application("app_new", "New Co");
    let listCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/applications") && init?.method === "POST") return jsonResponse(created, true, 201);
        if (url.endsWith("/applications")) {
          listCalls += 1;
          if (listCalls === 1) return jsonResponse([]);
          throw "offline";
        }
        if (url.endsWith("/applications/app_new/packet")) return jsonResponse(packet(created));
        if (url.endsWith("/ai/providers")) return jsonResponse([]);
        throw new Error(`Unhandled request: ${url}`);
      })
    );
    render(<App />);

    await screen.findByText("No applications yet. Add a JD to start a packet.");
    await userEvent.click(screen.getByRole("button", { name: "Add application" }));
    await userEvent.type(screen.getByLabelText("Company"), "New Co");
    await userEvent.type(screen.getByLabelText("Role title"), "Engineer");
    await userEvent.type(screen.getByLabelText("JD text"), "Build systems");
    await userEvent.click(screen.getByRole("button", { name: "Save application" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Application saved, but applications could not be refreshed.");
    expect(screen.queryByText("Save application failed.")).not.toBeInTheDocument();
    expect(screen.getAllByRole("alert")).toHaveLength(1);
  });

  test("a stale packet response cannot replace the current application", async () => {
    const first = application("app_1", "First Co");
    const second = application("app_2", "Second Co");
    let resolveFirst!: (value: ReturnType<typeof jsonResponse>) => void;
    const delayedFirst = new Promise<ReturnType<typeof jsonResponse>>((resolve) => {
      resolveFirst = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/applications")) return jsonResponse([first, second]);
        if (url.endsWith("/ai/providers")) return jsonResponse([]);
        if (url.endsWith("/applications/app_1/packet")) return delayedFirst;
        if (url.endsWith("/applications/app_2/packet")) return jsonResponse(packet(second, { selected_resume: { id: "res_2", name: "Second resume", role_family: "general", tex_sha256: "", pdf_sha256: null, parent_resume_version_id: null, render_status: "not_rendered", parse_status: "not_parsed", page_count: null, created_at: "", updated_at: "" } }));
        throw new Error(`Unhandled request: ${url}`);
      })
    );
    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: /Second Co/ }));
    expect(await screen.findByRole("heading", { name: "Second Co" })).toBeInTheDocument();
    expect(screen.getByText("Second resume")).toBeInTheDocument();
    await act(async () => {
      resolveFirst(jsonResponse(packet(first, { selected_resume: { id: "res_1", name: "Stale resume", role_family: "general", tex_sha256: "", pdf_sha256: null, parent_resume_version_id: null, render_status: "not_rendered", parse_status: "not_parsed", page_count: null, created_at: "", updated_at: "" } })));
    });

    expect(screen.getByRole("heading", { name: "Second Co" })).toBeInTheDocument();
    expect(screen.queryByText("Stale resume")).not.toBeInTheDocument();
  });

  test("an immediate application switch never renders the previous packet for the new selection", async () => {
    const first = application("app_1", "First Co");
    const second = application("app_2", "Second Co");
    let resolveSecond!: (value: ReturnType<typeof jsonResponse>) => void;
    const delayedSecond = new Promise<ReturnType<typeof jsonResponse>>((resolve) => {
      resolveSecond = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/applications/app_1/packet")) return jsonResponse(packet(first));
        if (url.endsWith("/applications/app_2/packet")) return delayedSecond;
        throw new Error(`Unhandled request: ${url}`);
      })
    );
    const renders: Array<{ selectedId: string; packetId: string | null }> = [];

    function PacketProbe({ selectedId }: { selectedId: string }) {
      const { packet: selectedPacket } = useApplicationPacket(selectedId);
      renders.push({
        selectedId,
        packetId: selectedPacket?.application.id ?? null
      });
      return <div>{selectedPacket?.application.company ?? "Loading packet"}</div>;
    }

    const view = render(<PacketProbe selectedId={first.id} />);
    expect(await screen.findByText("First Co")).toBeInTheDocument();
    renders.length = 0;

    view.rerender(<PacketProbe selectedId={second.id} />);

    expect(renders).not.toContainEqual({ selectedId: second.id, packetId: first.id });
    expect(screen.getByText("Loading packet")).toBeInTheDocument();
    await act(async () => {
      resolveSecond(jsonResponse(packet(second)));
    });
    expect(await screen.findByText("Second Co")).toBeInTheDocument();
  });

  test("a completed mutation cannot reload the application selected when it started", async () => {
    const first = application("app_1", "First Co");
    const second = application("app_2", "Second Co");
    let resolveUpload!: (value: ReturnType<typeof jsonResponse>) => void;
    const delayedUpload = new Promise<ReturnType<typeof jsonResponse>>((resolve) => {
      resolveUpload = resolve;
    });
    let firstPacketLoads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/applications")) return jsonResponse([first, second]);
        if (url.endsWith("/ai/providers")) return jsonResponse([]);
        if (url.endsWith("/applications/app_1/resume")) return delayedUpload;
        if (url.endsWith("/applications/app_1/packet")) {
          firstPacketLoads += 1;
          return jsonResponse(packet(first, {
            selected_resume: firstPacketLoads > 1
              ? { id: "res_stale", name: "Stale uploaded resume", role_family: "general", tex_sha256: "", pdf_sha256: null, parent_resume_version_id: null, render_status: "not_rendered", parse_status: "not_parsed", page_count: null, created_at: "", updated_at: "" }
              : null
          }));
        }
        if (url.endsWith("/applications/app_2/packet")) return jsonResponse(packet(second));
        throw new Error(`Unhandled request: ${url}`);
      })
    );
    render(<App />);

    await screen.findByRole("heading", { name: "First Co" });
    await userEvent.upload(screen.getByLabelText("LaTeX file"), new File(["resume"], "resume.tex", { type: "text/plain" }));
    await userEvent.click(screen.getByRole("button", { name: "Upload resume" }));
    await userEvent.click(screen.getByRole("button", { name: /Second Co/ }));
    expect(await screen.findByRole("heading", { name: "Second Co" })).toBeInTheDocument();

    await act(async () => {
      resolveUpload(jsonResponse({ id: "res_stale" }, true, 201));
    });

    await waitFor(() => expect(screen.getByRole("heading", { name: "Second Co" })).toBeInTheDocument());
    expect(screen.queryByText("Stale uploaded resume")).not.toBeInTheDocument();
  });

  test("shows packet errors and keeps unavailable actions disabled", async () => {
    const record = application("app_1", "Swiggy");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/applications")) return jsonResponse([record]);
        if (url.endsWith("/ai/providers")) return jsonResponse([]);
        if (url.endsWith("/applications/app_1/packet")) return jsonResponse({ detail: "Application packet is invalid" }, false, 409, "Conflict");
        throw new Error(`Unhandled request: ${url}`);
      })
    );
    render(<App />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Application packet is invalid");
    expect(screen.getByRole("button", { name: "Approve packet" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Mark submitted manually" })).toBeDisabled();
  });

  test("status labels use operational wording and banned marketing copy is absent", async () => {
    installDefaultFetch();
    render(<App />);

    await screen.findByText("No applications yet. Add a JD to start a packet.");
    expect(statusLabel("draft")).toBe("Draft");
    expect(statusLabel("needs_evidence")).toBe("Needs evidence");
    expect(statusLabel("needs_resume_update")).toBe("Needs resume update");
    expect(statusLabel("ready_for_review")).toBe("Ready to review");
    expect(statusLabel("approved")).toBe("Approved");
    expect(statusLabel("submitted_manual")).toBe("Submitted manually");
    expect(statusLabel("archived")).toBe("Archived");
    expect(screen.queryByText(/unlock opportunities|supercharge|ai-powered optimization|seamless/i)).not.toBeInTheDocument();
  });
});
