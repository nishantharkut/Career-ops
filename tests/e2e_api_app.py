from __future__ import annotations

import json
from typing import Any, Mapping

from fastapi import FastAPI

from apps.api.careerops_api.main import create_app, default_repository
from careerops.ai.contracts import ProviderRequest
from careerops.ai.providers.mock import MockProvider
from careerops.core.repository import CareerOpsRepository


UNSAFE_METRIC = "Improved service throughput by 70+ percent without verified evidence."
RESUME_SOURCE = "Worked on healthcare automation testing and release support with evidence-backed claims only."
RESUME_REPLACEMENT = "Worked on healthcare automation testing, release support, and production software delivery."


def _mock_output(request: ProviderRequest) -> Mapping[str, Any]:
    context = json.loads(request.context_text())
    linked_evidence = context.get("linked_evidence", [])
    evidence_ids = [
        item["id"]
        for item in linked_evidence
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    evidence_refs = evidence_ids[:1]

    if request.task_type == "analyze_jd":
        return {
            "task_type": "analyze_jd",
            "normalized_title": "Data Science Intern",
            "normalized_company": "Swiggy",
            "normalized_location": "Bangalore",
            "responsibilities": [],
            "required_skills": ["Python", "SQL"],
            "preferred_skills": [],
            "keywords": ["Machine learning"],
            "uncertainties": [],
        }
    if request.task_type == "analyze_fit":
        selected_resume = context.get("selected_resume", {})
        source_text = (
            selected_resume.get("source_text", "")
            if isinstance(selected_resume, dict)
            else ""
        )
        return {
            "task_type": "analyze_fit",
            "score": 90,
            "matches": [UNSAFE_METRIC] if UNSAFE_METRIC in source_text else [],
            "gaps": [],
            "risks": [],
            "recommendations": [],
        }
    if request.task_type == "suggest_resume_edits":
        return {
            "task_type": "suggest_resume_edits",
            "suggestions": [
                {
                    "source_text": RESUME_SOURCE,
                    "replacement_text": RESUME_REPLACEMENT,
                    "reason": "Aligns the wording with the evidence",
                    "evidence_refs": evidence_refs,
                }
            ],
        }
    if request.task_type == "draft_cover_letter":
        return {
            "task_type": "draft_cover_letter",
            "plain_text": "I am excited to apply.",
            "evidence_refs": evidence_refs,
        }
    if request.task_type == "review_claims":
        return {
            "task_type": "review_claims",
            "decisions": [
                {
                    "claim_id": claim_id,
                    "decision": "supported",
                    "reason": "Matches the supplied evidence",
                    "evidence_refs": evidence_refs,
                }
                for claim_id in request.expected_claim_ids
            ],
        }
    raise ValueError("Unsupported E2E mock task")


def create_e2e_app(repository: CareerOpsRepository | None = None) -> FastAPI:
    repo = repository or default_repository()
    provider = MockProvider(_mock_output)
    availability = provider.availability()
    binding = availability.model_dump(mode="json")
    binding.pop("available")
    binding.pop("detail")
    config = repo.upsert_ai_provider_config(
        provider_kind=availability.provider_kind,
        display_name="E2E mock provider",
        privacy_mode=availability.privacy_mode.value,
        endpoint=availability.endpoint,
        identity_name=availability.identity_name,
        model=availability.model,
        enabled=True,
        availability_binding=binding,
    )
    return create_app(repo, provider_registry={config.id: provider})
