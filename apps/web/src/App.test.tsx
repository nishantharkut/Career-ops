import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";

import App, { statusLabel } from "./App";
import type { ApplicationPacket, ApplicationRecord, EvidenceRecord, JsonValue, RequirementsRecord } from "./api";
import ReviewPanel from "./components/ReviewPanel";

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

const packet = (record: ApplicationRecord, overrides: Partial<ApplicationPacket> = {}): ApplicationPacket => ({
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
  evidence: [],
  versions: [],
  ...overrides
});

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

  test("preserves upload, render, analyze, approve, and manual submission flow", async () => {
    let record = application("app_1", "Swiggy");
    let currentPacket = packet(record);
    const uploaded = { id: "res_1", name: "Application resume", role_family: "general", tex_sha256: "tex", pdf_sha256: null, parent_resume_version_id: null, render_status: "not_rendered", parse_status: "not_parsed", page_count: null, created_at: "", updated_at: "" };
    const rendered = { ...uploaded, pdf_sha256: "pdf", render_status: "rendered", parse_status: "parsed", page_count: 1 };
    const report = { id: "rpt_1", application_id: "app_1", resume_version_id: "res_1", jd_snapshot_id: "jd_app_1", blocking_issue_count: 0, warning_count: 0, fit: { matched_requirements: [], missing_requirements: [], exact_keyword_gaps: [], summary_score: 100 }, risks: { blockers: [], warnings: [] }, created_at: "" };
    const approval = { id: "apr_1", application_id: "app_1", resume_version_id: "res_1", report_id: "rpt_1", pdf_sha256: "pdf", approved_at: "", approval_note: "Reviewed PDF and report" };
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
      if (url.endsWith("/applications/app_1/approve")) {
        record = { ...record, status: "approved", approval_id: "apr_1" };
        currentPacket = packet(record, { selected_resume: rendered, latest_report: report, approval });
        return jsonResponse(approval);
      }
      if (url.endsWith("/applications/app_1/mark-submitted")) {
        record = { ...record, status: "submitted_manual", submission_state: "submitted_manual" };
        currentPacket = packet(record, { selected_resume: rendered, latest_report: report, approval });
        return jsonResponse(record);
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
    await waitFor(() => expect(screen.getByRole("button", { name: "Analyze" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Analyze" }));
    expect((await screen.findAllByText("Ready to review")).length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: "Approve packet" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Mark submitted manually" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Mark submitted manually" }));
    expect(await screen.findAllByText("Submitted manually")).not.toHaveLength(0);
    expect(screen.getByRole("button", { name: "Mark submitted manually" })).toBeDisabled();
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
    expect(screen.getByRole("alert")).toHaveTextContent("Enter at least one list item for skills.");

    await userEvent.type(screen.getByLabelText("Correction value for skills"), "Python, Rust");
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
