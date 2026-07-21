import { FormEvent, KeyboardEvent, useState } from "react";
import { FilePlus2, Play, RefreshCw } from "lucide-react";

import { ApplicationPacket } from "../api";
import EvidencePanel from "./EvidencePanel";
import RequirementsEditor from "./RequirementsEditor";

type WorkspaceTab = "resume" | "requirements" | "evidence";

type PacketWorkspaceProps = {
  packet: ApplicationPacket;
  busy: string;
  statusLabel: (status: string) => string;
  onUpload: (payload: { name: string; roleFamily: string; file: File }) => Promise<void> | void;
  onRender: () => Promise<void> | void;
  onAnalyze: () => Promise<void> | void;
  onMutated: () => Promise<void> | void;
};

const TABS: Array<{ id: WorkspaceTab; label: string }> = [
  { id: "resume", label: "Resume" },
  { id: "requirements", label: "Requirements" },
  { id: "evidence", label: "Evidence" }
];

export default function PacketWorkspace({
  packet,
  busy,
  statusLabel,
  onUpload,
  onRender,
  onAnalyze,
  onMutated
}: PacketWorkspaceProps) {
  const [tab, setTab] = useState<WorkspaceTab>("resume");
  const [resumeFile, setResumeFile] = useState<File | null>(null);
  const [resumeName, setResumeName] = useState("Application resume");
  const [roleFamily, setRoleFamily] = useState("general");
  const [formError, setFormError] = useState("");
  const application = packet.application;
  const resume = packet.selected_resume;
  const readOnly =
    Boolean(packet.approval) ||
    application.status === "approved" ||
    application.submission_state !== "not_submitted";

  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError("");
    if (!resumeFile) {
      setFormError("Choose a LaTeX file before upload.");
      return;
    }
    await onUpload({ name: resumeName.trim() || "Application resume", roleFamily: roleFamily.trim() || "general", file: resumeFile });
  }

  function handleTabKey(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    if (!(["ArrowLeft", "ArrowRight", "Home", "End"] as string[]).includes(event.key)) return;
    event.preventDefault();
    let nextIndex = index;
    if (event.key === "ArrowLeft") nextIndex = (index - 1 + TABS.length) % TABS.length;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % TABS.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = TABS.length - 1;
    setTab(TABS[nextIndex].id);
    document.getElementById(`tab-${TABS[nextIndex].id}`)?.focus();
  }

  return (
    <div className="panel">
      <div className="detail-head">
        <div>
          <h2>{application.company}</h2>
          <p>{application.role_title}</p>
        </div>
        <span className="status">{statusLabel(application.status)}</span>
      </div>
      <dl className="meta">
        <div><dt>Location</dt><dd>{application.location || "Unknown"}</dd></div>
        <div><dt>Resume</dt><dd>{resume?.name ?? "No resume selected"}</dd></div>
        <div><dt>Render</dt><dd>{resume?.render_status ?? "Not rendered"}</dd></div>
      </dl>

      <div role="tablist" aria-label="Application packet">
        {TABS.map((item, index) => (
          <button
            id={`tab-${item.id}`}
            key={item.id}
            type="button"
            role="tab"
            aria-selected={tab === item.id}
            aria-controls={`panel-${item.id}`}
            tabIndex={tab === item.id ? 0 : -1}
            onClick={() => setTab(item.id)}
            onKeyDown={(event) => handleTabKey(event, index)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {tab === "resume" && (
        <section id="panel-resume" role="tabpanel" aria-labelledby="tab-resume">
          <h3>Resume version</h3>
          {resume && (
            <dl className="meta">
              <div><dt>PDF</dt><dd>{resume.pdf_sha256 ? "Available" : "Unavailable"}</dd></div>
              <div><dt>Pages</dt><dd>{resume.page_count ?? "Unknown"}</dd></div>
            </dl>
          )}
          {formError && <p className="error" role="alert">{formError}</p>}
          <form className="resume-form" onSubmit={upload}>
            <label>Resume name<input value={resumeName} disabled={readOnly} onChange={(event) => setResumeName(event.target.value)} /></label>
            <label>Role family<input value={roleFamily} disabled={readOnly} onChange={(event) => setRoleFamily(event.target.value)} /></label>
            <label>LaTeX file<input type="file" accept=".tex" disabled={readOnly} onChange={(event) => setResumeFile(event.target.files?.[0] ?? null)} /></label>
            <div className="actions">
              <button type="submit" disabled={readOnly || busy !== ""}>
                <FilePlus2 aria-hidden="true" size={16} /> Upload resume
              </button>
              <button type="button" disabled={readOnly || !resume || busy !== ""} onClick={() => void onRender()}>
                <RefreshCw aria-hidden="true" size={16} /> Render PDF
              </button>
              <button type="button" disabled={readOnly || !resume?.pdf_sha256 || busy !== ""} onClick={() => void onAnalyze()}>
                <Play aria-hidden="true" size={16} /> Analyze
              </button>
            </div>
          </form>
        </section>
      )}

      {tab === "requirements" && (
        <div id="panel-requirements" role="tabpanel" aria-labelledby="tab-requirements">
          <RequirementsEditor applicationId={application.id} readOnly={readOnly} onMutated={onMutated} />
        </div>
      )}

      {tab === "evidence" && (
        <div id="panel-evidence" role="tabpanel" aria-labelledby="tab-evidence">
          <EvidencePanel applicationId={application.id} readOnly={readOnly} onMutated={onMutated} />
        </div>
      )}

      <details>
        <summary>History</summary>
        <p>
          Resume {resume?.id ?? "not selected"}
          {resume?.parent_resume_version_id ? ` / Parent ${resume.parent_resume_version_id}` : " / No parent"}
        </p>
        {packet.versions.length === 0 ? (
          <p className="muted">No packet versions recorded.</p>
        ) : (
          <ol>
            {packet.versions.map((version) => (
              <li key={version.id}>
                <strong>Version {version.version_number}</strong> {version.event_type}
              </li>
            ))}
          </ol>
        )}
      </details>
    </div>
  );
}
