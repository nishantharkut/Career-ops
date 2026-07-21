import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import AiTasksPanel from "./components/AiTasksPanel";
import type { AiProviderRecord, AiTaskRecord } from "./api";

function jsonResponse(body: unknown, ok = true, status = 200, statusText = "OK") {
  return { ok, status, statusText, json: async () => body };
}

const provider = (overrides: Partial<AiProviderRecord>): AiProviderRecord => ({
  id: "provider_1",
  provider_kind: "codex",
  display_name: "Codex codex",
  identity_name: "codex",
  enabled: true,
  privacy_mode: "cloud_external",
  endpoint: "",
  model: "gpt-5.6-sol",
  available: true,
  availability_detail: "available",
  is_ai: true,
  data_leaves_machine: true,
  current_metadata: null,
  created_at: "",
  updated_at: "",
  ...overrides
});

const task = (overrides: Partial<AiTaskRecord> = {}): AiTaskRecord => ({
  id: "ait_1",
  application_id: "app_1",
  task_type: "analyze_fit",
  provider_config_id: "provider_1",
  prompt_template_id: "fit_v1",
  prompt_sha256: "prompt-checksum",
  input_artifact_ids: [],
  status: "completed",
  privacy_mode: "cloud_external",
  started_at: "",
  completed_at: "",
  error_summary: "",
  created_at: "",
  updated_at: "",
  ...overrides
});

