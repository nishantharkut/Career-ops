import { readFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";

const API_ORIGIN = "http://127.0.0.1:8765";
const CONTROL_HEADERS = { "X-CareerOps-E2E-Control": "careerops-e2e-control-v1" };
const BASELINE_PATH = resolve("test-results/e2e-root-baseline.json");

type Baseline = { roots: string[]; runName: string };

function e2eTempRoots(): string[] {
  return readdirSync(tmpdir(), { withFileTypes: true })
    .filter((entry) => entry.name.startsWith("careerops-e2e-") && (entry.isDirectory() || entry.isSymbolicLink()))
    .map((entry) => entry.name)
    .sort();
}

function sameRoots(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

export default async function globalTeardown(): Promise<void> {
  const baseline = JSON.parse(readFileSync(BASELINE_PATH, "utf-8")) as Baseline;
  const beforeCleanup = e2eTempRoots();
  let cleanupError = "";
  try {
    const response = await fetch(`${API_ORIGIN}/__e2e__/cleanup`, {
      method: "POST",
      headers: CONTROL_HEADERS
    });
    if (response.status !== 204) cleanupError = `cleanup endpoint returned ${response.status}`;
  } catch (error) {
    cleanupError = error instanceof Error ? error.message : "cleanup request failed";
  }
  const afterCleanup = e2eTempRoots();
  const expectedAfter = baseline.roots.filter((name) => name !== baseline.runName);
  const staleBefore = baseline.roots.length - 1;
  process.stdout.write(
    `E2E temp roots: stale-before=${staleBefore}, before-cleanup=${beforeCleanup.length}, after-cleanup=${afterCleanup.length}\n`
  );
  if (cleanupError) throw new Error(cleanupError);
  if (!sameRoots(beforeCleanup, baseline.roots)) {
    throw new Error("E2E run created unmanaged temp roots");
  }
  if (!sameRoots(afterCleanup, expectedAfter)) {
    throw new Error("E2E cleanup did not restore the stale temp-root baseline");
  }
}
