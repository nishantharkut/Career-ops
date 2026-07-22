import { expect, test } from "@playwright/test";

test("global teardown removes state after a failing run", async () => {
  test.skip(process.env.CAREEROPS_E2E_CLEANUP_FAILURE_PROBE !== "1", "Explicit lifecycle probe only");
  expect("intentional failure").toBe("cleanup must still run");
});
