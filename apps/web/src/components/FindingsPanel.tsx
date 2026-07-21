import { FormEvent, useState } from "react";
import { Check, FilePlus2, ShieldCheck, X } from "lucide-react";

import {
  AiFindingRecord,
  AiTaskRecord,
  createResumeDraft,
  decideAiFinding
} from "../api";

type FindingsPanelProps = {
  applicationId: string;
  task: AiTaskRecord;
  findings: AiFindingRecord[];
  onFindingUpdated: (finding: AiFindingRecord) => void;
  onPacketChanged: () => Promise<void> | void;
};

function isSafeSuggestion(finding: AiFindingRecord): boolean {
  return finding.finding_type === "resume_edit_suggestion" && finding.verifier_state === "verified";
}

function payloadText(finding: AiFindingRecord, key: string): string {
  const value = finding.payload[key];
  return typeof value === "string" ? value : "";
}

export default function FindingsPanel({
  applicationId,
  task,
  findings,
  onFindingUpdated,
  onPacketChanged
}: FindingsPanelProps) {
  const [draftName, setDraftName] = useState("Role-tailored draft");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const acceptedFindingIds = findings
    .filter((finding) => isSafeSuggestion(finding) && finding.decision === "accepted")
    .map((finding) => finding.id);
  const canCreateDraft =
    task.status === "completed" &&
    task.task_type === "suggest_resume_edits" &&
    acceptedFindingIds.length > 0;

  async function decide(finding: AiFindingRecord, decision: "accepted" | "rejected") {
    setBusy(finding.id);
    setError("");
    setNotice("");
    try {
      onFindingUpdated(await decideAiFinding(finding.id, decision));
      await onPacketChanged();
    } catch (decisionError) {
      setError(decisionError instanceof Error ? decisionError.message : "The finding decision could not be saved.");
    } finally {
      setBusy("");
    }
  }

  async function createDraft(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setNotice("");
    if (!draftName.trim()) {
      setError("Draft name is required.");
      return;
    }
    if (!canCreateDraft) return;
    setBusy("draft");
    try {
      await createResumeDraft(applicationId, {
        task_id: task.id,
        finding_ids: acceptedFindingIds,
        name: draftName.trim()
      });
      setNotice("Draft created. Render it before review.");
      await onPacketChanged();
    } catch (draftError) {
      setError(draftError instanceof Error ? draftError.message : "The resume draft could not be created.");
    } finally {
      setBusy("");
    }
  }

  return (
    <section aria-label="AI findings">
      <h3>Findings</h3>
      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p className="notice" role="status">{notice}</p>}
      {findings.length === 0 ? (
        <p className="muted">No findings returned.</p>
      ) : (
        <ul className="issue-list">
          {findings.map((finding) => {
            const safe = isSafeSuggestion(finding);
            return (
              <li key={finding.id}>
                <strong>{finding.claim_text || finding.recommendation || finding.finding_type}</strong>
                {finding.recommendation && <span>{finding.recommendation}</span>}
                {payloadText(finding, "replacement_text") && (
                  <span>{payloadText(finding, "replacement_text")}</span>
                )}
                {safe ? (
                  <span><ShieldCheck aria-hidden="true" size={16} /> Verified suggestion</span>
                ) : (
                  <span>Blocked by verifier</span>
                )}
                {safe && finding.decision === "pending" && (
                  <div className="actions">
                    <button
                      type="button"
                      disabled={busy !== ""}
                      onClick={() => void decide(finding, "accepted")}
                    >
                      <Check aria-hidden="true" size={16} /> Accept suggestion
                    </button>
                    <button
                      type="button"
                      className="secondary"
                      disabled={busy !== ""}
                      onClick={() => void decide(finding, "rejected")}
                    >
                      <X aria-hidden="true" size={16} /> Reject suggestion
                    </button>
                  </div>
                )}
                {safe && finding.decision !== "pending" && <span>Decision: {finding.decision}</span>}
              </li>
            );
          })}
        </ul>
      )}

      {task.task_type === "suggest_resume_edits" && (
        <form className="resume-form" onSubmit={createDraft}>
          <label>
            Draft name
            <input value={draftName} onChange={(event) => setDraftName(event.target.value)} />
          </label>
          <button type="submit" disabled={!canCreateDraft || busy !== ""}>
            <FilePlus2 aria-hidden="true" size={16} /> Create resume draft
          </button>
        </form>
      )}
    </section>
  );
}
