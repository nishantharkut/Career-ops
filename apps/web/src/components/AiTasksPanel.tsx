import { useEffect, useMemo, useState } from "react";
import { Play, RefreshCw, ShieldCheck } from "lucide-react";

import {
  AiFindingRecord,
  AiProviderRecord,
  AiTaskRecord,
  AiTaskType,
  createAiTask,
  getAiTask,
  listAiFindings,
  listAiProviders
} from "../api";
import FindingsPanel from "./FindingsPanel";

type AiTasksPanelProps = {
  applicationId: string;
  initialTask: AiTaskRecord | null;
  readOnly?: boolean;
  onPacketChanged: () => Promise<void> | void;
};

const TASK_OPTIONS: Array<{ value: AiTaskType; label: string }> = [
  { value: "analyze_fit", label: "Analyze fit" },
  { value: "analyze_jd", label: "Analyze JD" },
  { value: "suggest_resume_edits", label: "Suggest resume edits" },
  { value: "draft_cover_letter", label: "Draft cover letter" },
  { value: "review_claims", label: "Review claims" }
];

const TASK_COMMANDS: Record<AiTaskType, string> = {
  analyze_fit: "Run AI review",
  analyze_jd: "Analyze JD",
  suggest_resume_edits: "Suggest resume edits",
  draft_cover_letter: "Draft cover letter",
  review_claims: "Review claims"
};

const TERMINAL_STATUSES = new Set(["completed", "blocked", "failed"]);

function providerLabel(provider: AiProviderRecord): string {
  let label = provider.display_name;
  if (provider.provider_kind === "codex" && provider.identity_name === "codex2") {
    label = "Codex account 2 (codex2)";
  }
  if (provider.provider_kind === "codex" && provider.identity_name === "codex") {
    label = "Codex default (codex)";
  }
  return provider.is_ai ? label : `${label} (Not AI)`;
}

function statusLabel(status: string): string {
  return `${status.slice(0, 1).toUpperCase()}${status.slice(1)}`;
}

function bounded(value: string): string {
  if (value.length <= 28) return value;
  return `${value.slice(0, 16)}...${value.slice(-8)}`;
}

