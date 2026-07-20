# CareerOps Context Reconstruction

Status: approved reconstruction basis, amended with the AI-first correction. Implementation architecture still requires user approval before the next build phase.

This document reconstructs context from the current CareerOps thread and the historical application/resume session. It is not a product design, PRD, architecture, or implementation plan.

## Reconstruction Method

- Parsed JSONL incrementally from local session files. I did not load the multi-gigabyte June 13 file into one prompt.
- Retained user messages, assistant deliverables, and user corrections separately.
- Treated user-stated facts as stronger than assistant-generated drafts.
- Treated assistant resume bullets, cover letter text, and score claims as drafts unless backed by user confirmation or repository/evidence checks.
- Treated metrics as untrusted until verified against repositories, official pages, logs, or artifacts.
- Did not use web or product design work for this reconstruction beyond source-session parsing already present in the conversations.

## Session Verification

### Accepted Sources

1. Current CareerOps source thread, prior segment
   - Path: `C:\Users\nhnis\codex-account2\.codex\sessions\2026\07\20\rollout-2026-07-20T21-59-49-019f805c-bd65-75b3-8e85-126c18da0045.jsonl`
   - Size: `5,817,092` bytes
   - SHA-256: `d4fc3cbf9999438208b30888d46cd97a6f887a14e699d3cca998cd6e76b3b9c1`
   - First timestamp: `2026-07-20T16:43:13.241Z`
   - Last timestamp: `2026-07-20T18:32:02.713Z`
   - Status: used. This contains the CareerOps design discussion, decisions, corrections, and assistant failure incident.

2. Current CareerOps source thread, active restart segment
   - Path: `C:\Users\nhnis\codex-account2\.codex\sessions\2026\07\21\rollout-2026-07-21T00-02-22-019f80cc-ef94-77b2-a5b5-827575b70644.jsonl`
   - Snapshot size at first use: `382,702` bytes
   - Snapshot SHA-256 at first use: `9350d253047ab5378f6b22826cb2d5695cb6757e7f7ccf2d50799d66d6dcb551`
   - First timestamp at first use: `2026-07-20T18:32:26.530Z`
   - Last timestamp at first use: `2026-07-20T18:35:19.236Z`
   - AI-correction snapshot size: `5,308,266` bytes
   - AI-correction snapshot SHA-256: `8bfe177ab39114daff795646213d37c772a09f3ed8c4017839bb17a532f1b1a5`
   - AI-correction first timestamp: `2026-07-20T18:32:26.530Z`
   - AI-correction last timestamp: `2026-07-20T20:17:10.473Z`
   - Status: used as a point-in-time active-session snapshot. The file was locked by the running client, so the hash was computed through a shared-read stream.

3. Historical application/resume session
   - Path: `C:\Users\nhnis\.codex\sessions\2026\06\20\rollout-2026-06-20T01-58-04-019ee191-ba3c-7610-9478-15836e4a7cbc.jsonl`
   - Size: `91,507,970` bytes
   - SHA-256: `c20a2d684b78750aaee50cba549d6337c4f1b499b8b1699d690ff749d85e580c`
   - First timestamp: `2026-06-19T20:44:20.682Z`
   - Last timestamp: `2026-07-18T14:16:29.941Z`
   - JSONL validation: 8,149 lines, 0 invalid JSON lines, 146 user messages, 751 assistant messages.
   - Marker evidence: `Swiggy`, `Goldman Sachs`, `10+`, `70+`, `Priti`, `ATS`, `90+`, and extensive resume/application turns. Exact `Resume Worded` and `90-95` phrases were not present in this file, but the file matches the reported 91.6 MB June 20 session and contains the verified Swiggy/Goldman application history.
   - Status: used as the accepted historical source.

### Rejected Or Secondary Candidates

1. User-supplied example path under `C:\Users\nhnago\...`
   - Path checked: `C:\Users\nhnago\.codex\sessions\2026\06\13\rollout-2026-06-13T19-30-56-019ec149-21c7-7371-9d83-8b9accb34936.jsonl`
   - Status: rejected because the path does not exist.

