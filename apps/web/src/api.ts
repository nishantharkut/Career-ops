export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

export type ApplicationRecord = {
  id: string;
  company: string;
  role_title: string;
  location: string;
  source_url: string;
  source_kind: string;
  jd_snapshot_id: string;
  status: string;
  selected_resume_version_id: string | null;
  latest_report_id: string | null;
  approval_id: string | null;
  submission_state: string;
  deadline: string | null;
  notes: string;
  role_type: string;
  submitted_at: string | null;
  created_at: string;
  updated_at: string;
};

export type ResumeRecord = {
  id: string;
  name: string;
  role_family: string;
  tex_sha256: string;
  pdf_sha256: string | null;
  parent_resume_version_id: string | null;
  render_status: string;
  parse_status: string;
  page_count: number | null;
  created_at: string;
  updated_at: string;
  render_error?: string;
};

export type RiskIssue = {
  severity: string;
  label: string;
  source: string;
  why: string;
  next_action: string;
};

export type ReportRecord = {
  id: string;
  application_id: string;
  resume_version_id: string;
  jd_snapshot_id: string;
  blocking_issue_count: number;
  warning_count: number;
  fit: {
    matched_requirements?: string[];
    missing_requirements?: string[];
    exact_keyword_gaps?: string[];
    summary_score?: number;
    score?: number;
    [key: string]: unknown;
  };
  risks: {
    blockers: RiskIssue[];
    warnings: RiskIssue[];
    [key: string]: unknown;
  };
  created_at: string;
};

export type ApprovalRecord = {
  id: string;
  application_id: string;
  resume_version_id: string;
  report_id: string;
  approval_note: string;
  pdf_sha256: string;
  approved_at: string;
};

export type RequirementsRecord = {
  raw_ai: Record<string, JsonValue>;
  deterministic: Record<string, JsonValue>;
  user_corrected: Record<string, JsonValue>;
  uncertainty: Record<string, JsonValue>;
};

export type JobDescriptionRecord = {
  id: string;
  application_id: string;
  source_url: string;
  raw_text_sha256: string;
  captured_at: string;
  parser_version: string;
  requirements: RequirementsRecord;
};

export type AiTaskType =
  | "analyze_fit"
  | "analyze_jd"
  | "suggest_resume_edits"
  | "draft_cover_letter"
  | "review_claims";

export type AiTaskStatus = "pending" | "running" | "completed" | "blocked" | "failed";

export type AiTaskRecord = {
  id: string;
  application_id: string;
  task_type: AiTaskType;
  provider_config_id: string;
  prompt_template_id: string;
  prompt_sha256: string;
  input_artifact_ids: string[];
  status: AiTaskStatus;
  privacy_mode: string;
  started_at: string | null;
  completed_at: string | null;
  error_summary: string;
  created_at: string;
  updated_at: string;
};

export type AiProviderRecord = {
  id: string;
  provider_kind: string;
  display_name: string;
  identity_name: string;
  enabled: boolean;
  privacy_mode: string;
  endpoint: string;
  model: string;
  available: boolean;
  availability_detail: string;
  is_ai: boolean;
  data_leaves_machine: boolean;
  current_metadata: Record<string, JsonValue> | null;
  created_at: string;
  updated_at: string;
};

export type AiFindingRecord = {
  id: string;
  task_id: string;
  finding_type: string;
  severity: string;
  claim_text: string;
  evidence_item_id: string | null;
  recommendation: string;
  verifier_state: string;
  decision: "pending" | "accepted" | "rejected";
  payload: Record<string, JsonValue>;
  created_at: string;
  updated_at: string;
};

export type VerificationState =
  | "verified"
  | "user_stated"
  | "needs_verification"
  | "rejected"
  | "private_do_not_publish";

export type PrivacyLevel = "public" | "private" | "sensitive";

export type EvidenceRecord = {
  id: string;
  claim_text: string;
  evidence_type: string;
  source_url: string | null;
  source_sha256: string | null;
  verification_state: VerificationState;
  privacy_level: PrivacyLevel;
  publishable: boolean;
  notes: string;
  created_at: string;
  updated_at: string;
};

export type ProfileFactRecord = {
  id: string;
  fact_key: string;
  fact_value: string;
  verification_state: VerificationState;
  privacy_level: PrivacyLevel;
  notes: string;
  created_at: string;
  updated_at: string;
};

export type ApplicationVersionRecord = {
  id: string;
  application_id: string;
  version_number: number;
  event_type: string;
  snapshot: Record<string, JsonValue>;
  created_at: string;
};

