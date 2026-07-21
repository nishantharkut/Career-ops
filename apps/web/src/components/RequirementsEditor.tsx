import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { Check, FilePlus2, X } from "lucide-react";

import { JsonValue, RequirementsRecord, getRequirements, updateRequirements } from "../api";

type CorrectionKind = "text" | "number" | "boolean" | "list" | "unknown" | "preserve" | "remove";
type ListItemKind = "text" | "number" | "boolean" | "unknown";

type ListItemRow = {
  id: number;
  kind: ListItemKind;
  value: string;
};

type CorrectionRow = {
  id: number;
  originalKey: string;
  originalValue: JsonValue;
  key: string;
  value: string;
  kind: CorrectionKind;
  listItems: ListItemRow[];
  dirty: boolean;
};

type RequirementsEditorProps = {
  applicationId: string;
  readOnly: boolean;
  onMutated: () => Promise<void> | void;
};

const GROUPS: Array<{ key: keyof RequirementsRecord; label: string }> = [
  { key: "uncertainty", label: "Unknown and uncertain" },
  { key: "deterministic", label: "Deterministic" },
  { key: "raw_ai", label: "Raw AI" },
  { key: "user_corrected", label: "Current user corrections" }
];

function isPrimitiveList(value: JsonValue): value is Array<string | number | boolean | null> {
  return Array.isArray(value) && value.every((item) => item === null || typeof item !== "object");
}

function kindFor(value: JsonValue): CorrectionKind {
  if (value === null) return "unknown";
  if (typeof value === "number") return "number";
  if (typeof value === "boolean") return "boolean";
  if (typeof value === "string") return "text";
  if (isPrimitiveList(value)) return "list";
  return "preserve";
}

function inputValue(value: JsonValue): string {
  if (value === null) return "";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value !== "object") return String(value);
  return "";
}

function listItemKind(value: string | number | boolean | null): ListItemKind {
  if (value === null) return "unknown";
  if (typeof value === "number") return "number";
  if (typeof value === "boolean") return "boolean";
  return "text";
}

function listItemsFrom(value: JsonValue, nextId: () => number): ListItemRow[] {
  if (!isPrimitiveList(value)) return [];
  return value.map((item) => ({
    id: nextId(),
    kind: listItemKind(item),
    value: item === null ? "" : typeof item === "boolean" ? (item ? "yes" : "no") : String(item)
  }));
}

function rowsFrom(requirements: RequirementsRecord, nextId: () => number): CorrectionRow[] {
  return Object.entries(requirements.user_corrected).map(([key, value]) => ({
    id: nextId(),
    originalKey: key,
    originalValue: value,
    key,
    value: inputValue(value),
    kind: kindFor(value),
    listItems: listItemsFrom(value, nextId),
    dirty: false
  }));
}

function HumanValue({ value }: { value: JsonValue }) {
  if (value === null || value === "" || (typeof value === "string" && value.toLowerCase() === "unknown")) {
    return <span>Unknown</span>;
  }
  if (typeof value === "boolean") return <span>{value ? "Yes" : "No"}</span>;
  if (typeof value === "string" || typeof value === "number") return <span>{value}</span>;
  if (Array.isArray(value)) {
    if (value.length === 0) return <span>No values</span>;
    return <ul>{value.map((item, index) => <li key={index}><HumanValue value={item} /></li>)}</ul>;
  }
  const entries = Object.entries(value);
  if (entries.length === 0) return <span>No values</span>;
  return (
    <dl className="meta">
      {entries.map(([key, item]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd><HumanValue value={item} /></dd>
        </div>
      ))}
    </dl>
  );
}