2. Large June 13 file
   - Path: `C:\Users\nhnis\.codex\sessions\2026\06\13\rollout-2026-06-13T19-30-56-019ec149-21c7-7371-9d83-8bdf8e03636d.jsonl`
   - Size: `1,441,466,352` bytes
   - SHA-256: `c5123921a7cfa69ccdff91db4972da9d3188915b3cb4123e77375152ef3f4c6e`
   - First timestamp: `2026-06-13T14:00:59.795Z`
   - Last timestamp: `2026-07-20T12:51:38.658Z`
   - Status: not used as the main historical source. It is far larger than the reported 91.6 MB June 20 session, has mixed long-running history, and should not be treated as the correct session by default.

3. June 21 Resume Worded candidate
   - Path: `C:\Users\nhnis\.codex\sessions\2026\06\21\rollout-2026-06-21T14-32-37-019ee96a-e782-7691-86f8-37957bbb23d2.jsonl`
   - Size: `13,377,013` bytes
   - SHA-256: `b94ee766856afa561d69c90aedb3364d55f69dc1b140bf988c99392db54aa4c8`
   - First timestamp: `2026-06-21T09:24:41.044Z`
   - Last timestamp: `2026-06-26T20:43:22.443Z`
   - Status: not used as the main historical source. It contains exact `Resume Worded` and ATS-related search output, but lacks the core Goldman/Priti application-history markers. It appears related to resume/ATS research, not the primary application/resume conversation.

## 1. Confirmed User Facts

- The user is Nishant Harkut. The historical session begins with the user stating this name in the DE Shaw preparation context. [HIST20:L6, HIST20:L17]
- The user is in India and is in pre-final year during the internship-search period. The current thread corrects the market to India, and the historical recruiter-reply context states pre-final-year constraints. [CUR20:L334, HIST20:L8014]
- The user is targeting major internships and wants interview calls from big firms, not generic mass applications. [CUR20:L9]
- The user has an on-campus DE Shaw Systems Intern interview/preparation track with strong emphasis on OS, DBMS, COA, CN, OOP, resume deep dive, and DSA. [HIST20:L6, HIST20:L1107]
- The user considers DSA a weaker area and wants realistic preparation rather than empty reassurance. [HIST20:L6, HIST20:L286]
- The user has used or referenced GitHub, LinkedIn, coding profiles, achievements, and prior Codex sessions as profile context. [CUR20:L9, HIST20:L6]
- The user uses C++ for DSA/OA language choices. [HIST20:L4076]
- The user has QuickIntell experience that must be represented as an internship, not a full-time job. [HIST20:L7436]
- The user says QuickIntell work involved shared QA or release areas including CDI testing, OIG exclusion, security, EOB/ERA, and patient billing, while also warning not to name NDA-sensitive modules directly. [HIST20:L6585, HIST20:L4044]
- The user has open-source application history involving Learning Unlimited and Kiwix, entered as `Open Source Developer/Contributor` in a Google form. [HIST20:L4068]
- The user has or referenced these repositories/projects as relevant resume evidence: `ArthSaathi_ETgenAI`, `Codealpha07/GitLore`, `Covid-RARS`, `PocketBuddy`, `CuKD-XAI`, and private AllGPT/QuickIntell repositories `EOB_ERA_New`, `rcm_v2`, and `Accurio`. [HIST20:L498, HIST20:L5325, HIST20:L6424]
- The user states `Covid-RARS` and `CuKD-XAI` are research-paper-related repositories with paper writing in progress. [HIST20:L5325]
- The user states PocketBuddy was built for Amazon HackOn finals and deployed on AWS. [HIST20:L5325]
- The user states Amazon HackOn 6.0 finalist and MLSS 2026 selection are true personal achievements. [HIST20:L6823, HIST20:L6872]
- The user has prior Core-Subjects material where AI/LLM systems are described with deterministic boundaries, retrieval fallbacks, and controlled use of model output. [CORE-PLAN:L115, CORE-PLAN:L121, CORE-PLAN:L432, CORE-PLAN:L853, CORE-PLAN:L894, CORE-DOSSIER:L123, CORE-DOSSIER:L226, CORE-DOSSIER:L699]

## 2. Unconfirmed Or Contradictory Facts

