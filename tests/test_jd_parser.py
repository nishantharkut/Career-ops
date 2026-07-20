from pathlib import Path

from careerops.parsers.jd_parser import parse_jd


def test_parse_jd_extracts_structured_requirements():
    jd_text = Path("fixtures/sample_jd.txt").read_text(encoding="utf-8")

    parsed = parse_jd(jd_text)

    assert parsed.company == "Swiggy"
    assert parsed.role_title == "Data Science Intern"
    assert parsed.location == "Bangalore"
    assert parsed.work_mode == "Remote or WFO Bangalore"
    assert parsed.duration == "6 months"
    assert "Python" in parsed.required_skills
    assert "SQL" in parsed.required_skills
    assert "Basic machine learning" in parsed.required_skills
    assert "Statistics" in parsed.preferred_skills
    assert any("dashboards" in item for item in parsed.responsibilities)
    assert any("Pre-final year" in item for item in parsed.eligibility)
    assert "python" in parsed.exact_keywords
    assert "sql" in parsed.exact_keywords
    assert "machine learning" in parsed.exact_keywords


def test_parse_jd_keeps_missing_fields_unknown():
    parsed = parse_jd("Required skills:\n- C++\n- Operating systems")

    assert parsed.company == "Unknown"
    assert parsed.role_title == "Unknown"
    assert parsed.location == "Unknown"
    assert parsed.work_mode == "Unknown"
    assert parsed.duration == "Unknown"
    assert parsed.required_skills == ["C++", "Operating systems"]
    assert parsed.preferred_skills == []


def test_parse_jd_finds_knockout_conditions():
    parsed = parse_jd(
        "Eligibility:\n"
        "- Graduation year 2027 only.\n"
        "- Must be available for onsite internship in Hyderabad.\n"
    )

    assert "Graduation year 2027 only." in parsed.knockout_conditions
    assert "Must be available for onsite internship in Hyderabad." in parsed.knockout_conditions
