import { Check, FileDown, ShieldCheck } from "lucide-react";

import {
  ApprovalRecord,
  ReportRecord,
  ResumeRecord,
  approvalDownloadUrl,
  jdDownloadUrl,
  reportDownloadUrl,
  resumePdfDownloadUrl
} from "../api";

type ReviewPanelProps = {
  applicationId: string | null;
  resume: ResumeRecord | null;
  report: ReportRecord | null;
  approval: ApprovalRecord | null;
  submitted: boolean;
  busy: string;
  error: string;
  onApprove: () => Promise<void> | void;
  onSubmitted: () => Promise<void> | void;
};

function ExportAction({ href, available, label }: { href: string; available: boolean; label: string }) {
  if (!available) {
    return (
      <button type="button" disabled aria-label={`${label} unavailable`}>
        <FileDown aria-hidden="true" size={16} /> {label}
      </button>
    );
  }
  return (
    <a href={href} download>
      <FileDown aria-hidden="true" size={16} /> {label}
    </a>
  );
}

export default function ReviewPanel({
  applicationId,
  resume,
  report,
  approval,
  submitted,
  busy,
  error,
  onApprove,
  onSubmitted
}: ReviewPanelProps) {
  const hasCleanReport = Boolean(report && report.blocking_issue_count === 0);

  return (
    <section className="panel" aria-label="Checks and approval">
      <h2>Checks</h2>
      {busy && <p className="notice" role="status">{busy}</p>}
      {error && <p className="error" role="alert">{error}</p>}
      {!applicationId && <p className="muted">Select an application to review checks.</p>}
      {applicationId && !report && <p className="muted">Run Analyze after rendering the selected resume.</p>}
      {report && (
        <div className="report">
          <p><strong>{report.blocking_issue_count}</strong> blockers</p>
          <p><strong>{report.warning_count}</strong> warnings</p>
          {report.risks.blockers.length > 0 && (
            <ul className="issue-list">
              {report.risks.blockers.map((issue, index) => (
                <li key={`${issue.label}-${issue.source}-${index}`}>
                  <strong>{issue.label}</strong>
                  <span>{issue.why}</span>
                  <span>{issue.next_action}</span>
                </li>
              ))}
            </ul>
          )}
          {report.risks.warnings.length > 0 && (
            <ul className="issue-list">
              {report.risks.warnings.map((issue, index) => (
                <li key={`${issue.label}-${issue.source}-${index}`}>
                  <strong>{issue.label}</strong>
                  <span>{issue.next_action}</span>
                </li>
              ))}
            </ul>
          )}
          {hasCleanReport && <p className="notice"><ShieldCheck aria-hidden="true" size={16} /> Ready to review</p>}
        </div>
      )}

      <section aria-label="Exports">
        <h3>Exports</h3>
        <div className="actions actions--stacked">
          <ExportAction
            href={applicationId ? jdDownloadUrl(applicationId) : ""}
            available={Boolean(applicationId)}
            label="Export JD"
          />
          <ExportAction
            href={applicationId ? resumePdfDownloadUrl(applicationId) : ""}
            available={Boolean(applicationId && resume?.pdf_sha256)}
            label="Export resume PDF"
          />
          <ExportAction
            href={applicationId ? reportDownloadUrl(applicationId) : ""}
            available={Boolean(applicationId && report)}
            label="Export report"
          />
          <ExportAction
            href={applicationId ? approvalDownloadUrl(applicationId) : ""}
            available={Boolean(applicationId && approval)}
            label="Export approval"
          />
        </div>
      </section>

      <div className="actions actions--stacked">
        <button
          type="button"
          disabled={!hasCleanReport || Boolean(approval) || busy !== ""}
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