function valueForRow(row: CorrectionRow): JsonValue {
  if (!row.dirty && row.key.trim() === row.originalKey) return row.originalValue;
  if (row.kind === "preserve") return row.originalValue;
  if (row.kind === "unknown") return null;
  if (row.kind === "boolean") return row.value === "yes";
  if (row.kind === "number") {
    if (!row.value.trim()) throw new Error(`Enter a valid number for ${row.key.trim() || "this correction"}.`);
    const value = Number(row.value);
    if (!Number.isFinite(value)) throw new Error(`Enter a valid number for ${row.key.trim() || "this correction"}.`);
    return value;
  }
  if (row.kind === "list") {
    const key = row.key.trim() || "this correction";
    if (row.listItems.length === 0) throw new Error(`Enter at least one list item for ${key}.`);
    return row.listItems.map((item, index) => {
      if (item.kind === "unknown") return null;
      if (item.kind === "boolean") return item.value === "yes";
      if (item.kind === "number") {
        if (!item.value.trim() || !Number.isFinite(Number(item.value))) {
          throw new Error(`Enter a valid number for list item ${index + 1} in ${key}.`);
        }
        return Number(item.value);
      }
      if (!item.value) throw new Error(`Enter a value for list item ${index + 1} in ${key}.`);
      return item.value;
    });
  }
  return row.value;
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
    setRows((current) => current.map((row) => row.id === rowId ? { ...row, ...changes, dirty: true } : row));
  }

  function changeRowKind(row: CorrectionRow, kind: CorrectionKind) {
    const listItems = kind === "list" && row.listItems.length === 0
      ? [{ id: newId(), kind: "text" as const, value: row.value }]
      : row.listItems;
    updateRow(row.id, { kind, listItems });
  }

  function updateListItem(rowId: number, itemId: number, changes: Partial<ListItemRow>) {
    setRows((current) => current.map((row) => row.id === rowId ? {
      ...row,
      dirty: true,
      listItems: row.listItems.map((item) => item.id === itemId ? { ...item, ...changes } : item)
    } : row));
  }

  function changeListItemKind(rowId: number, item: ListItemRow, kind: ListItemKind) {
    updateListItem(rowId, item.id, {
      kind,
      value: kind === "boolean" ? (item.value === "no" ? "no" : "yes") : kind === "unknown" ? "" : item.value
    });
  }

  function removeRow(row: CorrectionRow) {
    if (!row.originalKey) {
      setRows((current) => current.filter((item) => item.id !== row.id));
      return;
    }
    updateRow(row.id, { kind: "remove" });
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setNotice("");
    if (!requirements) return;

    const corrected: Record<string, JsonValue> = { ...requirements.user_corrected };
    const usedKeys = new Set<string>();
    try {
      for (const row of rows) {
        if (row.originalKey) delete corrected[row.originalKey];
        if (row.kind === "remove") continue;
        const key = row.key.trim();
        if (!key) throw new Error("Every correction needs a key.");
        if (usedKeys.has(key)) throw new Error(`Correction key ${key} is duplicated.`);
        usedKeys.add(key);
        corrected[key] = valueForRow(row);
      }
    } catch (validationError) {
      setError(validationError instanceof Error ? validationError.message : "A correction is invalid.");
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
      {requirements && GROUPS.map((group) => {
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
                    <dd><HumanValue value={value} /></dd>
                  </div>
                ))}
              </dl>
            )}
          </section>
        );
      })}

      <form className="resume-form" onSubmit={save}>
        <h3>Edit user corrections</h3>
        {rows.length === 0 && <p className="muted">No corrections saved.</p>}
        {rows.map((row, index) => (
          <fieldset key={row.id} disabled={readOnly || busy}>
            <legend>Correction {index + 1}</legend>
            <label>
              Correction key {index + 1}
              <input value={row.key} onChange={(event) => updateRow(row.id, { key: event.target.value })} />
            </label>
            <label>
              {`Correction kind for ${row.key || `row ${index + 1}`}`}
              <select
                value={row.kind}
                onChange={(event) => changeRowKind(row, event.target.value as CorrectionKind)}
              >
                <option value="text">Text</option>
                <option value="number">Number</option>
                <option value="boolean">Yes or no</option>
                <option value="list">List</option>
                <option value="unknown">Unknown</option>
                {row.kind === "preserve" && <option value="preserve">Keep current value</option>}
                <option value="remove">Remove</option>
              </select>
            </label>
            {row.kind === "list" ? (
              <div aria-label={`List items for ${row.key || `row ${index + 1}`}`}>
                {row.listItems.map((item, itemIndex) => (
                  <div key={item.id}>
                    <label>
                      {`List item ${itemIndex + 1} kind for ${row.key || `row ${index + 1}`}`}
                      <select
                        value={item.kind}
                        onChange={(event) => changeListItemKind(row.id, item, event.target.value as ListItemKind)}
                      >
                        <option value="text">Text</option>
                        <option value="number">Number</option>
                        <option value="boolean">Yes or no</option>
                        <option value="unknown">Unknown</option>
                      </select>
                    </label>
                    {item.kind === "boolean" ? (
                      <label>
                        {`List item ${itemIndex + 1} value for ${row.key || `row ${index + 1}`}`}
                        <select value={item.value} onChange={(event) => updateListItem(row.id, item.id, { value: event.target.value })}>
                          <option value="yes">Yes</option>
                          <option value="no">No</option>
                        </select>
                      </label>
                    ) : item.kind === "unknown" ? (
                      <p>List item {itemIndex + 1} is Unknown.</p>
                    ) : (
                      <label>
                        {`List item ${itemIndex + 1} value for ${row.key || `row ${index + 1}`}`}
                        <input
                          type={item.kind === "number" ? "number" : "text"}
                          value={item.value}
                          onChange={(event) => updateListItem(row.id, item.id, { value: event.target.value })}
                        />
                      </label>
                    )}
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => updateRow(row.id, { listItems: row.listItems.filter((candidate) => candidate.id !== item.id) })}
                    >
                      <X aria-hidden="true" size={16} /> Remove list item {itemIndex + 1}
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  className="secondary"
                  onClick={() => updateRow(row.id, {
                    listItems: [...row.listItems, { id: newId(), kind: "text", value: "" }]
                  })}
                >
                  <FilePlus2 aria-hidden="true" size={16} /> Add list item for {row.key || `row ${index + 1}`}
                </button>
              </div>
            ) : row.kind === "boolean" ? (
              <label>
                {`Correction value for ${row.key || `row ${index + 1}`}`}
                <select value={row.value} onChange={(event) => updateRow(row.id, { value: event.target.value })}>
                  <option value="yes">Yes</option>
                  <option value="no">No</option>
                </select>
              </label>
            ) : ["text", "number"].includes(row.kind) ? (
              <label>
                {`Correction value for ${row.key || `row ${index + 1}`}`}
                <input
                  type={row.kind === "number" ? "number" : "text"}
                  value={row.value}
                  onChange={(event) => updateRow(row.id, { value: event.target.value })}
                />
              </label>
            ) : row.kind === "unknown" ? (
              <p>Saved as Unknown.</p>
            ) : row.kind === "remove" ? (
              <p>This correction will be removed.</p>
            ) : (
              <p>The current nested value will be preserved.</p>
            )}
            <button type="button" className="secondary" onClick={() => removeRow(row)}>
              <X aria-hidden="true" size={16} /> Remove correction
            </button>
          </fieldset>
        ))}
        <div className="actions">
          <button
            type="button"
            className="secondary"
            disabled={readOnly || busy}
            onClick={() => setRows((current) => [...current, {
              id: newId(),
              originalKey: "",
              originalValue: "",
              key: "",
              value: "",
              kind: "text",
              listItems: [],
              dirty: true
            }])}
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
