import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { FilePlus2, ShieldCheck } from "lucide-react";

import {
  EvidenceRecord,
  PrivacyLevel,
  ProfileFactRecord,
  VerificationState,
  createEvidence,
  createProfileFact,
  linkEvidence,
  listEvidence,
  listProfileFacts
} from "../api";

type EvidencePanelProps = {
  applicationId: string;
  readOnly: boolean;
  onMutated: () => Promise<void> | void;
};

const VERIFICATION_OPTIONS: Array<{ value: VerificationState; label: string }> = [
  { value: "verified", label: "Verified" },
  { value: "user_stated", label: "User stated" },
  { value: "needs_verification", label: "Needs verification" },
  { value: "rejected", label: "Rejected" },
  { value: "private_do_not_publish", label: "Private, do not publish" }
];

const PRIVACY_OPTIONS: Array<{ value: PrivacyLevel; label: string }> = [
  { value: "public", label: "Public" },
  { value: "private", label: "Private" },
  { value: "sensitive", label: "Sensitive" }
];

function SelectOptions() {
  return VERIFICATION_OPTIONS.map((option) => (
    <option value={option.value} key={option.value}>{option.label}</option>
  ));
}

function PrivacyOptions() {
  return PRIVACY_OPTIONS.map((option) => (
    <option value={option.value} key={option.value}>{option.label}</option>
  ));
}

export default function EvidencePanel({ applicationId, readOnly, onMutated }: EvidencePanelProps) {
  const [evidence, setEvidence] = useState<EvidenceRecord[]>([]);
  const [facts, setFacts] = useState<ProfileFactRecord[]>([]);
  const [claim, setClaim] = useState("");
  const [evidenceType, setEvidenceType] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [verification, setVerification] = useState<VerificationState>("needs_verification");
  const [privacy, setPrivacy] = useState<PrivacyLevel>("private");
  const [evidenceId, setEvidenceId] = useState("");
  const [factKey, setFactKey] = useState("");
  const [factValue, setFactValue] = useState("");
  const [factVerification, setFactVerification] = useState<VerificationState>("needs_verification");
  const [factPrivacy, setFactPrivacy] = useState<PrivacyLevel>("private");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const generation = useRef(0);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      const requestGeneration = ++generation.current;
      try {
        const [linked, profileFacts] = await Promise.all([
          listEvidence(applicationId, signal),
          listProfileFacts(signal)
        ]);
        if (requestGeneration === generation.current && !signal?.aborted) {
          setEvidence(linked);
          setFacts(profileFacts);
        }
      } catch (loadError) {
        if (requestGeneration === generation.current && !signal?.aborted) {
          setError(loadError instanceof Error ? loadError.message : "Evidence could not be loaded.");
        }
      }
    },
    [applicationId]
  );

  useEffect(() => {
    const controller = new AbortController();
    setEvidence([]);
    setFacts([]);
    setError("");
    setNotice("");
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  async function completeMutation(message: string) {
    setNotice(message);
    await onMutated();
    await load();
  }

  async function addEvidence(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setNotice("");
    if (!claim.trim() || !evidenceType.trim()) {
      setError("Evidence claim and type are required.");
      return;
    }
    setBusy(true);
    try {
      await createEvidence(applicationId, {
        claim_text: claim.trim(),
        evidence_type: evidenceType.trim(),
        ...(sourceUrl.trim() ? { source_url: sourceUrl.trim() } : {}),
        verification_state: verification,
        privacy_level: privacy
      });
      setClaim("");
      setEvidenceType("");
      setSourceUrl("");
      await completeMutation("Evidence added.");
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Evidence could not be added.");
    } finally {
      setBusy(false);
    }
  }

  async function linkExisting(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setNotice("");
    if (!evidenceId.trim()) {
      setError("Evidence ID is required.");
      return;
    }
    setBusy(true);
    try {
      await linkEvidence(applicationId, evidenceId.trim());
      setEvidenceId("");
      await completeMutation("Evidence linked.");
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Evidence could not be linked.");
    } finally {
      setBusy(false);
    }
  }

  async function addFact(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setNotice("");
    if (!factKey.trim() || !factValue.trim()) {
      setError("Fact key and value are required.");
      return;
    }
    setBusy(true);
    try {
      await createProfileFact({
        fact_key: factKey.trim(),
        fact_value: factValue.trim(),
        verification_state: factVerification,
        privacy_level: factPrivacy
      });
      setFactKey("");
      setFactValue("");
      await completeMutation("Profile fact added.");
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Profile fact could not be added.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Evidence and profile facts">
      <h3>Linked evidence</h3>
      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p className="notice" role="status">{notice}</p>}
      {evidence.length === 0 ? (
        <p className="muted">No evidence linked.</p>
      ) : (
        <ul className="issue-list">
          {evidence.map((item) => (
            <li key={item.id}>
              <strong>{item.claim_text}</strong>
              <span>{item.evidence_type} / {item.verification_state} / {item.privacy_level}</span>
              {item.publishable && <span><ShieldCheck aria-hidden="true" size={16} /> Verified public evidence</span>}
            </li>
          ))}
        </ul>
      )}

      <form className="resume-form" onSubmit={addEvidence}>
        <h3>Create evidence</h3>
        <label>Evidence claim<input value={claim} onChange={(event) => setClaim(event.target.value)} /></label>
        <label>Evidence type<input value={evidenceType} onChange={(event) => setEvidenceType(event.target.value)} /></label>
        <label>Source URL<input type="url" value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} /></label>
        <label>
          Evidence verification
          <select value={verification} onChange={(event) => setVerification(event.target.value as VerificationState)}><SelectOptions /></select>
        </label>
        <label>
          Evidence privacy
          <select value={privacy} onChange={(event) => setPrivacy(event.target.value as PrivacyLevel)}><PrivacyOptions /></select>
        </label>
        <button type="submit" disabled={readOnly || busy}><FilePlus2 aria-hidden="true" size={16} /> Add evidence</button>
      </form>

      <form className="resume-form" onSubmit={linkExisting}>
        <h3>Link existing evidence</h3>
        <label>Evidence ID<input value={evidenceId} onChange={(event) => setEvidenceId(event.target.value)} /></label>
        <button type="submit" disabled={readOnly || busy}>Link evidence</button>
      </form>

      <section>
        <h3>Profile facts</h3>
        {facts.length === 0 ? (
          <p className="muted">No profile facts recorded.</p>
        ) : (
          <dl className="meta">
            {facts.map((fact) => (
              <div key={fact.id}>
                <dt>{fact.fact_key}</dt>
                <dd>{fact.fact_value} / {fact.verification_state} / {fact.privacy_level}</dd>
              </div>
            ))}
          </dl>
        )}
      </section>

      <form className="resume-form" onSubmit={addFact}>
        <h3>Add profile fact</h3>
        <label>Fact key<input value={factKey} onChange={(event) => setFactKey(event.target.value)} /></label>
        <label>Fact value<input value={factValue} onChange={(event) => setFactValue(event.target.value)} /></label>
        <label>
          Fact verification
          <select value={factVerification} onChange={(event) => setFactVerification(event.target.value as VerificationState)}><SelectOptions /></select>
        </label>
        <label>
          Fact privacy
          <select value={factPrivacy} onChange={(event) => setFactPrivacy(event.target.value as PrivacyLevel)}><PrivacyOptions /></select>
        </label>
        <button type="submit" disabled={readOnly || busy}><FilePlus2 aria-hidden="true" size={16} /> Add profile fact</button>
      </form>
    </section>
  );
}
