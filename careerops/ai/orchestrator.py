from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import stat
from threading import Event, Thread
from typing import Any
from uuid import uuid4

from careerops.ai.context import ContextPacket, ContextPacketBuilder, load_prompt_template
from careerops.ai.contracts import (
    AnalyzeFitOutput,
    AnalyzeJdOutput,
    ClaimReviewOutput,
    CoverLetterOutput,
    InvocationMetadata,
    ProviderAvailability,
    ProviderOutputError,
    ProviderRequest,
    ProviderResult,
    ResumeEditsOutput,
    TaskOutput,
    output_schema_for_task,
    serialize_provider_output_bytes,
)
from careerops.ai.providers.base import (
    AiProvider,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from careerops.ai.verifier import BLOCKING_CODES, VerifierFinding, verify_task_output
from careerops.core.models import (
    AiArtifact,
    AiProviderConfig,
    AiTask,
)
from careerops.core.repository import (
    CareerOpsRepository,
    canonical_provider_binding,
)
from careerops.core.status import ApplicationStatus, PrivacyMode, SubmissionState


_RESUME_TASKS = frozenset(
    {"analyze_fit", "suggest_resume_edits", "draft_cover_letter"}
)
_METADATA_FIELDS = (
    "endpoint",
    "identity_name",
    "executable",
    "executable_sha256",
    "executable_version",
    "codex_home",
    "model_digest",
    "model_metadata_sha256",
    "show_metadata_sha256",
    "attestation_version",
    "runtime_instance_id",
    "runtime_handle",
    "issuer_instance_id",
    "egress_policy",
    "cloud_disabled",
    "egress_enforced",
    "isolation_evidence_sha256",
    "isolation_evidence_signature",
    "reasoning_effort",
)


class AiOrchestrator:
    def __init__(
        self,
        repository: CareerOpsRepository,
        providers: Mapping[str, AiProvider],
    ) -> None:
        self._repository = repository
        self._providers = dict(providers)

    def register_provider(self, provider_config_id: str, provider: AiProvider) -> None:
        if not provider_config_id or provider is None:
            raise ValueError("AI provider registration was invalid")
        self._providers[provider_config_id] = provider

    def enqueue(
        self,
        application_id: str,
        task_type: str,
        provider_config_id: str,
        input_artifact_ids: Sequence[str] = (),
        *,
        cloud_transfer_acknowledged: bool = False,
    ) -> AiTask:
        application = self._repository.get_application(application_id)
        if (
            application.approval_id is not None
            or application.status
            in {ApplicationStatus.APPROVED, ApplicationStatus.SUBMITTED_MANUAL}
            or application.submission_state != SubmissionState.NOT_SUBMITTED
        ):
            raise ValueError("Application is read-only")

        config = self._repository.get_ai_provider_config(provider_config_id)
        if not config.enabled:
            raise ValueError("AI provider configuration is disabled")
        provider = self._providers.get(provider_config_id)
        if provider is None:
            raise ValueError("Exact AI provider configuration is not registered")
        availability = provider.availability()
        if not self._availability_matches(config, availability):
            raise ValueError("AI provider metadata does not match its configuration")
        checked_binding, _binding_json, checked_binding_sha256 = (
            canonical_provider_binding(self._availability_binding(availability))
        )

        template = load_prompt_template(task_type)
        self._build_context(application.id, task_type)
        return self._repository.create_ai_task(
            application_id=application.id,
            task_type=task_type,
            provider_config_id=config.id,
            prompt_template_id=template.id,
            prompt_sha256=template.sha256,
            input_artifact_ids=list(input_artifact_ids),
            privacy_mode=config.privacy_mode.value,
            expected_provider_binding=checked_binding,
            expected_provider_binding_sha256=checked_binding_sha256,
            cloud_transfer_acknowledged=cloud_transfer_acknowledged,
        )

    def run(self, task_id: str) -> AiTask:
        claim_token = self._repository.claim_ai_task(task_id)
        if claim_token is None:
            return self._repository.get_ai_task(task_id)

        try:
            heartbeat_stop, heartbeat, heartbeat_errors, heartbeat_lost = (
                self._start_claim_heartbeat(task_id, claim_token)
            )
        except Exception:
            return self._fail_or_return_current(task_id, claim_token, "provider_error")
        artifacts: list[AiArtifact] = []
        findings: list[dict[str, Any]] = []
        try:
            task = self._repository.get_ai_task(task_id)
            if task.invocation_started_at is not None:
                return self._fail_or_return_current(
                    task_id, claim_token, "provider_error"
                )
            config = self._repository.get_ai_provider_config(task.provider_config_id)
            provider = self._providers.get(task.provider_config_id)
            if (
                not config.enabled
                or provider is None
                or task.privacy_mode != config.privacy_mode
                or task.provider_binding_sha256
                != config.availability_binding_sha256
                or task.provider_binding != config.availability_binding
            ):
                raise ProviderUnavailableError("Configured AI provider is unavailable")
            availability = provider.availability()
            if not self._availability_matches(config, availability):
                raise ProviderUnavailableError(
                    "AI provider availability metadata did not match"
                )

            template = load_prompt_template(task.task_type.value)
            if (
                template.id != task.prompt_template_id
                or template.sha256 != task.prompt_sha256
            ):
                raise RuntimeError("AI prompt template did not match the task")
            context = self._build_context(task.application_id, task.task_type.value)
            expected_jd_snapshot_id, expected_resume_version_id = (
                self._expected_selection(task, context)
            )
            attempt_dir = self._new_attempt_directory(task.id)
            request = self._write_provider_request(
                task,
                template.text.encode("utf-8"),
                context,
                attempt_dir,
            )
            artifacts.append(
                self._save_artifact(
                    task,
                    claim_token,
                    "context",
                    request.context_path,
                    context.canonical_bytes,
                    "1",
                )
            )
            self._require_claim_heartbeat_healthy(
                heartbeat_errors, heartbeat_lost
            )

            try:
                # The fence intentionally consumes an attempt if the process dies
                # before spawning the provider; the user must enqueue a new task.
                if not self._repository.begin_ai_task_invocation(
                    task.id, claim_token
                ):
                    return self._fail_or_return_current(
                        task_id, claim_token, "provider_error"
                    )
                result = provider.run(request)
            except ProviderOutputError:
                self._require_claim_heartbeat_healthy(
                    heartbeat_errors, heartbeat_lost
                )
                return self._block_invalid_output(
                    task,
                    claim_token,
                    expected_jd_snapshot_id,
                    expected_resume_version_id,
                )

            self._require_claim_heartbeat_healthy(
                heartbeat_errors, heartbeat_lost
            )

            self._validate_result_metadata(config, availability, result)
            self._require_claim_heartbeat_healthy(
                heartbeat_errors, heartbeat_lost
            )
            raw_bytes = self._provider_raw_bytes(result)
            raw_path = attempt_dir / "raw_output.json"
            self._atomic_write(raw_path, raw_bytes)
            artifacts.append(
                self._save_artifact(
                    task,
                    claim_token,
                    "raw_output",
                    raw_path,
                    raw_bytes,
                    "1",
                )
            )
            self._require_claim_heartbeat_healthy(
                heartbeat_errors, heartbeat_lost
            )

            verification = verify_task_output(
                task.task_type.value,
                result.structured_output,
                context=context,
            )
            if verification.output is not None:
                validated_bytes = serialize_provider_output_bytes(verification.output)
                validated_path = attempt_dir / "validated_output.json"
                self._atomic_write(validated_path, validated_bytes)
                artifacts.append(
                    self._save_artifact(
                        task,
                        claim_token,
                        "validated_output",
                        validated_path,
                        validated_bytes,
                        "1",
                    )
                )
            self._require_claim_heartbeat_healthy(
                heartbeat_errors, heartbeat_lost
            )
            for finding in verification.findings:
                findings.append(self._verifier_finding_spec(finding))

            target_status = "completed" if verification.safe else "blocked"
            success_payload: dict[str, Any] = {}
            if verification.safe and verification.output is not None:
                success_payload = self._prepare_success_effect(
                    task,
                    claim_token,
                    request.task_dir,
                    verification.output,
                    artifacts,
                    findings,
                )
            self._require_claim_heartbeat_healthy(
                heartbeat_errors, heartbeat_lost
            )
            return self._finalize_attempt(
                task,
                claim_token,
                target_status,
                expected_jd_snapshot_id,
                expected_resume_version_id,
                findings,
                success_payload,
                "" if verification.safe else (
                    "invalid_output"
                    if verification.output is None
                    else "verification_failed"
                ),
            )
        except ProviderTimeoutError:
            return self._fail_or_return_current(task_id, claim_token, "provider_timeout")
        except ProviderUnavailableError:
            return self._fail_or_return_current(
                task_id, claim_token, "provider_unavailable"
            )
        except Exception:
            return self._fail_or_return_current(task_id, claim_token, "provider_error")
        finally:
            heartbeat_stop.set()
            heartbeat.join()

    def _start_claim_heartbeat(
        self, task_id: str, claim_token: str
    ) -> tuple[Event, Thread, list[BaseException], Event]:
        stop = Event()
        errors: list[BaseException] = []
        lost = Event()

        def renew() -> None:
            while not stop.wait(self._repository.claim_heartbeat_interval):
                try:
                    renewed = self._repository.renew_ai_task_claim(
                        task_id, claim_token
                    )
                except BaseException as exc:
                    errors.append(exc)
                    lost.set()
                    return
                if not renewed:
                    lost.set()
                    return

        heartbeat = Thread(
            target=renew,
            name=f"ai-task-heartbeat-{task_id}",
            daemon=True,
        )
        heartbeat.start()
        return stop, heartbeat, errors, lost

    @staticmethod
    def _require_claim_heartbeat_healthy(
        errors: Sequence[BaseException],
        lost: Event,
    ) -> None:
        if errors or lost.is_set():
            raise RuntimeError("AI task claim heartbeat failed")

    @staticmethod
    def _availability_matches(
        config: AiProviderConfig, availability: ProviderAvailability
    ) -> bool:
        expected_egress: bool | None = None
        if config.privacy_mode in {
            PrivacyMode.LOCAL_PRIVATE,
            PrivacyMode.VERIFIER_ONLY,
        }:
            expected_egress = False
        elif config.privacy_mode == PrivacyMode.CLOUD_EXTERNAL:
            expected_egress = True
        try:
            binding, _serialized, binding_sha256 = canonical_provider_binding(
                AiOrchestrator._availability_binding(availability)
            )
        except (TypeError, ValueError):
            return False
        return (
            availability.available
            and availability.provider_kind == config.provider_kind
            and availability.model == config.model
            and availability.privacy_mode == config.privacy_mode
            and availability.endpoint == config.endpoint
            and availability.identity_name == config.identity_name
            and binding == config.availability_binding
            and binding_sha256 == config.availability_binding_sha256
            and (
                expected_egress is None
                or availability.data_leaves_machine is expected_egress
            )
        )

    @staticmethod
    def _availability_binding(
        availability: ProviderAvailability,
    ) -> dict[str, Any]:
        binding = availability.model_dump(mode="json")
        binding.pop("available")
        binding.pop("detail")
        return binding

    def _build_context(self, application_id: str, task_type: str) -> ContextPacket:
        application = self._repository.get_application(application_id)
        jd = self._repository.get_jd_snapshot(application.jd_snapshot_id)
        jd_text = self._read_checked_utf8(jd.raw_text_path, jd.raw_text_sha256)

        resume: dict[str, str] | None = None
        if task_type in _RESUME_TASKS:
            if application.selected_resume_version_id is None:
                raise ValueError("A selected resume is required for this AI task")
            selected = self._repository.get_resume_version(
                application.selected_resume_version_id
            )
            if task_type == "analyze_fit":
                rendered = self._repository.read_verified_resume_pdf(selected.id)
                resume = {
                    "version_id": selected.id,
                    "source_kind": "rendered_pdf",
                    "source_text": rendered.text,
                    "pdf_sha256": rendered.sha256,
                }
            else:
                resume = {
                    "version_id": selected.id,
                    "source_kind": "latex",
                    "source_text": self._read_checked_utf8(
                        selected.tex_path, selected.tex_sha256
                    ),
                }

        profile_facts = self._repository.list_profile_facts()
        facts = [
            {
                "id": fact.id,
                "key": fact.fact_key,
                "value": fact.fact_value,
                "verification_state": fact.verification_state.value,
                "privacy_level": fact.privacy_level.value,
            }
            for fact in profile_facts
        ]
        evidence = [
            {
                "id": item.id,
                "summary": item.claim_text,
                "verification_state": item.verification_state.value,
                "privacy_level": item.privacy_level.value,
            }
            for item in self._repository.list_application_evidence(application.id)
        ]
        rejected = [
            {
                "id": fact.id,
                "text": fact.fact_value,
                "reason": fact.notes or "Rejected profile fact",
            }
            for fact in profile_facts
            if fact.verification_state.value == "rejected"
        ]
        preferences: Mapping[str, Any] = {}
        preferences_getter = getattr(
            self._repository, "get_writing_preferences", None
        )
        if callable(preferences_getter):
            loaded = preferences_getter(application.id)
            if loaded is not None:
                preferences = loaded
        return ContextPacketBuilder().build(
            task_type,
            job_description={"snapshot_id": jd.id, "text": jd_text},
            resume=resume,
            confirmed_facts=facts,
            evidence=evidence,
            rejected_claims=rejected,
            writing_preferences=preferences,
        )

    def _expected_selection(
        self, task: AiTask, context: ContextPacket
    ) -> tuple[str, str | None]:
        application = self._repository.get_application(task.application_id)
        context_data = context.to_dict()
        context_jd = context_data.get("job_description")
        if (
            isinstance(context_jd, dict)
            and context_jd.get("snapshot_id") != application.jd_snapshot_id
        ):
            raise ValueError("Selected job description changed during context build")
        context_resume = context_data.get("selected_resume")
        if (
            isinstance(context_resume, dict)
            and context_resume.get("version_id")
            != application.selected_resume_version_id
        ):
            raise ValueError("Selected resume changed during context build")
        expected_resume_id = (
            application.selected_resume_version_id
            if task.task_type.value in _RESUME_TASKS
            else None
        )
        return application.jd_snapshot_id, expected_resume_id

    @staticmethod
    def _read_checked_utf8(path: Path, expected_sha256: str) -> str:
        source_bytes = Path(path).read_bytes()
        if hashlib.sha256(source_bytes).hexdigest() != expected_sha256:
            raise ValueError("Repository source checksum did not match")
        return source_bytes.decode("utf-8")

    def _new_attempt_directory(self, task_id: str) -> Path:
        vault_root = self._repository.vault_path.resolve(strict=True)
        ai_root = self._repository.vault_path / "ai-tasks"
        ai_root.mkdir(parents=True, exist_ok=True)
        task_root = ai_root / task_id
        task_root.mkdir(parents=True, exist_ok=True)
        if stat.S_ISLNK(task_root.lstat().st_mode):
            raise ValueError("AI task directory must not be a symlink")
        resolved_task_root = task_root.resolve(strict=True)
        resolved_task_root.relative_to(vault_root)
        attempt_dir = resolved_task_root / f"attempt-{uuid4().hex}"
        attempt_dir.mkdir()
        return attempt_dir

    def _write_provider_request(
        self,
        task: AiTask,
        prompt_bytes: bytes,
        context: ContextPacket,
        attempt_dir: Path,
    ) -> ProviderRequest:
        template = load_prompt_template(task.task_type.value)
        if (
            hashlib.sha256(prompt_bytes).hexdigest() != template.sha256
            or template.sha256 != task.prompt_sha256
        ):
            raise ValueError("AI prompt checksum did not match")
        schema_bytes = self._canonical_json_bytes(
            output_schema_for_task(task.task_type.value)
        )
        if hashlib.sha256(schema_bytes).hexdigest() != template.schema_sha256:
            raise ValueError("AI schema checksum did not match")

        prompt_path = attempt_dir / "prompt.txt"
        context_path = attempt_dir / "context.json"
        schema_path = attempt_dir / "schema.json"
        self._atomic_write(prompt_path, prompt_bytes)
        self._atomic_write(context_path, context.canonical_bytes)
        self._atomic_write(schema_path, schema_bytes)
        expected_claim_ids: tuple[str, ...] = ()
        if task.task_type.value == "review_claims":
            expected_claim_ids = tuple(
                item["id"]
                for item in context.to_dict().get("confirmed_profile_facts", [])
            )
        return ProviderRequest(
            task_id=task.id,
            task_type=task.task_type.value,
            task_dir=attempt_dir,
            prompt_path=prompt_path,
            context_path=context_path,
            schema_path=schema_path,
            expected_claim_ids=expected_claim_ids,
        )

    @staticmethod
    def _canonical_json_bytes(value: Any) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
        try:
            with temporary.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _save_artifact(
        self,
        task: AiTask,
        claim_token: str,
        artifact_type: str,
        path: Path,
        content: bytes,
        schema_version: str,
    ) -> AiArtifact:
        return self._repository.save_ai_artifact(
            task.id,
            claim_token,
            artifact_type,
            path,
            hashlib.sha256(content).hexdigest(),
            schema_version,
        )

    @staticmethod
    def _validate_result_metadata(
        config: AiProviderConfig,
        availability: ProviderAvailability,
        result: ProviderResult,
    ) -> None:
        if (
            result.provider_kind != config.provider_kind
            or result.model != config.model
            or result.privacy_mode != config.privacy_mode
            or result.provider_kind != availability.provider_kind
            or result.model != availability.model
            or result.privacy_mode != availability.privacy_mode
            or result.data_leaves_machine != availability.data_leaves_machine
            or result.is_ai != availability.is_ai
        ):
            raise ValueError("AI provider result metadata did not match")
        invocation = result.invocation
        if not isinstance(invocation, InvocationMetadata):
            raise ValueError("AI provider invocation metadata was invalid")
        for field in _METADATA_FIELDS:
            expected = getattr(availability, field)
            observed = getattr(invocation, field)
            if observed != expected:
                raise ValueError("AI provider invocation metadata did not match")

    @staticmethod
    def _provider_raw_bytes(result: ProviderResult) -> bytes:
        raw = result.raw_output.encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        if (
            digest != result.raw_output_sha256
            or digest != result.invocation.raw_output_sha256
        ):
            raise ValueError("AI provider raw output metadata did not match")
        return raw

    @staticmethod
    def _verifier_finding_spec(finding: VerifierFinding) -> dict[str, Any]:
        location = (
            f"output item {finding.item_index}"
            if finding.item_index is not None
            else "provider output"
        )
        return {
            "finding_type": finding.code,
            "severity": (
                "blocking" if finding.code in BLOCKING_CODES else "warning"
            ),
            "claim_text": f"{finding.code} in {location}",
            "evidence_item_id": finding.evidence_item_id,
            "recommendation": finding.recommendation,
            "verifier_state": finding.verifier_state,
            "payload": {
                "claim_id": finding.claim_id,
                "code": finding.code,
                "item_index": finding.item_index,
            },
        }

    def _block_invalid_output(
        self,
        task: AiTask,
        claim_token: str,
        expected_jd_snapshot_id: str,
        expected_resume_version_id: str | None,
    ) -> AiTask:
        finding = self._verifier_finding_spec(
            VerifierFinding(
                code="schema_invalid",
                recommendation=(
                    "Discard the output and rerun the task with the matching schema."
                ),
            ),
        )
        return self._finalize_attempt(
            task,
            claim_token,
            "blocked",
            expected_jd_snapshot_id,
            expected_resume_version_id,
            [finding],
            {},
            "invalid_output",
        )

    def _finalize_attempt(
        self,
        task: AiTask,
        claim_token: str,
        status: str,
        expected_jd_snapshot_id: str,
        expected_resume_version_id: str | None,
        findings: Sequence[Mapping[str, Any]],
        success_payload: Mapping[str, Any],
        error_summary: str,
    ) -> AiTask:
        return self._repository.finalize_ai_task_attempt(
            task_id=task.id,
            claim_token=claim_token,
            terminal_status=status,
            error_summary=error_summary,
            expected_jd_snapshot_id=expected_jd_snapshot_id,
            expected_resume_version_id=expected_resume_version_id,
            findings=findings,
            success_payload=success_payload,
        )

    def _prepare_success_effect(
        self,
        task: AiTask,
        claim_token: str,
        attempt_dir: Path,
        output: TaskOutput,
        artifacts: list[AiArtifact],
        findings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if isinstance(output, AnalyzeJdOutput):
            raw_ai = {
                "normalized_company": output.normalized_company,
                "normalized_location": output.normalized_location,
                "normalized_title": output.normalized_title,
                "preferred_skills": list(output.preferred_skills),
                "required_skills": list(output.required_skills),
                "responsibilities": list(output.responsibilities),
                "keywords": list(output.keywords),
            }
            return {
                "raw_ai": raw_ai,
                "uncertainty": {
                    "items": list(output.uncertainties),
                },
            }

        if isinstance(output, AnalyzeFitOutput):
            return {
                "fit": {
                    "gaps": list(output.gaps),
                    "matches": list(output.matches),
                    "recommendations": list(output.recommendations),
                    "score": output.score,
                },
                "risks": {
                    "blockers": [],
                    "warnings": list(output.risks),
                },
            }

        if isinstance(output, ResumeEditsOutput):
            for index, suggestion in enumerate(output.suggestions):
                findings.append(
                    {
                        "finding_type": "resume_edit_suggestion",
                        "severity": "info",
                        "claim_text": suggestion.replacement_text,
                        "evidence_item_id": None,
                        "recommendation": suggestion.reason,
                        "verifier_state": "verified",
                        "payload": {
                            "evidence_refs": list(suggestion.evidence_refs),
                            "item_index": index,
                            "replacement_text": suggestion.replacement_text,
                            "source_text": suggestion.source_text,
                        },
                    }
                )
            return {}

        if isinstance(output, CoverLetterOutput):
            draft_bytes = output.plain_text.encode("utf-8")
            draft_path = attempt_dir / "draft.txt"
            self._atomic_write(draft_path, draft_bytes)
            draft = self._save_artifact(
                task,
                claim_token,
                "draft_text",
                draft_path,
                draft_bytes,
                "1",
            )
            artifacts.append(draft)
            return {}

        if isinstance(output, ClaimReviewOutput):
            states = {
                "supported": ("info", "verified"),
                "unsupported": ("warning", "rejected"),
                "needs_review": ("warning", "needs_evidence"),
                "private": ("blocking", "blocked"),
            }
            for decision in output.decisions:
                severity, verifier_state = states[decision.decision]
                findings.append(
                    {
                        "finding_type": "claim_review_decision",
                        "severity": severity,
                        "claim_text": f"{decision.claim_id}: {decision.decision}",
                        "evidence_item_id": None,
                        "recommendation": decision.reason,
                        "verifier_state": verifier_state,
                        "payload": {
                            "claim_id": decision.claim_id,
                            "decision": decision.decision,
                            "evidence_refs": list(decision.evidence_refs),
                            "reason": decision.reason,
                        },
                    }
                )
            return {}

        raise ValueError("AI task output did not match its task type")

    def _fail_or_return_current(
        self, task_id: str, claim_token: str, category: str
    ) -> AiTask:
        try:
            return self._repository.fail_ai_task(task_id, claim_token, category)
        except ValueError:
            self._repository.requeue_expired_ai_task_claim(task_id, claim_token)
            return self._repository.get_ai_task(task_id)


__all__ = ["AiOrchestrator"]
