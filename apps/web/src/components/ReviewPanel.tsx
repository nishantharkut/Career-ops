import { useState } from "react";
import { Check, FileDown, ShieldCheck } from "lucide-react";

import {
  ApprovalRecord,
  DownloadedArtifact,
  ReportRecord,
  ResumeRecord,
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
  submitted: boolean;
  busy: string;
  error: string;
  onApprove: () => Promise<void> | void;
  onSubmitted: () => Promise<void> | void;
};

type DownloadClient = (applicationId: string) => Promise<DownloadedArtifact>;

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
  submitted,
  busy,
  error,
  onApprove,
  onSubmitted
}: ReviewPanelProps) {
  const [exportBusy, setExportBusy] = useState("");
  const [exportError, setExportError] = useState("");
  const hasCleanReport = Boolean(report && report.blocking_issue_count === 0);

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
