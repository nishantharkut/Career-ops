import { mkdirSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";

const API_ORIGIN = "http://127.0.0.1:8765";
const CONTROL_HEADERS = { "X-CareerOps-E2E-Control": "careerops-e2e-control-v1" };
const BASELINE_PATH = resolve("test-results/e2e-root-baseline.json");

function e2eTempRoots(): string[] {
  return readdirSync(tmpdir(), { withFileTypes: true })
    .filter((entry) => entry.name.startsWith("careerops-e2e-") && (entry.isDirectory() || entry.isSymbolicLink()))
    .map((entry) => entry.name)
    .sort();
}

export default async function globalSetup(): Promise<void> {
  const response = await fetch(`${API_ORIGIN}/__e2e__/control`, { headers: CONTROL_HEADERS });
  if (!response.ok) throw new Error(`E2E control endpoint returned ${response.status}`);
  const control = await response.json() as { run_name: string };
  const roots = e2eTempRoots();
  if (!roots.includes(control.run_name)) {
    throw new Error(`Active E2E root ${control.run_name} was not found under OS temp`);
  }
  mkdirSync(dirname(BASELINE_PATH), { recursive: true });
  writeFileSync(BASELINE_PATH, JSON.stringify({ roots, runName: control.run_name }), "utf-8");
}