export type ApplicationPacket = {
  application: ApplicationRecord;
  job_description: JobDescriptionRecord;
  selected_resume: ResumeRecord | null;
  latest_report: ReportRecord | null;
  approval: ApprovalRecord | null;
  latest_ai_task: AiTaskRecord | null;
  evidence: EvidenceRecord[];
  versions: ApplicationVersionRecord[];
};

type ApiEnvironment = {
  DEV?: boolean;
  VITE_CAREEROPS_API?: string;
};

export function resolveApiBase(environment: ApiEnvironment): string {
  if (environment.VITE_CAREEROPS_API) return environment.VITE_CAREEROPS_API;
  return environment.DEV ? "http://127.0.0.1:8765" : "";
}

const API_BASE = resolveApiBase(import.meta.env);

function id(value: string): string {
  return encodeURIComponent(value);
}

async function responseError(response: Response): Promise<Error> {
  const fallback = `${response.status} ${response.statusText || "Request failed"}`;
  let message = fallback;
  try {
    const body: unknown = await response.json();
    if (
      body !== null &&
      typeof body === "object" &&
      "detail" in body &&
      typeof body.detail === "string" &&
      body.detail.trim()
    ) {
      message = body.detail.trim().slice(0, 500);
    }
  } catch {
    message = fallback;
  }
  return new Error(message);
}

async function requireOk(response: Response): Promise<Response> {
  if (!response.ok) {
    throw await responseError(response);
  }
  return response;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await requireOk(await fetch(`${API_BASE}${path}`, init));
  return response.json() as Promise<T>;
}

function jsonInit(method: "POST" | "PUT", payload: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  };
}

export function listApplications(signal?: AbortSignal): Promise<ApplicationRecord[]> {
  return request<ApplicationRecord[]>("/applications", { signal });
}

export function createApplication(payload: {
  company: string;
  role_title: string;
  location: string;
  source_url: string;
  jd_text: string;
}): Promise<ApplicationRecord> {
  return request<ApplicationRecord>("/applications", jsonInit("POST", payload));
}

export function getApplicationPacket(applicationId: string, signal?: AbortSignal): Promise<ApplicationPacket> {
  return request<ApplicationPacket>(`/applications/${id(applicationId)}/packet`, { signal });
}

export function uploadResume(
  applicationId: string,
  payload: { name: string; role_family: string; file: File }
): Promise<ResumeRecord> {
  const form = new FormData();
  form.set("name", payload.name);
  form.set("role_family", payload.role_family);
  form.set("tex_file", payload.file);
  return request<ResumeRecord>(`/applications/${id(applicationId)}/resume`, {
    method: "POST",
    body: form
  });
}

export function renderResume(resumeId: string): Promise<ResumeRecord> {
  return request<ResumeRecord>(`/resumes/${id(resumeId)}/render`, { method: "POST" });
}

export function analyzeApplication(applicationId: string): Promise<ReportRecord> {
  return request<ReportRecord>(
    `/applications/${id(applicationId)}/analyze`,
    jsonInit("POST", { claims: [] })
  );
}

export function approveApplication(applicationId: string, reportId: string, note: string): Promise<ApprovalRecord> {
  return request<ApprovalRecord>(
    `/applications/${id(applicationId)}/approve`,
    jsonInit("POST", { report_id: reportId, note })
  );
}

export function markSubmitted(applicationId: string, note: string): Promise<ApplicationRecord> {
  return request<ApplicationRecord>(
    `/applications/${id(applicationId)}/mark-submitted`,
    jsonInit("POST", { note })
  );
}

export function listAiProviders(signal?: AbortSignal): Promise<AiProviderRecord[]> {
  return request<AiProviderRecord[]>("/ai/providers", { signal });
}

export function createAiTask(
  applicationId: string,
  payload: { task_type: AiTaskType; provider_config_id: string; input_artifact_ids: string[] }
): Promise<AiTaskRecord> {
  return request<AiTaskRecord>(
    `/applications/${id(applicationId)}/ai-tasks`,
    jsonInit("POST", payload)
  );
}

export function getAiTask(taskId: string, signal?: AbortSignal): Promise<AiTaskRecord> {
  return request<AiTaskRecord>(`/ai-tasks/${id(taskId)}`, { signal });
}

export function listAiFindings(taskId: string, signal?: AbortSignal): Promise<AiFindingRecord[]> {
  return request<AiFindingRecord[]>(`/ai-tasks/${id(taskId)}/findings`, { signal });
}

export function decideAiFinding(
  findingId: string,
  decision: "accepted" | "rejected"
): Promise<AiFindingRecord> {
  return request<AiFindingRecord>(
    `/ai-findings/${id(findingId)}/decision`,
    jsonInit("POST", { decision })
  );
}