export default function AiTasksPanel({ applicationId, initialTask, readOnly = false, onPacketChanged }: AiTasksPanelProps) {
  const [providers, setProviders] = useState<AiProviderRecord[]>([]);
  const [providersLoaded, setProvidersLoaded] = useState(false);
  const [providerId, setProviderId] = useState("");
  const [taskType, setTaskType] = useState<AiTaskType>("analyze_fit");
  const [acknowledged, setAcknowledged] = useState(false);
  const [activeTask, setActiveTask] = useState<AiTaskRecord | null>(initialTask);
  const [findings, setFindings] = useState<AiFindingRecord[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setError("");
    listAiProviders(controller.signal)
      .then((loaded) => {
        if (controller.signal.aborted) return;
        setProvidersLoaded(true);
        setProviders(loaded);
        setProviderId((current) => {
          if (loaded.some((provider) => provider.id === current)) return current;
          return loaded.find((provider) => provider.identity_name === "codex2")?.id ?? loaded[0]?.id ?? "";
        });
      })
      .catch((loadError: unknown) => {
        if (!controller.signal.aborted) {
          setProvidersLoaded(true);
          setError(loadError instanceof Error ? loadError.message : "Providers could not be loaded.");
        }
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    setFindings([]);
    setAcknowledged(false);
    setError("");
  }, [applicationId, initialTask?.id]);

  useEffect(() => {
    setActiveTask(initialTask);
  }, [applicationId, initialTask?.id, initialTask?.status]);

  useEffect(() => {
    if (!activeTask || !TERMINAL_STATUSES.has(activeTask.status)) return;
    const controller = new AbortController();
    const expectedTaskId = activeTask.id;
    listAiFindings(expectedTaskId, controller.signal)
      .then((loaded) => {
        if (!controller.signal.aborted && activeTask.id === expectedTaskId) setFindings(loaded);
      })
      .catch((loadError: unknown) => {
        if (!controller.signal.aborted) {
          setError(loadError instanceof Error ? loadError.message : "Findings could not be loaded.");
        }
      });
    return () => controller.abort();
  }, [activeTask?.id, activeTask?.status]);

  useEffect(() => {
    if (!activeTask || !["pending", "running"].includes(activeTask.status)) return;
    const expectedTaskId = activeTask.id;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let disposed = false;
    let controller: AbortController | null = null;

    const poll = async () => {
      controller = new AbortController();
      try {
        const updated = await getAiTask(expectedTaskId, controller.signal);
        if (disposed) return;
        setError("");
        setActiveTask(updated);
        if (TERMINAL_STATUSES.has(updated.status)) {
          await onPacketChanged();
          return;
        }
        timer = setTimeout(() => void poll(), 1500);
      } catch (pollError) {
        if (!disposed && !controller.signal.aborted) {
          setError(pollError instanceof Error ? pollError.message : "Task status could not be refreshed.");
          timer = setTimeout(() => void poll(), 1500);
        }
      }
    };

    timer = setTimeout(() => void poll(), 1500);
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      controller?.abort();
    };
  }, [activeTask?.id, activeTask?.status, applicationId, onPacketChanged]);

  const selectedProvider = useMemo(
    () => providers.find((provider) => provider.id === providerId) ?? null,
    [providerId, providers]
  );
  const taskProvider = useMemo(
    () => providers.find((provider) => provider.id === activeTask?.provider_config_id) ?? null,
    [activeTask?.provider_config_id, providers]
  );
  const cloudCodex =
    selectedProvider?.provider_kind === "codex" && selectedProvider.privacy_mode === "cloud_external";
  const providerUnavailable =
    !selectedProvider || !selectedProvider.enabled || !selectedProvider.available;
  const unavailableReason = !selectedProvider
    ? "no providers configured"
    : selectedProvider.enabled
      ? selectedProvider.availability_detail
      : "disabled";
  const unavailableReasonId = `provider-unavailable-${applicationId}`;
  const taskInProgress = activeTask?.status === "pending" || activeTask?.status === "running";
  const runDisabled = readOnly || busy || taskInProgress || providerUnavailable || (cloudCodex && !acknowledged);

  async function runTask() {
    if (readOnly || !selectedProvider || runDisabled) return;
    setBusy(true);
    setError("");
    setFindings([]);
    const selectedProviderId = selectedProvider.id;
    try {
      const created = await createAiTask(applicationId, {
        task_type: taskType,
        provider_config_id: selectedProviderId,
        input_artifact_ids: [],
        cloud_transfer_acknowledged: cloudCodex && acknowledged
      });
      setActiveTask(created);
      setAcknowledged(false);
      await onPacketChanged();
    } catch (launchError) {
      setError(launchError instanceof Error ? launchError.message : "The task could not be launched.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel" aria-label="AI tasks">
      <h2>AI review</h2>
      {error && <p className="error" role="alert">{error}</p>}
      <label>
        Provider
        <select
          value={providerId}
          disabled={readOnly}
          aria-describedby={providersLoaded && providerUnavailable ? unavailableReasonId : undefined}
          onChange={(event) => {
            setProviderId(event.target.value);
            setAcknowledged(false);
          }}
        >
          {providers.length === 0 && <option value="">No providers configured</option>}
          {providers.map((provider) => (
            <option value={provider.id} key={provider.id}>
              {providerLabel(provider)}
            </option>
          ))}
        </select>
      </label>
      <label>
        Task
        <select
          value={taskType}
          disabled={readOnly}
          onChange={(event) => {
            setTaskType(event.target.value as AiTaskType);
            setAcknowledged(false);
          }}
        >
          {TASK_OPTIONS.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
        </select>
      </label>

      {providersLoaded && providerUnavailable && (
        <p id={unavailableReasonId} className="error" role="status" aria-live="polite">
          Provider unavailable: {unavailableReason}
        </p>
      )}
      {(selectedProvider?.is_ai === false || taskProvider?.is_ai === false) && (
        <p className="notice" role="status">Not AI. This verifier cannot unlock approval.</p>
      )}
      {cloudCodex && (
        <>
          <p className="notice">Selected task data is sent through your signed-in Codex account</p>
          <label>
            <input
              type="checkbox"
              checked={acknowledged}
              disabled={readOnly}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />
            Acknowledge Codex data transfer for this run
          </label>
        </>
      )}
      {taskType === "suggest_resume_edits" && <p>Creates suggestions, not final files</p>}
      <button type="button" disabled={runDisabled} onClick={() => void runTask()}>
        {busy || activeTask?.status === "pending" || activeTask?.status === "running" ? (
          <RefreshCw aria-hidden="true" size={16} />
        ) : selectedProvider?.is_ai === false ? (
          <ShieldCheck aria-hidden="true" size={16} />
        ) : (
          <Play aria-hidden="true" size={16} />
        )}
        {selectedProvider?.is_ai === false ? "Run verifier" : TASK_COMMANDS[taskType]}
      </button>

      {activeTask && (
        <section aria-label="Task status">
          <h3>Status</h3>
          <p role="status">{statusLabel(activeTask.status)}</p>
          {activeTask.error_summary && <p className="error" role="alert" aria-live="assertive">{activeTask.error_summary}</p>}
          <dl className="meta">
            <div><dt>Provider</dt><dd>{taskProvider ? providerLabel(taskProvider) : activeTask.provider_config_id}</dd></div>
            <div><dt>Model</dt><dd>{taskProvider?.model || "Not reported"}</dd></div>
            <div><dt>Privacy</dt><dd>{activeTask.privacy_mode}</dd></div>
            <div><dt>Task</dt><dd>{activeTask.task_type} / {bounded(activeTask.id)}</dd></div>
            <div><dt>Prompt</dt><dd>{activeTask.prompt_template_id || "Not reported"}</dd></div>
            <div><dt>Checksum</dt><dd>{bounded(activeTask.prompt_sha256 || "Not reported")}</dd></div>
          </dl>
        </section>
      )}

      {activeTask && TERMINAL_STATUSES.has(activeTask.status) && (
        <FindingsPanel
          applicationId={applicationId}
          task={activeTask}
          findings={findings}
          readOnly={readOnly}
          onFindingUpdated={(updated) =>
            setFindings((current) => current.map((finding) => (finding.id === updated.id ? updated : finding)))
          }
          onPacketChanged={onPacketChanged}
        />
      )}
    </section>
  );
}
