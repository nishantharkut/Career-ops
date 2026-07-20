import { FormEvent, useEffect, useMemo, useState } from "react";

import {
  ApplicationRecord,
  ApprovalRecord,
  ReportRecord,
  ResumeRecord,
  analyzeApplication,
  approveApplication,
  createApplication,
  listApplications,
  markSubmitted,
  renderResume,
  uploadResume
} from "./api";
import "./styles.css";

const STATUS_LABELS: Record<string, string> = {
  draft: "Draft",
  analyzing: "Analyzing",
  needs_evidence: "Needs evidence",
  needs_resume_update: "Needs resume update",
  ready_for_review: "Ready to review",
  approved: "Approved",
  submitted_manual: "Submitted manually",
  archived: "Archived"
};

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

type ApplicationForm = {
  company: string;
  role_title: string;
  location: string;
  source_url: string;
  jd_text: string;
};

const emptyForm: ApplicationForm = {
  company: "",
  role_title: "",
  location: "",
  source_url: "",
  jd_text: ""
};

export default function App() {
  const [applications, setApplications] = useState<ApplicationRecord[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<ApplicationForm>(emptyForm);
  const [formError, setFormError] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [resume, setResume] = useState<ResumeRecord | null>(null);
  const [resumeFile, setResumeFile] = useState<File | null>(null);
  const [resumeName, setResumeName] = useState("Application resume");
  const [roleFamily, setRoleFamily] = useState("general");
  const [report, setReport] = useState<ReportRecord | null>(null);
  const [approval, setApproval] = useState<ApprovalRecord | null>(null);

  useEffect(() => {
    listApplications()
      .then((records) => {
        setApplications(records);
        setSelectedId(records[0]?.id ?? null);
      })
      .catch((loadError: Error) => setError(loadError.message));
  }, []);

  const selected = useMemo(
    () => applications.find((application) => application.id === selectedId) ?? null,
    [applications, selectedId]
  );

  function clearPacketState() {
    setResume(null);
    setResumeFile(null);
    setReport(null);
    setApproval(null);
    setError("");
  }

  function selectApplication(applicationId: string) {
    setSelectedId(applicationId);
    setShowForm(false);
    clearPacketState();
  }

  async function submitApplication(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError("");
    setError("");
    if (!form.company.trim() || !form.role_title.trim() || !form.jd_text.trim()) {
      setFormError("Company, role title, and JD text are required.");
      return;
    }
    setBusy("Saving application");
    try {
      const created = await createApplication({
        ...form,
        location: form.location.trim() || "Unknown"
      });
      setApplications((current) => [created, ...current]);
      selectApplication(created.id);
      setShowForm(false);
      setForm(emptyForm);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Save application failed.");
    } finally {
      setBusy("");
    }
  }

  async function submitResume(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected || !resumeFile) {
      setError("Choose a LaTeX file before upload.");
      return;
    }
    setBusy("Uploading resume");
    setError("");
    try {
      const uploaded = await uploadResume(selected.id, {
        name: resumeName,
        role_family: roleFamily,
        file: resumeFile
      });
      setResume(uploaded);
      setReport(null);
      setApproval(null);
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : "Resume upload failed.");
    } finally {
      setBusy("");
    }
  }

  async function handleRender() {
    if (!resume) return;
    setBusy("Rendering PDF");
    setError("");
    try {
      const rendered = await renderResume(resume.id);
      setResume(rendered);
      if (rendered.render_error) setError(rendered.render_error);
    } catch (renderError) {
      setError(renderError instanceof Error ? renderError.message : "PDF render failed. Open the render log.");
    } finally {
      setBusy("");
    }
  }

  async function handleAnalyze() {
    if (!selected) return;
    setBusy("Analyzing");
    setError("");
    try {
      setReport(await analyzeApplication(selected.id));
      setApproval(null);
    } catch (analyzeError) {
      setError(analyzeError instanceof Error ? analyzeError.message : "Analyze failed.");
    } finally {
      setBusy("");
    }
  }

  async function handleApprove() {
    if (!selected || !report) return;
    setBusy("Approving");
    setError("");
    try {
      const approved = await approveApplication(selected.id, report.id, "Reviewed PDF and report");
      setApproval(approved);
      setApplications((current) =>
        current.map((application) =>
          application.id === selected.id ? { ...application, status: "approved", approval_id: approved.id } : application
        )
      );
    } catch (approvalError) {
      setError(approvalError instanceof Error ? approvalError.message : "Approval blocked.");
    } finally {
      setBusy("");
    }
  }

  async function handleSubmitted() {
    if (!selected) return;
    setBusy("Marking submitted");
    setError("");
    try {
      const submitted = await markSubmitted(selected.id, "Submitted manually");
      setApplications((current) => current.map((application) => (application.id === submitted.id ? submitted : application)));
      setSelectedId(submitted.id);
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : "Manual submission record failed.");
    } finally {
      setBusy("");
    }
  }

  return (
    <main className="app-shell" aria-busy={busy !== ""}>
      <aside className="rail" aria-label="Applications">
        <div className="rail__header">
          <h1>CareerOps</h1>
          <button type="button" onClick={() => setShowForm(true)}>
            Add application
          </button>
        </div>
        {applications.length === 0 ? (
          <p className="empty">No applications yet. Add a JD to start a packet.</p>
        ) : (
          <ul className="application-list">
            {applications.map((application) => (
              <li key={application.id}>
                <button
                  type="button"
                  className={application.id === selectedId ? "application-list__item is-selected" : "application-list__item"}
                  onClick={() => selectApplication(application.id)}
                >
                  <strong>{application.company}</strong>
                  <span>{application.role_title}</span>
                  <small>{statusLabel(application.status)}</small>
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      <section className="workspace" aria-label="Application detail">
        {showForm && (
          <form className="panel" onSubmit={submitApplication}>
            <h2>Add application</h2>
            {formError && (
              <p className="error" role="alert">
                {formError}
              </p>
            )}
            <label>
              Company
              <input aria-required="true" value={form.company} onChange={(event) => setForm({ ...form, company: event.target.value })} />
            </label>
            <label>
              Role title
              <input aria-required="true" value={form.role_title} onChange={(event) => setForm({ ...form, role_title: event.target.value })} />
            </label>
            <label>
              Location
              <input value={form.location} placeholder="Bangalore" onChange={(event) => setForm({ ...form, location: event.target.value })} />
            </label>
            <label>
              Source URL
              <input value={form.source_url} placeholder="https://example.com/role" onChange={(event) => setForm({ ...form, source_url: event.target.value })} />
            </label>
            <label>
              JD text
              <textarea aria-required="true" value={form.jd_text} onChange={(event) => setForm({ ...form, jd_text: event.target.value })} />
            </label>
            <div className="actions">
              <button type="submit" disabled={busy !== ""}>
                Save application
              </button>
              <button type="button" className="secondary" onClick={() => setShowForm(false)}>
                Cancel
              </button>
            </div>
          </form>
        )}

        {!showForm && selected && (
          <div className="panel">
            <div className="detail-head">
              <div>
                <h2>{selected.company}</h2>
                <p>{selected.role_title}</p>
              </div>
              <span className="status">{statusLabel(selected.status)}</span>
            </div>
            <dl className="meta">
              <div>
                <dt>Location</dt>
                <dd>{selected.location || "Unknown"}</dd>
              </div>
              <div>
                <dt>Resume</dt>
                <dd>{resume?.name ?? "No resume selected"}</dd>
              </div>
              <div>
                <dt>Render</dt>
                <dd>{resume?.render_status ?? "Not rendered"}</dd>
              </div>
            </dl>

            <form className="resume-form" onSubmit={submitResume}>
              <h3>Resume version</h3>
              <label>
                Resume name
                <input value={resumeName} onChange={(event) => setResumeName(event.target.value)} />
              </label>
              <label>
                Role family
                <input value={roleFamily} onChange={(event) => setRoleFamily(event.target.value)} />
              </label>
              <label>
                LaTeX file
                <input type="file" accept=".tex" onChange={(event) => setResumeFile(event.target.files?.[0] ?? null)} />
              </label>
              <div className="actions">
                <button type="submit" disabled={busy !== ""}>
                  Upload resume
                </button>
                <button type="button" disabled={!resume || busy !== ""} onClick={handleRender}>
                  Render PDF
                </button>
                <button type="button" disabled={!resume?.pdf_sha256 || busy !== ""} onClick={handleAnalyze}>
                  Analyze
                </button>
              </div>
            </form>
          </div>
        )}
      </section>

      <aside className="checks" aria-label="Checks and approval">
        <section className="panel">
          <h2>Checks</h2>
          {busy && (
            <p className="notice" role="status">
              {busy}
            </p>
          )}
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          {!selected && <p className="muted">Select an application to review checks.</p>}
          {report && (
            <div className="report">
              <p>
                <strong>{report.blocking_issue_count}</strong> blockers
              </p>
              <p>
                <strong>{report.warning_count}</strong> warnings
              </p>
              {report.risks.blockers.length > 0 && (
                <ul className="issue-list">
                  {report.risks.blockers.map((issue) => (
                    <li key={`${issue.label}-${issue.source}`}>
                      <strong>{issue.label}</strong>
                      <span>{issue.next_action}</span>
                    </li>
                  ))}
                </ul>
              )}
              {report.blocking_issue_count === 0 && <p className="notice">Ready to review</p>}
            </div>
          )}
          <div className="actions actions--stacked">
            <button type="button" disabled={!report || report.blocking_issue_count > 0 || busy !== ""} onClick={handleApprove}>
              Approve packet
            </button>
            <button type="button" disabled={!approval || busy !== ""} onClick={handleSubmitted}>
              Mark submitted manually
            </button>
          </div>
        </section>
      </aside>
    </main>
  );
}
