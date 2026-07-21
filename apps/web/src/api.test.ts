import { beforeEach, describe, expect, test, vi } from "vitest";

import {
  approvalDownloadUrl,
  createAiTask,
  getApplicationPacket,
  jdDownloadUrl,
  reportDownloadUrl,
  resolveApiBase,
  resumePdfDownloadUrl
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

  test("builds same-origin artifact download URLs", () => {
    expect(jdDownloadUrl("app_1")).toMatch(/\/applications\/app_1\/artifacts\/jd$/);
    expect(resumePdfDownloadUrl("app_1")).toMatch(/\/applications\/app_1\/artifacts\/resume-pdf$/);
    expect(reportDownloadUrl("app_1")).toMatch(/\/applications\/app_1\/artifacts\/report$/);
    expect(approvalDownloadUrl("app_1")).toMatch(/\/applications\/app_1\/artifacts\/approval$/);
  });
});