- Degree, institute, CGPA `9.1/10`, and 2028 graduation are present in assistant-generated artifacts and resume recommendations, but this reconstruction should verify them from the canonical resume/profile before treating them as immutable. [HIST20:L7878, HIST20:L8147]
- The QuickIntell title is unresolved. The user requested `AI Engineering Intern` for at least one resume, but assistant warned the title must match official records or be phrased safely as `Software Engineering Intern, AI Systems`. [HIST20:L5034, HIST20:L5111, HIST20:L5121]
- QuickIntell impact metrics vary across drafts: older `60 to 92%`, later `50% to 80%`, `70+` payer formats, `108` regression checks, `40+` improvements, and `23+` defects. Treat all metrics as untrusted until reverified against repos, QA sheets, PRs, logs, or user-approved evidence. [HIST20:L1024, HIST20:L6324, HIST20:L6519, HIST20:L6622, HIST20:L6746]
- The user at times asked to fabricate or create realistic numbers, but the later approved CareerOps direction requires repository inspection and evidence before accepting metrics. This should be treated as a rejected workflow, not a current requirement. [HIST20:L4904, HIST20:L6319, CUR20:L334]
- The ISAC/RIS paper or reproduction work is explicitly uncertain and should not be claimed as the user's work unless independently proven. [HIST20:L4635, HIST20:L4661]
- `Resume Worded` is a real user concern in the current CareerOps thread, but it should not be automated, scraped, or treated as an API-backed dependency. [CUR20:L334, CUR20:L427]
- Exact ATS or resume score targets like `90-95` are a user goal pattern, but no local system can truthfully reproduce Resume Worded's proprietary score. It can build a transparent local scorer and calibrate against user-provided reports. [CUR20:L427, CUR20:L481]
- The historical June 20 session contains a pasted Supabase DB password and later assistant secret-handling mistakes. The value must not be copied into CareerOps. Treat that credential as compromised and rotate if still active. [HIST20:L1805, HIST20:L1825, HIST20:L2213, HIST20:L2223]
- "Fully local/open-source resume analysis" was previously misread as "no AI." The user's later correction says that without AI the app will not work. The corrected interpretation is local/open-source AI analysis with deterministic verification, not deterministic-only analysis. [CUR21:L7, CUR21:L2207, CUR21:L2214, CUR21:L2215]
- Whether the live Codex assistant can be "attached" to CareerOps is not guaranteed as an embedded app dependency. The feasible design is a controlled Codex worker or `codex exec` integration for explicit deep-review tasks, with scoped file access and stored artifacts. [CUR21:L2214, CUR21:L2215, CORE-DOSSIER:L784, CORE-DOSSIER:L788]
- The first local AI runtime is undecided. Ollama and LM Studio are plausible targets, but neither should be assumed installed or reliable until implementation checks the machine and exposes provider status. [CUR21:L2214, CUR21:L2325]

## 3. User Preferences

- Reliability over speed. The user explicitly rejected a prototype-like system and wants something genuinely usable. [CUR20:L511]
- India-first job discovery and application workflow. [CUR20:L334, CUR20:L372]
- AI is a core requirement. The user explicitly corrected the deterministic-only app by saying nothing works without AI. [CUR21:L2197, CUR21:L2206, CUR21:L2207, CUR21:L2214, CUR21:L2215]
- Free and open-source/local resume analysis means local or user-controlled AI where possible, not paid Resume Worded automation and not hidden cloud AI calls. [CUR20:L427, CUR21:L7, CUR21:L2214]
- Strict human approval before any submission or real application action. [CUR20:L334, CUR20:L516, CUR20:L527]
- Repository inspection before accepting project metrics or resume claims. [CUR20:L334, HIST20:L378, HIST20:L669]
- Separate tracking of resume versions and application versions, including which resume was submitted to which application. [CUR20:L334]
- LaTeX-only resumes. [CUR20:L334]
- Avoid AI-sounding wording. The user repeatedly asked for non-AI-slop, humanized, concise wording. [HIST20:L4028, HIST20:L4737, HIST20:L7754, HIST20:L7965]
- Keep the product UI clean, minimal, and non-jargon-heavy. The user explicitly rejected complex UI, AI slop, and jargon. [CUR21:L414, CUR21:L415]
- Prefer maximum uptime with low or no recurring cost. The app should be hostable, not only a local process that must run endlessly on the user's machine. [CUR21:L1434, CUR21:L1435, CUR21:L1561, CUR21:L1562]
- Avoid em dashes and semicolons in resume/cover-letter writing. [HIST20:L4737, HIST20:L7335]
- Do not include NDA-sensitive module names in public application text. [HIST20:L4044]
- Use XYZ-style resume bullets where defensible: result, metric or scale, and method. [HIST20:L4839, HIST20:L6210]
- Maintain dense, one-page, readable resumes with minimal whitespace and no overflow. [HIST20:L4455, HIST20:L4993]
- Preserve content unless a change is specifically requested, especially when only CGPA or a single field is being changed. [HIST20:L3919, HIST20:L3980]
- The user wants direct, strict, experienced guidance and dislikes bloated documents or vague reassurance. [HIST20:L1123, HIST20:L1902, HIST20:L5078]

