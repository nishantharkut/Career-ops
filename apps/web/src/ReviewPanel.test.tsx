import { render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import ReviewPanel from "./components/ReviewPanel";
import type { ReportRecord } from "./api";

describe("review panel", () => {
  test("shows the complete AI fit report", () => {
    const report: ReportRecord = {
      id: "rpt_1",
      application_id: "app_1",
      resume_version_id: "res_1",
      jd_snapshot_id: "jd_1",
      ai_task_id: "ait_1",
      pdf_sha256: "a".repeat(64),
      blocking_issue_count: 1,
      warning_count: 1,
      fit: {
        score: 55,
        matches: ["109 automated tests"],
        gaps: ["Graduation window mismatch"],
        recommendations: ["Confirm eligibility before applying"]
      },
      risks: {
        blockers: ["Candidate is currently enrolled"],
        warnings: ["Location flexibility is unclear"]
      },
      created_at: ""
    };

    render(
      <ReviewPanel
        applicationId="app_1"
        resume={null}
        report={report}
        approval={null}
        approvalEligibility={{ eligible: false, reason: "Blocking risks remain", ai_task_id: "ait_1" }}
        submitted={false}
        busy=""
        error=""
        onApprove={vi.fn()}
        onSubmitted={vi.fn()}
      />
    );

    expect(screen.getByText("55")).toBeInTheDocument();
    expect(screen.getByText("109 automated tests")).toBeInTheDocument();
    expect(screen.getByText("Graduation window mismatch")).toBeInTheDocument();
    expect(screen.getByText("Candidate is currently enrolled")).toBeInTheDocument();
    expect(screen.getByText("Location flexibility is unclear")).toBeInTheDocument();
    expect(screen.getByText("Confirm eligibility before applying")).toBeInTheDocument();
  });
});
