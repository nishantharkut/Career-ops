import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

export const sampleJdPath = resolve("fixtures/sample_jd.txt");
export const unsafeMetricClaim = "Improved service throughput by 70+ percent without verified evidence.";
export const safeResumeSource = "Worked on healthcare automation testing and release support with evidence-backed claims only.";
export const safeResumeReplacement = "Worked on healthcare automation testing, release support, and production software delivery.";

export function writeCleanResume(path: string): string {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(
    path,
    String.raw`\documentclass[10pt]{article}
\pagestyle{empty}
\setlength{\parindent}{0pt}
\begin{document}

{\Large CareerOps Test Candidate}\\
Software Engineering Intern Candidate\\

\section*{Experience}
Example Labs -- Software Engineering Intern\\
${safeResumeSource}\\

\section*{Skills}
Python, SQL, Machine Learning, Data Analysis

\end{document}
`,
    "utf-8"
  );
  return path;
}

export function writeRiskyResume(path: string): string {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(
    path,
    String.raw`\documentclass[10pt]{article}
\pagestyle{empty}
\setlength{\parindent}{0pt}
\begin{document}

{\Large CareerOps Test Candidate}\\
Software Engineering Intern Candidate\\

\section*{Experience}
Example Labs -- Software Engineering Intern\\
Worked on CDI testing and release support.\\
${unsafeMetricClaim}\\

\section*{Skills}
C++, Python, SQL, Machine Learning, Operating Systems, DBMS

\end{document}
`,
    "utf-8"
  );
  return path;
}