## 4. Previous Application History

- DE Shaw Systems Intern preparation. [HIST20:L6, HIST20:L1177]
  - User requested a 30-day realistic preparation path for a DE Shaw systems role. [HIST20:L6, HIST20:L286]
  - Assistant produced a deep-dive plan, progress tracker, repo-audit set, word-by-word resume grilling map, Divya Notion mapping, and a responsive study dashboard. [HIST20:L655, HIST20:L1024, HIST20:L1118, HIST20:L1177]
  - Assistant later built and deployed a Next/Supabase/Vercel DE Shaw dashboard and pushed it to GitHub. [HIST20:L2108, HIST20:L3166]
  - This produced useful evidence about what CareerOps must avoid: overbuilding, deployment complexity, secret handling risk, and responsive UI failures. [HIST20:L1805, HIST20:L2288, HIST20:L2819, HIST20:L2865]

- Google on-campus application. [HIST20:L3919, HIST20:L4084]
  - User asked to copy an existing final resume into `google_oncampus`, change only CGPA to `9.1`, and render to PDF. [HIST20:L3919, HIST20:L3980, HIST20:L4009]
  - User filled QuickIntell as the first experience and asked for a non-AI-slop job description that avoided NDA module names. [HIST20:L4017, HIST20:L4028, HIST20:L4044]
  - User listed Learning Unlimited / Kiwix as the second open-source entry and used C++ as the computer language for OA/interview consistency. [HIST20:L4068, HIST20:L4076]
  - User submitted the form. [HIST20:L4084]

- Swiggy Data Science Intern application. [HIST20:L4098, HIST20:L4281]
  - User requested a separate Swiggy Data Science Intern resume folder for a Google Form role requiring Data Science, ML or analytics experience, Python, SQL, and basic ML. [HIST20:L4098]
  - Assistant created `swiggy_ds_intern` and generated `Nishant_Harkut_Swiggy_Data_Science_Resume.pdf`. [HIST20:L4281]
  - User corrections included too much whitespace, insufficient repo inspection, uncertain ISAC ownership, stronger open-source focus, XYZ method, no em dashes or semicolons, no model-name dumping in skills, and a requested QuickIntell title change. [HIST20:L4455, HIST20:L4516, HIST20:L4635, HIST20:L4737, HIST20:L4839, HIST20:L4993, HIST20:L5034]
  - Assistant eventually advised submission but flagged the title risk. [HIST20:L5111, HIST20:L5121]

- Goldman Sachs Engineering Summer Analyst 2027. [HIST20:L5129, HIST20:L5153]
  - User described Goldman Sachs as a dream company and pointed to `C:\Users\nhnis\Desktop\Core-Subjects\goldman sachs on campus`. [HIST20:L5129]
  - Local JD evidence identified India, Bengaluru/Hyderabad, Engineering, Summer Analyst, and a role focused on scalable systems, AI/data, reliability, security, teamwork, DSA, and core fundamentals. [HIST20:L5153, HIST20:L5154]
  - Assistant created Goldman research brief, strict prep plan, resume PDF, and LaTeX source. [HIST20:L5313]
  - User approved the PocketBuddy + ArthSaathi + CuKD-XAI project lineup for the Goldman resume. [HIST20:L6164, HIST20:L6170]
  - Assistant audited repositories and produced evidence-backed Goldman resume iterations, including PocketBuddy, ArthSaathi, CuKD-XAI, QuickIntell metrics, open-source ordering, achievements, and final A4 one-page checks. [HIST20:L6161, HIST20:L6746, HIST20:L7033, HIST20:L7058, HIST20:L7156]
  - User corrections included removing a manufactured keyword banner, using `July 2026`, enforcing reverse chronological order, adding Amazon HackOn and MLSS, renaming CuKD-XAI, and adding Discrete Mathematics only. [HIST20:L6752, HIST20:L6782, HIST20:L6795, HIST20:L6823, HIST20:L6857, HIST20:L7039, HIST20:L7111]