describe("AI tasks panel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  test("prefers codex2, shows exact identities and privacy copy, and gates each cloud run", async () => {
    const providers = [
      provider({ id: "provider_default", identity_name: "codex", display_name: "Codex codex" }),
      provider({ id: "provider_account2", identity_name: "codex2", display_name: "Codex codex2" })
    ];
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(providers)));

    render(<AiTasksPanel applicationId="app_1" initialTask={null} onPacketChanged={vi.fn()} />);

    const select = await screen.findByLabelText("Provider");
    expect(select).toHaveValue("provider_account2");
    expect(screen.getByRole("option", { name: "Codex account 2 (codex2)" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Codex default (codex)" })).toBeInTheDocument();
    expect(screen.getByText("Selected task data is sent through your signed-in Codex account")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run AI review" })).toBeDisabled();

    await userEvent.selectOptions(screen.getByLabelText("Task"), "suggest_resume_edits");
    expect(screen.getByText("Creates suggestions, not final files")).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Acknowledge Codex data transfer for this run"));
    expect(screen.getByRole("button", { name: "Suggest resume edits" })).toBeEnabled();
  });

  test("requires a fresh acknowledgement for every cloud run", async () => {
    let createCount = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/ai/providers")) {
        return jsonResponse([provider({ id: "provider_account2", identity_name: "codex2" })]);
      }
      if (url.endsWith("/applications/app_1/ai-tasks")) {
        createCount += 1;
        return jsonResponse(task({ id: `ait_${createCount}`, task_type: "suggest_resume_edits" }), true, 202);
      }
      if (/\/ai-tasks\/ait_\d+\/findings$/.test(url)) return jsonResponse([]);
      throw new Error(`Unhandled request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AiTasksPanel applicationId="app_1" initialTask={null} onPacketChanged={vi.fn()} />);
    await userEvent.selectOptions(await screen.findByLabelText("Task"), "suggest_resume_edits");
    const acknowledgement = screen.getByLabelText("Acknowledge Codex data transfer for this run");
    await userEvent.click(acknowledgement);
    await userEvent.click(screen.getByRole("button", { name: "Suggest resume edits" }));

    await waitFor(() => expect(createCount).toBe(1));
    expect(acknowledgement).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Suggest resume edits" })).toBeDisabled();
    await userEvent.click(acknowledgement);
    await userEvent.click(screen.getByRole("button", { name: "Suggest resume edits" }));
    await waitFor(() => expect(createCount).toBe(2));
  });

  test("uses verifier language for a deterministic non-AI provider", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse([
          provider({
            id: "provider_deterministic",
            provider_kind: "deterministic",
            display_name: "Deterministic verifier",
            identity_name: "",
            privacy_mode: "verifier_only",
            model: "deterministic-v1",
            data_leaves_machine: false,
            is_ai: false
          })
        ])
      )
    );

    render(<AiTasksPanel applicationId="app_1" initialTask={null} onPacketChanged={vi.fn()} />);

    const runButton = await screen.findByRole("button", { name: "Run verifier" });
    expect(runButton).toBeEnabled();
    for (const option of ["analyze_jd", "suggest_resume_edits", "draft_cover_letter", "review_claims"]) {
      await userEvent.selectOptions(screen.getByLabelText("Task"), option);
      expect(screen.getByRole("button", { name: "Run verifier" })).toBeEnabled();
      expect(screen.queryByRole("button", { name: "Run AI review" })).not.toBeInTheDocument();
    }
  });

  test("uses a command label specific to each AI task", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse([provider({ privacy_mode: "local_private", data_leaves_machine: false })]))
    );
    render(<AiTasksPanel applicationId="app_1" initialTask={null} onPacketChanged={vi.fn()} />);

    expect(await screen.findByRole("button", { name: "Run AI review" })).toBeEnabled();
    const choices = [
      ["analyze_jd", "Analyze JD"],
      ["suggest_resume_edits", "Suggest resume edits"],
      ["draft_cover_letter", "Draft cover letter"],
      ["review_claims", "Review claims"]
    ];
    for (const [value, label] of choices) {
      await userEvent.selectOptions(screen.getByLabelText("Task"), value);
      expect(screen.getByRole("button", { name: label })).toBeEnabled();
    }
    expect(screen.getByRole("option", { name: "Analyze JD" })).toBeInTheDocument();
  });

  test("polls every 1.5 seconds until terminal and never retries another provider", async () => {
    const selected = provider({ id: "provider_selected", privacy_mode: "local_private", data_leaves_machine: false });
    const fallback = provider({ id: "provider_fallback", identity_name: "fallback", privacy_mode: "local_private", data_leaves_machine: false });
    let gets = 0;
    const posts: unknown[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/ai/providers")) return jsonResponse([selected, fallback]);
      if (url.endsWith("/applications/app_1/ai-tasks")) {
        posts.push(JSON.parse(String(init?.body)));
        return jsonResponse(task({ provider_config_id: "provider_selected", status: "pending", completed_at: null }), true, 202);
      }
      if (url.endsWith("/ai-tasks/ait_1")) {
        gets += 1;
        return jsonResponse(task({ provider_config_id: "provider_selected", status: gets === 1 ? "running" : "completed" }));
      }
      if (url.endsWith("/ai-tasks/ait_1/findings")) return jsonResponse([]);
      throw new Error(`Unhandled request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const onPacketChanged = vi.fn();
    render(<AiTasksPanel applicationId="app_1" initialTask={null} onPacketChanged={onPacketChanged} />);

    const runButton = await screen.findByRole("button", { name: "Run AI review" });
    await waitFor(() => expect(runButton).toBeEnabled());
    vi.useFakeTimers();
    fireEvent.click(runButton);
    await act(async () => Promise.resolve());
    expect(runButton).toBeDisabled();
    expect(onPacketChanged).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
      await vi.advanceTimersByTimeAsync(1500);
    });

    expect(gets).toBe(2);
    expect(posts).toEqual([expect.objectContaining({ provider_config_id: "provider_selected" })]);
    expect(onPacketChanged).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4500);
    });
    expect(gets).toBe(2);
  });

  test("retries a transient poll failure, clears the error, and stops at terminal", async () => {
    let gets = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/ai/providers")) {
        return jsonResponse([provider({ privacy_mode: "local_private", data_leaves_machine: false })]);
      }
      if (url.endsWith("/applications/app_1/ai-tasks")) {
        return jsonResponse(task({ status: "pending", completed_at: null }), true, 202);
      }
      if (url.endsWith("/ai-tasks/ait_1")) {
        gets += 1;
        if (gets === 1) throw new Error("Temporary task status failure");
        return jsonResponse(task({ status: "completed" }));
      }
      if (url.endsWith("/ai-tasks/ait_1/findings")) return jsonResponse([]);
      throw new Error(`Unhandled request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<AiTasksPanel applicationId="app_1" initialTask={null} onPacketChanged={vi.fn()} />);

    const runButton = await screen.findByRole("button", { name: "Run AI review" });
    vi.useFakeTimers();
    fireEvent.click(runButton);
    await act(async () => Promise.resolve());
    await act(async () => vi.advanceTimersByTimeAsync(1500));
    expect(screen.getByRole("alert")).toHaveTextContent("Temporary task status failure");
    await act(async () => vi.advanceTimersByTimeAsync(1500));
    expect(gets).toBe(2);
    expect(screen.queryByText("Temporary task status failure")).not.toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(gets).toBe(2);
    expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/applications/app_1/ai-tasks"))).toHaveLength(1);
  });

  test("cancels scheduled polling when unmounted", async () => {
    let gets = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/ai/providers")) {
          return jsonResponse([provider({ privacy_mode: "local_private", data_leaves_machine: false })]);
        }
        if (url.endsWith("/applications/app_1/ai-tasks")) {
          return jsonResponse(task({ status: "pending", completed_at: null }), true, 202);
        }
        if (url.endsWith("/ai-tasks/ait_1")) {
          gets += 1;
          return jsonResponse(task({ status: "running", completed_at: null }));
        }
        throw new Error(`Unhandled request: ${url}`);
      })
    );
    const view = render(<AiTasksPanel applicationId="app_1" initialTask={null} onPacketChanged={vi.fn()} />);
    const runButton = await screen.findByRole("button", { name: "Run AI review" });
    vi.useFakeTimers();
    fireEvent.click(runButton);
    await act(async () => Promise.resolve());
    view.unmount();
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(gets).toBe(0);
  });

  test("offers decisions for verified claim findings without making them draft eligible", async () => {
    let decision = "pending";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/ai/providers")) return jsonResponse([]);
        if (url.endsWith("/ai-tasks/ait_1/findings")) {
          return jsonResponse([
            { id: "aif_claim", task_id: "ait_1", finding_type: "claim_review_decision", severity: "warning", claim_text: "Claim is supported", evidence_item_id: "evd_1", recommendation: "Keep the claim", verifier_state: "verified", decision, payload: {}, created_at: "", updated_at: "" },
            { id: "aif_unsafe", task_id: "ait_1", finding_type: "claim_review_decision", severity: "blocking", claim_text: "Unsupported claim", evidence_item_id: null, recommendation: "Remove it", verifier_state: "blocked", decision: "pending", payload: {}, created_at: "", updated_at: "" }
          ]);
        }
        if (url.endsWith("/ai-findings/aif_claim/decision")) {
          decision = "accepted";
          return jsonResponse({ id: "aif_claim", task_id: "ait_1", finding_type: "claim_review_decision", severity: "warning", claim_text: "Claim is supported", evidence_item_id: "evd_1", recommendation: "Keep the claim", verifier_state: "verified", decision, payload: {}, created_at: "", updated_at: "" });
        }
        throw new Error(`Unhandled request: ${url}`);
      })
    );

    render(<AiTasksPanel applicationId="app_1" initialTask={task({ task_type: "review_claims" })} onPacketChanged={vi.fn()} />);
    expect(await screen.findByRole("button", { name: "Accept" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
    expect(screen.getAllByText("Blocked by verifier")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Create resume draft" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    expect(await screen.findByText("Decision: accepted")).toBeInTheDocument();
  });

  test("accepts one safe suggestion, blocks unsafe findings, and creates a child draft", async () => {
    const completed = task({ task_type: "suggest_resume_edits", status: "completed", provider_config_id: "provider_selected" });
    let safeDecision = "pending";
    const draftBodies: unknown[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/ai/providers")) return jsonResponse([provider({ id: "provider_selected", privacy_mode: "local_private", data_leaves_machine: false })]);
      if (url.endsWith("/ai-tasks/ait_1/findings")) {
        return jsonResponse([
          { id: "aif_safe", task_id: "ait_1", finding_type: "resume_edit_suggestion", severity: "info", claim_text: "Use evidence-backed wording", evidence_item_id: null, recommendation: "Replace the sentence", verifier_state: "verified", decision: safeDecision, payload: { source_text: "Old", replacement_text: "New", reason: "Clearer", evidence_refs: [] }, created_at: "", updated_at: "" },
          { id: "aif_blocked", task_id: "ait_1", finding_type: "resume_edit_suggestion", severity: "blocking", claim_text: "Invent a metric", evidence_item_id: null, recommendation: "Add 90%", verifier_state: "blocked", decision: "pending", payload: {}, created_at: "", updated_at: "" }
        ]);
      }
      if (url.endsWith("/ai-findings/aif_safe/decision")) {
        safeDecision = "accepted";
        return jsonResponse({ id: "aif_safe", task_id: "ait_1", finding_type: "resume_edit_suggestion", severity: "info", claim_text: "Use evidence-backed wording", evidence_item_id: null, recommendation: "Replace the sentence", verifier_state: "verified", decision: "accepted", payload: { source_text: "Old", replacement_text: "New", reason: "Clearer", evidence_refs: [] }, created_at: "", updated_at: "" });
      }
      if (url.endsWith("/applications/app_1/resume-drafts")) {
        draftBodies.push(JSON.parse(String(init?.body)));
        return jsonResponse({ id: "res_child", name: "Tailored draft", parent_resume_version_id: "res_parent", render_status: "not_rendered" }, true, 201);
      }
      throw new Error(`Unhandled request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const onPacketChanged = vi.fn();
    render(<AiTasksPanel applicationId="app_1" initialTask={completed} onPacketChanged={onPacketChanged} />);

    expect((await screen.findAllByText("Blocked by verifier")).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Create resume draft" })).toBeDisabled();
    expect(screen.getAllByRole("button", { name: "Accept suggestion" })).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Accept suggestion" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Create resume draft" })).toBeEnabled());
    expect(onPacketChanged).toHaveBeenCalledTimes(1);
    await userEvent.clear(screen.getByLabelText("Draft name"));
    await userEvent.type(screen.getByLabelText("Draft name"), "Tailored draft");
    await userEvent.click(screen.getByRole("button", { name: "Create resume draft" }));

    expect(await screen.findByText("Draft created. Render it before review.")).toBeInTheDocument();
    expect(draftBodies).toEqual([{ task_id: "ait_1", finding_ids: ["aif_safe"], name: "Tailored draft" }]);
    expect(onPacketChanged).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: "Reject suggestion" })).not.toBeInTheDocument();
  });

  test("makes unavailable, blocked, and failed states explicit", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/ai/providers")) return jsonResponse([provider({ available: false, availability_detail: "runtime_unavailable" })]);
        if (url.endsWith("/ai-tasks/ait_1/findings")) return jsonResponse([]);
        throw new Error(`Unhandled request: ${url}`);
      })
    );

    const { rerender } = render(<AiTasksPanel applicationId="app_1" initialTask={task({ status: "blocked", error_summary: "verification_blocked" })} onPacketChanged={vi.fn()} />);

    expect(await screen.findByText(/Provider unavailable: runtime_unavailable/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run AI review" })).toBeDisabled();
    expect(screen.getByText("Blocked")).toBeInTheDocument();
    rerender(<AiTasksPanel applicationId="app_1" initialTask={task({ status: "failed", error_summary: "provider_unavailable" })} onPacketChanged={vi.fn()} />);
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("provider_unavailable")).toBeInTheDocument();
  });
});