export function getRequirements(applicationId: string, signal?: AbortSignal): Promise<RequirementsRecord> {
  return request<RequirementsRecord>(`/applications/${id(applicationId)}/requirements`, { signal });
}

export function updateRequirements(
  applicationId: string,
  userCorrected: Record<string, JsonValue>
): Promise<RequirementsRecord> {
  return request<RequirementsRecord>(
    `/applications/${id(applicationId)}/requirements`,
    jsonInit("PUT", { user_corrected: userCorrected })
  );
}

export function listProfileFacts(signal?: AbortSignal): Promise<ProfileFactRecord[]> {
  return request<ProfileFactRecord[]>("/profile-facts", { signal });
}

export function createProfileFact(payload: {
  fact_key: string;
  fact_value: string;
  verification_state: VerificationState;
  privacy_level: PrivacyLevel;
  notes?: string;
}): Promise<ProfileFactRecord> {
  return request<ProfileFactRecord>("/profile-facts", jsonInit("POST", { notes: "", ...payload }));
}

export function listEvidence(applicationId: string, signal?: AbortSignal): Promise<EvidenceRecord[]> {
  return request<EvidenceRecord[]>(`/applications/${id(applicationId)}/evidence`, { signal });
}

export function createEvidence(
  applicationId: string,
  payload: {
    claim_text: string;
    evidence_type: string;
    source_url?: string;
    source_sha256?: string;
    verification_state: VerificationState;
    privacy_level: PrivacyLevel;
    notes?: string;
  }
): Promise<EvidenceRecord> {
  return request<EvidenceRecord>(
    `/applications/${id(applicationId)}/evidence`,
    jsonInit("POST", payload)
  );
}

export function linkEvidence(applicationId: string, evidenceId: string): Promise<EvidenceRecord> {
  return request<EvidenceRecord>(
    `/applications/${id(applicationId)}/evidence`,
    jsonInit("POST", { evidence_id: evidenceId })
  );
}

export function listApplicationVersions(
  applicationId: string,
  signal?: AbortSignal
): Promise<ApplicationVersionRecord[]> {
  return request<ApplicationVersionRecord[]>(`/applications/${id(applicationId)}/versions`, { signal });
}

export function createResumeDraft(
  applicationId: string,
  payload: { task_id: string; finding_ids: string[]; name: string }
): Promise<ResumeRecord> {
  return request<ResumeRecord>(
    `/applications/${id(applicationId)}/resume-drafts`,
    jsonInit("POST", payload)
  );
}

export type DownloadedArtifact = {
  blob: Blob;
  filename: string;
  contentType: string;
};

function boundedFilename(value: string, fallback: string): string {
  const leaf = value.split(/[\\/]/).at(-1)?.replace(/[\u0000-\u001f\u007f]/g, "").trim() || fallback;
  if (leaf.length <= 180) return leaf;
  const extensionIndex = leaf.lastIndexOf(".");
  const extension = extensionIndex > 0 && leaf.length - extensionIndex <= 16 ? leaf.slice(extensionIndex) : "";
  return `${leaf.slice(0, 180 - extension.length)}${extension}`;
}

function responseFilename(response: Response, fallback: string): string {
  const disposition = response.headers.get("Content-Disposition") || "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const plain = disposition.match(/filename=(?:"([^"]+)"|([^;]+))/i);
  let reported = plain?.[1] || plain?.[2]?.trim() || fallback;
  if (encoded) {
    try {
      reported = decodeURIComponent(encoded);
    } catch {
      reported = fallback;
    }
  }
  return boundedFilename(reported, fallback);
}

async function downloadArtifact(
  applicationId: string,
  artifact: string,
  fallbackFilename: string
): Promise<DownloadedArtifact> {
  const response = await requireOk(
    await fetch(`${API_BASE}/applications/${id(applicationId)}/artifacts/${artifact}`)
  );
  const contentType = (response.headers.get("Content-Type") || "application/octet-stream")
    .split(";", 1)[0]
    .trim()
    .slice(0, 120) || "application/octet-stream";
  return {
    blob: await response.blob(),
    filename: responseFilename(response, fallbackFilename),
    contentType
  };
}

export const downloadJd = (applicationId: string) =>
  downloadArtifact(applicationId, "jd", "job-description.txt");
export const downloadResumePdf = (applicationId: string) =>
  downloadArtifact(applicationId, "resume-pdf", "resume.pdf");
export const downloadReport = (applicationId: string) =>
  downloadArtifact(applicationId, "report", "analysis-report.json");
export const downloadApproval = (applicationId: string) =>
  downloadArtifact(applicationId, "approval", "approval.json");