- Goldman Sachs cover letter. [HIST20:L7162, HIST20:L8006]
  - User requested a one-page, professional, non-AI-slop cover letter PDF, sans-serif, blue/black only, with selective bolding, no logo, and later a stronger TLDR. [HIST20:L7162, HIST20:L7187, HIST20:L7208, HIST20:L7219, HIST20:L7335]
  - Assistant generated and repeatedly revised `Nishant_Harkut_Goldman_Sachs_Cover_Letter.pdf`. [HIST20:L7329, HIST20:L7430, HIST20:L7606, HIST20:L8006]
  - User rejected multiple versions as bloated, unclear, checklist-like, or AI slop. [HIST20:L7446, HIST20:L7754, HIST20:L7802, HIST20:L7854, HIST20:L7965]

- LinkedIn recruiter reply. [HIST20:L8014, HIST20:L8097]
  - User received a GenAI / LLM / Agentic AI Engineer message for Ahmedabad from a Vrinda Global recruiter and asked for authenticity assessment and reply help. [HIST20:L8014]
  - Assistant classified it as likely genuine but broad recruiter sourcing, not a verified specific vacancy, and drafted a reply saying the user is in pre-final year and can consider remote semester internship or summer internship, not full-time onsite Ahmedabad. [HIST20:L8087, HIST20:L8097]
  - Assistant recommended the Google on-campus resume as the fastest existing resume to upload but warned it was not submission-ready for GenAI/LLM without tailoring. [HIST20:L8147]

## 5. Repeated Assistant Failure Modes

- Treating a long chat as a database and mixing facts, drafts, preferences, rejected wording, and speculative metrics. [CUR20:L183, CUR20:L328]
- Giving unrelated or corrupted assistant output in the current CareerOps thread: the company-name list, `~`, and `842/900` style text. [CUR20:L558, CUR20:L591, CUR20:L664, CUR20:L686]
- Creating or referencing the wrong project path in the prior thread, including `C:\N Drive\Internships +\CareerOps`, while the approved path is `C:\N Drive\Internships + Placements\CareerOps`. [CUR20:L602, CUR21:L7]
- Misinterpreting "fully local/open-source resume analysis" as "no AI" and building a deterministic-only v0. The correction is that AI is mandatory, while deterministic checks remain evidence and safety gates. [CUR21:L7, CUR21:L2207, CUR21:L2214, CUR21:L2215]
- Making claims before adequate repository/code inspection. The user repeatedly objected to lack of repo cloning or deep code review. [HIST20:L378, HIST20:L669, HIST20:L4516]
- Overproducing documentation or bloated content instead of focused action. [HIST20:L1123, HIST20:L1902]
- Producing resumes with too much whitespace, stale renders, layout issues, or mobile/responsive UI issues. [HIST20:L4455, HIST20:L2819, HIST20:L2865]
- Missing obvious resume basics: reverse chronological order, correct dates, achievements, role labels, and avoiding generic keyword banners. [HIST20:L6752, HIST20:L6782, HIST20:L6795, HIST20:L6823]
- Using AI-slop wording, unnatural TLDRs, vague claims, or checklist-style cover-letter phrasing. [HIST20:L7754, HIST20:L7965]
- Adding risky or disliked signals, such as `Playwright` on a resume, excessive AWS-service dumping, generic model names in skills, or unsupported "realistic" numbers. [HIST20:L6260, HIST20:L6210, HIST20:L4993, HIST20:L6319]
- Mishandling secrets in historical deployment work. The transcript contains pasted Supabase credentials and an assistant command that encoded a password. This must not be repeated in CareerOps. [HIST20:L1805, HIST20:L1825, HIST20:L2213, HIST20:L2223]

## 6. Requirements For The New System

