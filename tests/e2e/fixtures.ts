import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

export const sampleJdPath = resolve("fixtures/sample_jd.txt");
export const cleanResumePath = resolve("fixtures/sample_resume.tex");

export function makeRiskyResume(): string {
  const directory = mkdtempSync(join(tmpdir(), "careerops-e2e-"));
  const path = join(directory, "risky_resume.tex");
  writeFileSync(
    path,
    String.raw`\documentclass[10pt]{article}
\pagestyle{empty}
\setlength{\parindent}{0pt}
\begin{document}

{\Large Nishant Harkut}\\
Software Engineering Intern Candidate\\

\section*{Experience}
QuickIntell -- Software Engineering Intern, AI Systems\\
Worked on CDI testing and release support.\\

\section*{Skills}
C++, Python, SQL, Machine Learning, Operating Systems, DBMS

\end{document}
`,
    "utf-8"
  );
  return path;
}
