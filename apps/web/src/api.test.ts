import { beforeEach, describe, expect, test, vi } from "vitest";

import {
  createAiTask,
  downloadApproval,
  downloadJd,
  downloadReport,
  downloadResumePdf,
  getApplicationPacket,
  listEvidence,
  resolveApiBase,
  type EvidenceRecord
} from "./api";

describe("API base resolution", () => {
  test("uses localhost only during Vite development", () => {
    expect(resolveApiBase({ DEV: true })).toBe("http://127.0.0.1:8765");
  });

  test("uses same-origin requests for hosted production builds", () => {
    expect(resolveApiBase({ DEV: false })).toBe("");
  });

  test("uses explicit configured API origin when provided", () => {
    expect(resolveApiBase({ DEV: false, VITE_CAREEROPS_API: "https://api.example.com" })).toBe("https://api.example.com");
  });
});

describe("workbench API client", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  test("fetches a packet with the supplied cancellation signal", async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ application: { id: "app_1" } }) }));
    vi.stubGlobal("fetch", fetchMock);

    await getApplicationPacket("app_1", controller.signal);

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/applications\/app_1\/packet$/),
      expect.objectContaining({ signal: controller.signal })
    );
  });

  test("creates an AI task without changing the selected provider", async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ id: "ait_1", status: "pending" }) }));
    vi.stubGlobal("fetch", fetchMock);

    await createAiTask("app_1", {
      task_type: "analyze_fit",
      provider_config_id: "provider_codex2",
      input_artifact_ids: []
    });

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/applications\/app_1\/ai-tasks$/),
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          task_type: "analyze_fit",
          provider_config_id: "provider_codex2",
          input_artifact_ids: []
        })
      })
    );
  });

  test("does not surface non-string API detail objects", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 409,
        statusText: "Conflict",
        json: async () => ({ detail: { provider_binding: "sensitive" } })
      }))
    );

    await expect(getApplicationPacket("app_1")).rejects.toThrow("409 Conflict");
  });

  test("downloads every artifact through typed clients with bounded response metadata", async () => {
    const paths: string[] = [];
    const longName = `${"a".repeat(220)}.txt`;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        paths.push(String(input));
        return new Response(new Blob(["artifact"]), {
          status: 200,
          headers: {
            "Content-Disposition": `attachment; filename="${longName}"`,
            "Content-Type": "text/plain"
          }
        });
      })
    );

    const downloads = await Promise.all([
      downloadJd("app_1"),
      downloadResumePdf("app_1"),
      downloadReport("app_1"),
      downloadApproval("app_1")
    ]);

    expect(paths).toEqual([
      expect.stringMatching(/\/applications\/app_1\/artifacts\/jd$/),
      expect.stringMatching(/\/applications\/app_1\/artifacts\/resume-pdf$/),
      expect.stringMatching(/\/applications\/app_1\/artifacts\/report$/),
      expect.stringMatching(/\/applications\/app_1\/artifacts\/approval$/)
    ]);
    for (const download of downloads) {
      expect(download.blob).toBeInstanceOf(Blob);
      expect(download.filename.length).toBeLessThanOrEqual(180);
      expect(download.filename).toMatch(/\.txt$/);
      expect(download.contentType).toBe("text/plain");
    }
  });

  test("uses safe response detail for an unavailable artifact", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ detail: "Resume PDF is not available." }), {
          status: 404,
          statusText: "Not Found",
          headers: { "Content-Type": "application/json" }
        })
      )
    );

    await expect(downloadResumePdf("app_1")).rejects.toThrow("Resume PDF is not available.");
  });

  test("accepts evidence records with a null source URL", async () => {
    const record: EvidenceRecord = {
      id: "evd_1",
      claim_text: "Built a service",
      evidence_type: "project",
      source_url: null,
      source_sha256: null,
      verification_state: "verified",
      privacy_level: "private",
      publishable: false,
      notes: "",
      created_at: "",
      updated_at: ""
    };
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => [record] })));

    await expect(listEvidence("app_1")).resolves.toEqual([expect.objectContaining({ source_url: null })]);
  });
});
