import { useState } from "react";
import { Check, FileDown, ShieldCheck } from "lucide-react";

import {
  ApprovalEligibilityRecord,
  ApprovalRecord,
  DownloadedArtifact,
  ReportRecord,
  ResumeRecord,
  RiskIssue,
  downloadApproval,
  downloadJd,
  downloadReport,
  downloadResumePdf
} from "../api";

type ReviewPanelProps = {
  applicationId: string | null;
  resume: ResumeRecord | null;
  report: ReportRecord | null;
  approval: ApprovalRecord | null;
  approvalEligibility: ApprovalEligibilityRecord;
  submitted: boolean;
  busy: string;
  error: string;
  onApprove: () => Promise<void> | void;
  onSubmitted: () => Promise<void> | void;
};

type DownloadClient = (applicationId: string) => Promise<DownloadedArtifact>;

function issueHasContent(issue: RiskIssue | string): boolean {
  if (typeof issue === "string") return issue.trim().length > 0;
  return [issue.label, issue.why, issue.next_action].some((value) => value.trim().length > 0);
}

function ReportIssue({ issue, index, kind }: { issue: RiskIssue | string; index: number; kind: string }) {
  if (typeof issue === "string") {
    return <li key={`${kind}-${index}`}><span>{issue}</span></li>;
  }
  return (
    <li key={`${issue.label}-${issue.source}-${index}`}>
      {issue.label && <strong>{issue.label}</strong>}
      {issue.why && <span>{issue.why}</span>}
      {issue.next_action && <span>{issue.next_action}</span>}
    </li>
  );
}

function ExportAction({
  available,
  busy,
  label,
  onClick
}: {
  available: boolean;
  busy: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={!available || busy}
      aria-label={available ? label : `${label} unavailable`}
      onClick={onClick}
    >
      <FileDown aria-hidden="true" size={16} /> {label}
    </button>
  );
}

export default function ReviewPanel({
  applicationId,
  resume,
  report,
  approval,
  approvalEligibility,
  submitted,
  busy,
  error,
  onApprove,
  onSubmitted
}: ReviewPanelProps) {
  const [exportBusy, setExportBusy] = useState("");
  const [exportError, setExportError] = useState("");
  const hasCleanReport = Boolean(report && report.blocking_issue_count === 0);
  const canApprove = hasCleanReport && approvalEligibility.eligible;
  const blockers = report?.risks.blockers.filter(issueHasContent) ?? [];
  const warnings = report?.risks.warnings.filter(issueHasContent) ?? [];
  const score = report?.fit.score ?? report?.fit.summary_score;
  const matches = report?.fit.matches ?? report?.fit.matched_requirements ?? [];
  const gaps = report?.fit.gaps ?? report?.fit.missing_requirements ?? [];
  const recommendations = report?.fit.recommendations ?? [];

  async function exportArtifact(label: string, download: DownloadClient) {
    if (!applicationId || exportBusy) return;
    setExportBusy(label);
    setExportError("");
    let objectUrl = "";
    try {
      const artifact = await download(applicationId);
      objectUrl = URL.createObjectURL(artifact.blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = artifact.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
    } catch (downloadError) {
      setExportError(downloadError instanceof Error ? downloadError.message : `${label} could not be downloaded.`);
    } finally {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      setExportBusy("");
    }
  }

  return (
    <section className="panel" aria-label="Checks and approval">
      <h2>Checks</h2>
      {busy && <p className="notice" role="status">{busy}</p>}
      {error && <p className="error" role="alert">{error}</p>}
      {exportError && <p className="error" role="alert">{exportError}</p>}
      {!applicationId && <p className="muted">Select an application to review checks.</p>}
      {applicationId && !report && <p className="muted">Run AI review after rendering the selected resume.</p>}
      {report && (
        <div className="report">
          <p><strong>{report.blocking_issue_count}</strong> blockers</p>
          <p><strong>{report.warning_count}</strong> warnings</p>
          {score !== undefined && (
            <dl className="meta">
              <div><dt>Fit score</dt><dd>{score}</dd></div>
            </dl>
          )}
          {matches.length > 0 && (
            <section aria-label="Fit matches">
              <h3>Matches</h3>
              <ul className="issue-list">{matches.map((match, index) => <li key={`match-${index}`}><span>{match}</span></li>)}</ul>
            </section>
          )}
          {gaps.length > 0 && (
            <section aria-label="Fit gaps">
              <h3>Gaps</h3>
              <ul className="issue-list">{gaps.map((gap, index) => <li key={`gap-${index}`}><span>{gap}</span></li>)}</ul>
            </section>
          )}
          {blockers.length > 0 && (
            <section aria-label="Fit blockers">
              <h3>Blockers</h3>
            <ul className="issue-list">
                {blockers.map((issue, index) => <ReportIssue issue={issue} index={index} kind="blocker" key={`blocker-${index}`} />)}
            </ul>
            </section>
          )}
          {warnings.length > 0 && (
            <section aria-label="Fit warnings">
              <h3>Warnings</h3>
            <ul className="issue-list">
                {warnings.map((issue, index) => <ReportIssue issue={issue} index={index} kind="warning" key={`warning-${index}`} />)}
            </ul>
            </section>
          )}
          {recommendations.length > 0 && (
            <section aria-label="Fit recommendations">
              <h3>Recommendations</h3>
              <ul className="issue-list">{recommendations.map((item, index) => <li key={`recommendation-${index}`}><span>{item}</span></li>)}</ul>
            </section>
          )}
          {canApprove && <p className="notice"><ShieldCheck aria-hidden="true" size={16} /> Ready to review</p>}
        </div>
      )}
      {applicationId && !approvalEligibility.eligible && (
        <p className="notice" role="status">{approvalEligibility.reason}</p>
      )}

      <section aria-label="Exports">
        <h3>Exports</h3>
        <div className="actions actions--stacked">
          <ExportAction
            available={Boolean(applicationId)}
            busy={exportBusy !== ""}
            label="Export JD"
            onClick={() => void exportArtifact("Export JD", downloadJd)}
          />
          <ExportAction
            available={Boolean(applicationId && resume?.pdf_sha256)}
            busy={exportBusy !== ""}
            label="Export resume PDF"
            onClick={() => void exportArtifact("Export resume PDF", downloadResumePdf)}
          />
          <ExportAction
            available={Boolean(applicationId && report)}
            busy={exportBusy !== ""}
            label="Export report"
            onClick={() => void exportArtifact("Export report", downloadReport)}
          />
          <ExportAction
            available={Boolean(applicationId && approval)}
            busy={exportBusy !== ""}
            label="Export approval"
            onClick={() => void exportArtifact("Export approval", downloadApproval)}
          />
        </div>
      </section>

      <div className="actions actions--stacked">
        <button
          type="button"
          disabled={!canApprove || Boolean(approval) || busy !== ""}
          onClick={() => void onApprove()}
        >
          <Check aria-hidden="true" size={16} /> Approve packet
        </button>
        <button
          type="button"
          disabled={!approval || submitted || busy !== ""}
          onClick={() => void onSubmitted()}
        >
          Mark submitted manually
        </button>
      </div>
    </section>
  );
}
