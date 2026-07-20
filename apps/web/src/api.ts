export type ApplicationRecord = {
  id: string;
  company: string;
  role_title: string;
  location: string;
  status: string;
  selected_resume_version_id: string | null;
  latest_report_id: string | null;
  approval_id: string | null;
  submission_state: string;
};

export type ResumeRecord = {
  id: string;
  name: string;
  role_family: string;
  tex_path: string;
  render_status: string;
  parse_status: string;
  pdf_path: string | null;
  pdf_sha256: string | null;
  page_count: number | null;
  render_error?: string;
  render_log_path?: string;
};

export type ReportRecord = {
  id: string;
  blocking_issue_count: number;
  warning_count: number;
  fit: {
    matched_requirements: string[];
    missing_requirements: string[];
    exact_keyword_gaps: string[];
    summary_score: number;
  };
  risks: {
    blockers: RiskIssue[];
    warnings: RiskIssue[];
  };
};

export type RiskIssue = {
  severity: string;
  label: string;
  source: string;
  why: string;
  next_action: string;
};

export type ApprovalRecord = {
  id: string;
  approval_note: string;
  pdf_sha256: string;
};

const API_BASE = import.meta.env.VITE_CAREEROPS_API ?? "http://127.0.0.1:8765";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      message = body.detail ?? message;
    } catch {
      message = "Request failed.";
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export function listApplications(): Promise<ApplicationRecord[]> {
  return request<ApplicationRecord[]>("/applications");
}

export function createApplication(payload: {
  company: string;
  role_title: string;
  location: string;
  source_url: string;
  jd_text: string;
}): Promise<ApplicationRecord> {
  return request<ApplicationRecord>("/applications", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
}

export function uploadResume(
  applicationId: string,
  payload: { name: string; role_family: string; file: File }
): Promise<ResumeRecord> {
  const form = new FormData();
  form.set("name", payload.name);
  form.set("role_family", payload.role_family);
  form.set("tex_file", payload.file);
  return request<ResumeRecord>(`/applications/${applicationId}/resume`, {
    method: "POST",
    body: form
  });
}

export function renderResume(resumeId: string): Promise<ResumeRecord> {
  return request<ResumeRecord>(`/resumes/${resumeId}/render`, { method: "POST" });
}

export function analyzeApplication(applicationId: string): Promise<ReportRecord> {
  return request<ReportRecord>(`/applications/${applicationId}/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ claims: [] })
  });
}

export function approveApplication(applicationId: string, reportId: string, note: string): Promise<ApprovalRecord> {
  return request<ApprovalRecord>(`/applications/${applicationId}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ report_id: reportId, note })
  });
}

export function markSubmitted(applicationId: string, note: string): Promise<ApplicationRecord> {
  return request<ApplicationRecord>(`/applications/${applicationId}/mark-submitted`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note })
  });
}
