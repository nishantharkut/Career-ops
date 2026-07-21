import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { Check, FilePlus2, X } from "lucide-react";

import { JsonValue, RequirementsRecord, getRequirements, updateRequirements } from "../api";

type CorrectionRow = {
  id: number;
  key: string;
  value: string;
  format: "text" | "json";
};

type RequirementsEditorProps = {
  applicationId: string;
  readOnly: boolean;
  onMutated: () => Promise<void> | void;
};

const GROUPS: Array<{ key: keyof RequirementsRecord; label: string }> = [
  { key: "raw_ai", label: "Raw AI" },
  { key: "deterministic", label: "Deterministic" },
  { key: "uncertainty", label: "Uncertainty" }
];

function displayValue(value: JsonValue): string {
  if (typeof value === "string") return value || "Unknown";
  if (value === null) return "Unknown";
  return JSON.stringify(value);
}

function rowsFrom(requirements: RequirementsRecord, nextId: () => number): CorrectionRow[] {
  return Object.entries(requirements.user_corrected).map(([key, value]) => ({
    id: nextId(),
    key,
    value: typeof value === "string" ? value : JSON.stringify(value),
    format: typeof value === "string" ? "text" : "json"
  }));
}

export default function RequirementsEditor({ applicationId, readOnly, onMutated }: RequirementsEditorProps) {
  const [requirements, setRequirements] = useState<RequirementsRecord | null>(null);
  const [rows, setRows] = useState<CorrectionRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const nextRowId = useRef(0);
  const generation = useRef(0);

  const newId = useCallback(() => ++nextRowId.current, []);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      const requestGeneration = ++generation.current;
      setError("");
      try {
        const loaded = await getRequirements(applicationId, signal);
        if (requestGeneration === generation.current && !signal?.aborted) {
          setRequirements(loaded);
          setRows(rowsFrom(loaded, newId));
        }
      } catch (loadError) {
        if (!signal?.aborted && requestGeneration === generation.current) {
          setError(loadError instanceof Error ? loadError.message : "Requirements could not be loaded.");
        }
      }
    },
    [applicationId, newId]
  );

  useEffect(() => {
    const controller = new AbortController();
    setRequirements(null);
    setRows([]);
    setNotice("");
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  function updateRow(rowId: number, changes: Partial<CorrectionRow>) {
    setRows((current) => current.map((row) => (row.id === rowId ? { ...row, ...changes } : row)));
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setNotice("");
    const corrected: Record<string, JsonValue> = {};
    try {
      for (const row of rows) {
        const key = row.key.trim();
        if (!key) throw new Error("Every correction needs a key.");
        if (key in corrected) throw new Error(`Correction key ${key} is duplicated.`);
        corrected[key] = row.format === "json" ? (JSON.parse(row.value) as JsonValue) : row.value;
      }
    } catch (parseError) {
      setError(parseError instanceof Error ? parseError.message : "A structured correction is invalid.");
      return;
    }

    setBusy(true);
    try {
      const updated = await updateRequirements(applicationId, corrected);
      setRequirements(updated);
      setRows(rowsFrom(updated, newId));
      setNotice("Requirements saved.");
      await onMutated();
      await load();
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Requirements could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  if (!requirements && !error) return <p role="status">Loading requirements</p>;

  return (
    <section aria-label="Requirements editor">
      <h3>Requirements</h3>
      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p className="notice" role="status">{notice}</p>}
      {requirements &&
        GROUPS.map((group) => {
          const entries = Object.entries(requirements[group.key]);
          return (
            <section key={group.key} aria-labelledby={`requirements-${group.key}`}>
              <h3 id={`requirements-${group.key}`}>{group.label}</h3>
              {entries.length === 0 ? (
                <p className="muted">No values recorded.</p>
              ) : (
                <dl className="meta">
                  {entries.map(([key, value]) => (
                    <div key={key}>
                      <dt>{key}</dt>
                      <dd>{displayValue(value)}</dd>
                    </div>
                  ))}
                </dl>
              )}
            </section>
          );
        })}

      <form className="resume-form" onSubmit={save}>
        <h3>User corrections</h3>
        {rows.length === 0 && <p className="muted">No corrections saved.</p>}
        {rows.map((row, index) => (
          <fieldset key={row.id} disabled={readOnly || busy}>
            <legend>Correction {index + 1}</legend>
            <label>
              Correction key {index + 1}
              <input value={row.key} onChange={(event) => updateRow(row.id, { key: event.target.value })} />
            </label>
            <label>
              {`Correction value for ${row.key || `row ${index + 1}`}`}
              <input value={row.value} onChange={(event) => updateRow(row.id, { value: event.target.value })} />
            </label>
            <label>
              {`Correction type for ${row.key || `row ${index + 1}`}`}
              <select value={row.format} onChange={(event) => updateRow(row.id, { format: event.target.value as CorrectionRow["format"] })}>
                <option value="text">Text</option>
                <option value="json">Structured value</option>
              </select>
            </label>
            <button
              type="button"
              className="secondary"
              onClick={() => setRows((current) => current.filter((item) => item.id !== row.id))}
            >
              <X aria-hidden="true" size={16} /> Remove correction
            </button>
          </fieldset>
        ))}
        <div className="actions">
          <button
            type="button"
            className="secondary"
            disabled={readOnly || busy}
            onClick={() => setRows((current) => [...current, { id: newId(), key: "", value: "", format: "text" }])}
          >
            <FilePlus2 aria-hidden="true" size={16} /> Add correction
          </button>
          <button type="submit" disabled={readOnly || busy}>
            <Check aria-hidden="true" size={16} /> Save corrections
          </button>
        </div>
      </form>
    </section>
  );
}
