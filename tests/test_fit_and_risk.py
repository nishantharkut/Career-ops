from pathlib import Path

from careerops.analyzers.fit import analyze_fit
from careerops.gates.risk import evaluate_risks
from careerops.parsers.jd_parser import parse_jd


def test_fit_analyzer_reports_matches_and_gaps():
    jd = parse_jd(Path("fixtures/sample_jd.txt").read_text(encoding="utf-8"))
    resume_text = "Nishant Harkut\nSkills: Python, SQL, data analysis\nProjects: PocketBuddy"

    report = analyze_fit(jd, resume_text)

    assert "Python" in report.matched_requirements
    assert "SQL" in report.matched_requirements
    assert "Basic machine learning" in report.missing_requirements
    assert "machine learning" in report.exact_keyword_gaps
    assert report.score_parts["requirement_coverage"] < 100
    assert any("local evaluator" in item for item in report.explanations)


def test_risk_gate_blocks_unverified_metric_claim():
    report = evaluate_risks(
        resume_text="Improved extraction accuracy from 50% to 80% across 70+ payer formats.",
        claims=[
            {
                "text": "Improved extraction accuracy from 50% to 80% across 70+ payer formats.",
                "verification_state": "needs_verification",
                "public_safe": True,
            }
        ],
    )

    assert "Missing evidence" in [issue.label for issue in report.blockers]
    assert "Claim not verified" in [issue.label for issue in report.blockers]


def test_risk_gate_blocks_nda_sensitive_wording():
    report = evaluate_risks(
        resume_text="Worked on CDI testing, OIG exclusion, EOB_ERA_New, rcm_v2, and Accurio.",
        claims=[],
    )

    assert "NDA-sensitive wording" in [issue.label for issue in report.blockers]


def test_risk_gate_blocks_unsupported_quickintell_title():
    report = evaluate_risks(
        resume_text="QuickIntell -- AI Engineering Intern\nBuilt healthcare automation systems.",
        claims=[],
    )

    assert "Unsupported title" in [issue.label for issue in report.blockers]


def test_risk_gate_blocks_stale_pdf():
    report = evaluate_risks(resume_text="Nishant Harkut", claims=[], pdf_is_stale=True)

    assert "PDF is stale" in [issue.label for issue in report.blockers]


def test_clean_resume_fixture_has_no_blockers():
    report = evaluate_risks(
        resume_text=(
            "Nishant Harkut\n"
            "QuickIntell -- Software Engineering Intern, AI Systems\n"
            "PocketBuddy -- Amazon HackOn finalist project deployed on AWS."
        ),
        claims=[
            {
                "text": "PocketBuddy -- Amazon HackOn finalist project deployed on AWS.",
                "verification_state": "user_stated",
                "public_safe": True,
            }
        ],
    )

    assert report.blockers == []
