import { describe, expect, test } from "vitest";

import { resolveApiBase } from "./api";

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
