import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApplicationRecord,
  analyzeApplication,
  approveApplication,
  createApplication,
  listApplications,
  markSubmitted,
  renderResume,
  uploadResume
} from "./api";
import AiTasksPanel from "./components/AiTasksPanel";
import ApplicationRail from "./components/ApplicationRail";
import PacketWorkspace from "./components/PacketWorkspace";
import ReviewPanel from "./components/ReviewPanel";
import { useApplicationPacket } from "./hooks/useApplicationPacket";
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

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export default function App() {
  const [applications, setApplications] = useState<ApplicationRecord[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<ApplicationForm>(emptyForm);
  const [formError, setFormError] = useState("");
  const [busy, setBusy] = useState("");
  const [actionError, setActionError] = useState("");
  const [listError, setListError] = useState("");
  const listController = useRef<AbortController | null>(null);
  const listGeneration = useRef(0);
  const { packet, loading: packetLoading, error: packetError, reload: reloadPacket } = useApplicationPacket(selectedId);

  const loadApplications = useCallback(async (failureMessage: string) => {
    listController.current?.abort();
    const controller = new AbortController();
    const requestGeneration = ++listGeneration.current;
    listController.current = controller;
    try {
      const records = await listApplications(controller.signal);
      if (requestGeneration === listGeneration.current && !controller.signal.aborted) {
        setApplications(records);
        setListError("");
        setSelectedId((current) => {
          if (current && records.some((application) => application.id === current)) return current;
          return records[0]?.id ?? null;
        });
        return records;
      }
    } catch (loadError) {
      if (requestGeneration === listGeneration.current && !controller.signal.aborted) {
        setListError(errorMessage(loadError, failureMessage));
      }
    }
    return null;
  }, []);

  useEffect(() => {
    void loadApplications("Applications could not be loaded.");
    return () => {
      listGeneration.current += 1;
      listController.current?.abort();
    };
  }, [loadApplications]);

  const selected = useMemo(
    () => applications.find((application) => application.id === selectedId) ?? null,
    [applications, selectedId]
  );

  const refreshData = useCallback(async () => {
    await Promise.allSettled([
      loadApplications("Applications could not be refreshed."),
      reloadPacket()
    ]);
  }, [loadApplications, reloadPacket]);

  function selectApplication(applicationId: string) {
    setSelectedId(applicationId);
    setShowForm(false);
    setActionError("");
  }

  async function submitApplication(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError("");
    setActionError("");
    if (!form.company.trim() || !form.role_title.trim() || !form.jd_text.trim()) {
      setFormError("Company, role title, and JD text are required.");
      return;
    }
    setBusy("Saving application");
    let created: ApplicationRecord;
    try {
      created = await createApplication({
        company: form.company.trim(),
        role_title: form.role_title.trim(),
        location: form.location.trim() || "Unknown",
        source_url: form.source_url.trim(),
        jd_text: form.jd_text.trim()
      });
    } catch (saveError) {
      setActionError(errorMessage(saveError, "Save application failed."));
      setBusy("");
      return;
    }

    setApplications((current) => [created, ...current.filter((item) => item.id !== created.id)]);
    setSelectedId(created.id);
    setShowForm(false);
    setForm(emptyForm);
    try {
      await loadApplications("Application saved, but applications could not be refreshed.");
    } finally {
      setBusy("");
    }
  }

  async function handleUpload(payload: { name: string; roleFamily: string; file: File }) {
    if (!selectedId) return;
    setBusy("Uploading resume");
    setActionError("");
    try {
      await uploadResume(selectedId, {
        name: payload.name,
        role_family: payload.roleFamily,
        file: payload.file
      });
      await refreshData();
    } catch (uploadError) {
      setActionError(errorMessage(uploadError, "Resume upload failed."));
    } finally {
      setBusy("");
    }
  }

  async function handleRender() {
    if (!packet?.selected_resume) return;
    setBusy("Rendering PDF");
    setActionError("");
    try {
      const rendered = await renderResume(packet.selected_resume.id);
      if (rendered.render_error) setActionError(rendered.render_error);
      await refreshData();
    } catch (renderError) {
      setActionError(errorMessage(renderError, "PDF render failed."));
    } finally {
      setBusy("");
    }
  }

  async function handleAnalyze() {
    if (!selectedId) return;
    setBusy("Running verifier");
    setActionError("");
    try {
      await analyzeApplication(selectedId);
      await refreshData();
    } catch (analyzeError) {
      setActionError(errorMessage(analyzeError, "Verifier failed."));
    } finally {
      setBusy("");
    }
  }

  async function handleApprove() {
    if (!selectedId || !packet?.latest_report) return;
    setBusy("Approving");
    setActionError("");
    try {
      await approveApplication(selectedId, packet.latest_report.id, "Reviewed PDF and report");
      await refreshData();
    } catch (approvalError) {
      setActionError(errorMessage(approvalError, "Approval blocked."));
    } finally {
      setBusy("");
    }
  }

  async function handleSubmitted() {
    if (!selectedId || !packet?.approval) return;
    setBusy("Marking submitted");
    setActionError("");
    try {
      await markSubmitted(selectedId, "Submitted manually");
      await refreshData();
    } catch (submitError) {
      setActionError(errorMessage(submitError, "Manual submission record failed."));
    } finally {
      setBusy("");
    }
  }

  const reviewError = actionError || packetError || listError;
  const packetReadOnly = Boolean(
    packet && (
      packet.approval ||
      packet.application.status === "approved" ||
      packet.application.status === "submitted_manual" ||
      packet.application.submission_state !== "not_submitted"
    )
  );

  return (
    <main className="app-shell" aria-busy={busy !== ""}>
      <ApplicationRail
        applications={applications}
        selectedId={selectedId}
        onAdd={() => {
          setShowForm(true);
          setFormError("");
        }}
        onSelect={selectApplication}
        statusLabel={statusLabel}
      />

      <section className="workspace" aria-label="Application and resume">
        {showForm && (
          <form className="panel" onSubmit={submitApplication}>
            <h2>Add application</h2>
            {formError && <p className="error" role="alert">{formError}</p>}
            <label>Company<input aria-required="true" value={form.company} onChange={(event) => setForm({ ...form, company: event.target.value })} /></label>
            <label>Role title<input aria-required="true" value={form.role_title} onChange={(event) => setForm({ ...form, role_title: event.target.value })} /></label>
            <label>Location<input value={form.location} placeholder="Bangalore" onChange={(event) => setForm({ ...form, location: event.target.value })} /></label>
            <label>Source URL<input value={form.source_url} placeholder="https://example.com/role" onChange={(event) => setForm({ ...form, source_url: event.target.value })} /></label>
            <label>JD text<textarea aria-required="true" value={form.jd_text} onChange={(event) => setForm({ ...form, jd_text: event.target.value })} /></label>
            <div className="actions">
              <button type="submit" disabled={busy !== ""}>Save application</button>
              <button type="button" className="secondary" onClick={() => setShowForm(false)}>Cancel</button>
            </div>
          </form>
        )}
        {!showForm && packet && (
          <PacketWorkspace
            packet={packet}
            busy={busy}
            statusLabel={statusLabel}
            onUpload={handleUpload}
            onRender={handleRender}
            onAnalyze={handleAnalyze}
            onMutated={refreshData}
          />
        )}
        {!showForm && selected && packetLoading && <p role="status">Loading application packet</p>}
        {!showForm && !selected && applications.length > 0 && <p className="muted">Select an application.</p>}
      </section>

      <aside className="checks" aria-label="AI review and approval">
        {selectedId && packet && (
          <AiTasksPanel
            applicationId={selectedId}
            initialTask={packet.latest_ai_task}
            readOnly={packetReadOnly}
            onPacketChanged={refreshData}
          />
        )}
        <ReviewPanel
          applicationId={selectedId}
          resume={packet?.selected_resume ?? null}
          report={packet?.latest_report ?? null}
          approval={packet?.approval ?? null}
          approvalEligibility={packet?.approval_eligibility ?? {
            eligible: false,
            reason: "Run AI review for the current JD and resume before approval.",
            ai_task_id: null
          }}
          submitted={Boolean(packet && packet.application.submission_state !== "not_submitted")}
          busy={busy}
          error={reviewError}
          onApprove={handleApprove}
          onSubmitted={handleSubmitted}
        />
      </aside>
    </main>
  );
}