- Build a local, evidence-locked Career Operations System, not another unbounded chat that "knows everything". [CUR20:L328]
- Build an AI-assisted Career Operations System, not a deterministic-only checker. AI should perform JD understanding, semantic fit analysis, resume tailoring, draft generation, and deep review, while deterministic gates verify claims and block unsafe packets. [CUR21:L2214, CUR21:L2215, CORE-PLAN:L115, CORE-PLAN:L121, CORE-DOSSIER:L226, CORE-DOSSIER:L699]
- Maintain a canonical private profile/evidence vault separate from job descriptions, generated artifacts, and feedback. [CUR20:L347]
- Keep resume versions and application records separate but linked: job description, resume PDF/source, answers, score report, approval, submission state, and post-submission feedback. [CUR20:L334, CUR20:L516]
- Implement fully local/open-source resume analysis as local or user-controlled AI plus deterministic verification. Do not automate, scrape, or depend on Resume Worded. [CUR20:L427, CUR21:L7, CUR21:L2214]
- Model ATS behavior as an explainable ensemble rather than a single fake percentage: parsing, knockout gates, literal keyword retrieval, structured fields, semantic skill equivalence, and separate resume-quality checks. [CUR20:L445, CUR20:L462]
- Label unknowable employer-side factors such as hidden configurations, knockout thresholds, and applicant-pool-relative ranking. [CUR20:L495]
- Use India-focused discovery: company career portals and ATS feeds where allowed, and saved alerts/email or user-opened links for LinkedIn, Naukri, Unstop, Internshala, and similar platforms unless terms/API allow more. [CUR20:L372, CUR20:L471]
- Use a cloud watcher only for public job discovery and notifications. Private profile, resumes, credentials, evidence, answers, scoring, and application preparation stay local. [CUR20:L527, CUR20:L540]
- Use explicit AI provider modes: local OSS provider first, Codex worker for scoped deep review, optional cloud AI only after configuration and task approval, and deterministic fallback/verifier for safety. [CUR21:L2214, CUR21:L2325, CORE-DOSSIER:L784, CORE-DOSSIER:L788]
- Track AI provenance for every model-generated artifact: provider, model, prompt version, input artifact IDs, privacy mode, output checksum, and verifier status. [CUR21:L2214, CUR21:L2366]
- Require final user approval before any submission. [CUR20:L516, CUR20:L527]
- Inspect repositories, branches, PRs, tests, logs, and artifacts before accepting project metrics. [CUR20:L334, HIST20:L6424, HIST20:L6499]
- Treat every prior assistant/user correction as a regression test for the system: no invented metrics, no stale PDF, no wrong chronology, no title mismatch, no unsupported paper ownership, no NDA module names, no unapproved submission. [HIST20:L4635, HIST20:L5111, HIST20:L6746]
- Redact secrets from logs, prompts, and stored artifacts by default. [HIST20:L1805, HIST20:L2223]

## 7. Decisions Already Approved

- Project name/location: `C:\N Drive\Internships + Placements\CareerOps`. [CUR21:L7]
- India-focused job search. [CUR20:L334, CUR21:L7]
- Reliability over speed. [CUR20:L511, CUR21:L7]
- AI is core to the app; deterministic-only is not acceptable. [CUR21:L2197, CUR21:L2206, CUR21:L2207, CUR21:L2214, CUR21:L2215]
- Fully local/open-source resume analysis, interpreted as local/open-source AI analysis plus deterministic verification. [CUR20:L427, CUR21:L7, CUR21:L2214]
- Cloud watcher plus local private processing. [CUR20:L527, CUR20:L540, CUR21:L7]
- Separate resume and application version history. [CUR20:L334, CUR21:L7]
- Repository inspection before accepting project metrics. [CUR20:L334, CUR21:L7]
- User approval before submission. [CUR20:L516, CUR20:L527, CUR21:L7]
- PRD before implementation. [CUR20:L378, CUR21:L7]
- Do not treat the 1.44 GB June 13 session as definitely correct. Search for the 91.6 MB June 20 historical session first. [CUR21:L7]
- Clean, minimal UI with no complex AI-slop interface and no jargon-heavy product copy. [CUR21:L414, CUR21:L415]
- Hostable low/no-cost architecture with stronger uptime than an endlessly running local-only app. [CUR21:L1434, CUR21:L1435, CUR21:L1561, CUR21:L1562]

## 8. Open Decisions

