import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";

import App, { statusLabel } from "./App";

const emptyFetch = vi.fn(async () => ({
  ok: true,
  json: async () => []
}));

describe("Application Desk UI", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.stubGlobal("fetch", emptyFetch);
  });

  test("shows the approved empty state and add action", async () => {
    render(<App />);

    expect(await screen.findByText("No applications yet. Add a JD to start a packet.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add application" })).toBeInTheDocument();
  });

  test("validates required application fields", async () => {
    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: "Add application" }));
    await userEvent.click(screen.getByRole("button", { name: "Save application" }));

    expect(screen.getByText("Company, role title, and JD text are required.")).toBeInTheDocument();
  });

  test("renders existing application with approved status wording", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => [
          {
            id: "app_1",
            company: "Swiggy",
            role_title: "Data Science Intern",
            location: "Bangalore",
            status: "ready_for_review",
            selected_resume_version_id: null,
            latest_report_id: null,
            approval_id: null,
            submission_state: "not_submitted"
          }
        ]
      }))
    );

    render(<App />);

    expect(await screen.findAllByText("Swiggy")).toHaveLength(2);
    expect(screen.getAllByText("Ready to review")).toHaveLength(2);
  });

  test("clears packet-local state when another application is selected", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/applications/app_1/resume")) {
          return {
            ok: true,
            json: async () => ({
              id: "resume_1",
              name: "App one resume",
              role_family: "general",
              tex_path: "sample_resume.tex",
              tex_sha256: "abc",
              pdf_path: null,
              pdf_sha256: null,
              parent_resume_version_id: null,
              render_status: "not_rendered",
              parse_status: "not_parsed",
              page_count: null,
              created_at: "2026-07-21T00:00:00Z",
              updated_at: "2026-07-21T00:00:00Z"
            })
          };
        }
        return {
          ok: true,
          json: async () => [
            {
              id: "app_1",
              company: "Swiggy",
              role_title: "Data Science Intern",
              location: "Bangalore",
              status: "draft",
              selected_resume_version_id: null,
              latest_report_id: null,
              approval_id: null,
              submission_state: "not_submitted"
            },
            {
              id: "app_2",
              company: "Goldman Sachs",
              role_title: "Engineering Analyst",
              location: "Bangalore",
              status: "draft",
              selected_resume_version_id: null,
              latest_report_id: null,
              approval_id: null,
              submission_state: "not_submitted"
            }
          ]
        };
      })
    );

    render(<App />);

    await screen.findByRole("button", { name: /Swiggy/ });
    await userEvent.upload(screen.getByLabelText("LaTeX file"), new File(["resume"], "resume.tex", { type: "text/plain" }));
    await userEvent.click(screen.getByRole("button", { name: "Upload resume" }));
    expect(await screen.findByText("App one resume")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Goldman Sachs/ }));

    expect(screen.getByText("No resume selected")).toBeInTheDocument();
    expect(screen.queryByText("App one resume")).not.toBeInTheDocument();
  });

  test("status labels use the required operational wording", () => {
    expect(statusLabel("draft")).toBe("Draft");
    expect(statusLabel("needs_evidence")).toBe("Needs evidence");
    expect(statusLabel("needs_resume_update")).toBe("Needs resume update");
    expect(statusLabel("ready_for_review")).toBe("Ready to review");
    expect(statusLabel("approved")).toBe("Approved");
    expect(statusLabel("submitted_manual")).toBe("Submitted manually");
    expect(statusLabel("archived")).toBe("Archived");
  });

  test("does not render banned marketing copy", async () => {
    render(<App />);

    await screen.findByText("No applications yet. Add a JD to start a packet.");
    expect(screen.queryByText(/unlock opportunities/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/supercharge/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/ai-powered optimization/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/seamless/i)).not.toBeInTheDocument();
  });
});