- Which canonical profile fields should be stored first, and which must be manually verified from resume/profile artifacts before use. [CUR20:L328, CUR20:L347, HIST20:L7878]
- Whether the QuickIntell public title should be `Software Engineering Intern`, `AI Engineering Intern`, or a safer combined phrase. [HIST20:L5034, HIST20:L5111, HIST20:L5121]
- Which historical application artifacts should be migrated first: Google, Swiggy, Goldman resume, Goldman cover letter, DE Shaw dashboard, or LinkedIn recruiter reply. [HIST20:L4084, HIST20:L4281, HIST20:L7156, HIST20:L8006, HIST20:L8147]
- The exact local storage format for evidence, resumes, application ledgers, score reports, and backups. [CUR20:L334, CUR20:L516]
- The cloud watcher provider and deployment target. [CUR20:L527, CUR20:L540]
- Which sources are allowed for automated monitoring versus user-provided links or email alerts. [CUR20:L372, CUR20:L471]
- The scoring thresholds and calibration method for the local ATS/resume evaluator. [CUR20:L427, CUR20:L445, CUR20:L462, CUR20:L481]
- Whether the user approves the recommended first AI runtime: Ollama first, with LM Studio or another OpenAI-compatible local server later. [CUR21:L2214]
- Whether the user approves the recommended sequencing: local OSS provider first, then Codex worker for deep evidence/repository review. [CUR21:L2214, CUR21:L2215]
- Whether optional OpenAI API mode should remain deferred until the local provider and Codex worker are stable. [CUR21:L2214]
- Whether environment variables are acceptable for optional provider secrets in v1, with Windows Credential Manager deferred. [CUR21:L2214]
- Whether the user approves the recommended first AI task: JD analysis plus semantic fit review before resume tailoring, cover-letter drafting, or Codex evidence review. [CUR21:L2186, CUR21:L2187, CUR21:L2214]
- The exact LaTeX resume template baseline and how version diffs should be reviewed. [CUR20:L334, HIST20:L3919, HIST20:L3980]
- How private repository access will be authenticated, audited, cached, and redacted. [CUR20:L334, HIST20:L6424, HIST20:L6499, HIST20:L1805]
- Whether old accidental folders, especially `C:\N Drive\Internships +\CareerOps`, should be deleted, ignored, or migrated. No deletion should happen without explicit user approval. [CUR20:L602, CUR21:L7]
- Whether exposed historical credentials have already been rotated. The system should assume rotation is needed until confirmed. [HIST20:L1805, HIST20:L1825, HIST20:L2213, HIST20:L2223]

## 9. Source Index

- `CUR20`: `C:\Users\nhnis\codex-account2\.codex\sessions\2026\07\20\rollout-2026-07-20T21-59-49-019f805c-bd65-75b3-8e85-126c18da0045.jsonl`
- `CUR21`: `C:\Users\nhnis\codex-account2\.codex\sessions\2026\07\21\rollout-2026-07-21T00-02-22-019f80cc-ef94-77b2-a5b5-827575b70644.jsonl`
- `HIST20`: `C:\Users\nhnis\.codex\sessions\2026\06\20\rollout-2026-06-20T01-58-04-019ee191-ba3c-7610-9478-15836e4a7cbc.jsonl`
- `HIST13-REJECTED`: `C:\Users\nhnis\.codex\sessions\2026\06\13\rollout-2026-06-13T19-30-56-019ec149-21c7-7371-9d83-8bdf8e03636d.jsonl`
- `HIST21-SECONDARY`: `C:\Users\nhnis\.codex\sessions\2026\06\21\rollout-2026-06-21T14-32-37-019ee96a-e782-7691-86f8-37957bbb23d2.jsonl`
- `CORE-PLAN`: `C:\Users\nhnis\Desktop\Core-Subjects\DE_SHAW_SYSTEMS_INTERN_30_DAY_DEEP_DIVE_PLAN.md`
- `CORE-DOSSIER`: `C:\Users\nhnis\Desktop\Core-Subjects\DE_SHAW_PROJECT_CODE_GRILLING_DOSSIER.md`
- `CORE-README`: `C:\Users\nhnis\Desktop\Core-Subjects\github-profile-readme\README.md`

Line references in this document refer to JSONL physical line numbers in those files.
