from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

from fastapi.testclient import TestClient
import pytest

from apps.api.careerops_api.main import create_app
from careerops.ai.context import build_context_packet, load_prompt_template
from careerops.ai.contracts import ProviderRequest, output_schema_for_task
from careerops.ai.providers.codex import discover_codex_providers
from careerops.core.repository import CareerOpsRepository
from careerops.core.status import PrivacyMode


pytestmark = pytest.mark.live_provider


def synthetic_analyze_jd_request(
    tmp_path: Path,
    *,
    task_id: str = "ait_synthetic_live_001",
    provider_thread_id: str | None = None,
) -> ProviderRequest:
    task_dir = tmp_path / task_id
    task_dir.mkdir(parents=True)
    prompt = load_prompt_template("analyze_jd")
    context = build_context_packet(
        "analyze_jd",
        job_description={
            "snapshot_id": "jd_synthetic_live_001",
            "text": (
                "Synthetic Systems India is hiring a Software Engineering Intern "
                "in Bengaluru. The intern will build Python services and write tests. "
                "SQL is required; FastAPI is preferred."
            ),
        },
    )
    prompt_path = task_dir / "prompt.txt"
    context_path = task_dir / "context.json"
    schema_path = task_dir / "schema.json"
    prompt_path.write_text(prompt.text, encoding="utf-8")
    context_path.write_bytes(context.canonical_bytes)
    schema_path.write_text(
        json.dumps(
            output_schema_for_task("analyze_jd"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return ProviderRequest(
        task_id=task_id,
        application_id="app_synthetic_live_001",
        task_type="analyze_jd",
        task_dir=task_dir,
        prompt_path=prompt_path,
        context_path=context_path,
        schema_path=schema_path,
        provider_thread_id=provider_thread_id,
    )


def test_codex2_real_structured_invocation_uses_only_account2(tmp_path: Path):
    if shutil.which("codex2") is None:
        pytest.skip("codex2 is absent from PATH; no fallback identity was attempted")

    environment = dict(os.environ)
    environment.pop("CAREEROPS_CODEX_COMMAND", None)
    environment["CAREEROPS_CODEX_COMMANDS"] = "codex2"
    providers = discover_codex_providers(
        environment=environment,
        timeout_seconds=120,
        session_root=tmp_path / "codex-sessions",
    )
    if not providers:
        pytest.skip(
            "codex2 failed its identity descriptor check; no fallback identity was attempted"
        )
    assert len(providers) == 1
    provider = providers[0]
    assert provider.identity_name == "codex2"

    availability = provider.availability()
    if not availability.available:
        pytest.skip(
            "codex2 failed the native identity, version, or ChatGPT sign-in check; "
            "no fallback identity was attempted"
        )
    assert availability.identity_name == "codex2"
    assert availability.privacy_mode is PrivacyMode.CLOUD_EXTERNAL
    assert availability.data_leaves_machine is True

    result = provider.run(synthetic_analyze_jd_request(tmp_path))

    assert result.structured_output.task_type == "analyze_jd"
    assert result.provider_kind == "codex"
    assert result.privacy_mode is PrivacyMode.CLOUD_EXTERNAL
    assert result.data_leaves_machine is True
    assert result.invocation.identity_name == "codex2"
    assert result.invocation.endpoint == "codex-identity://codex2"
    assert result.invocation.command[1] == "exec"
    assert result.invocation.provider_thread_mode == "started"
    assert result.invocation.provider_thread_id

    continued = provider.run(
        synthetic_analyze_jd_request(
            tmp_path,
            task_id="ait_synthetic_live_002",
            provider_thread_id=result.invocation.provider_thread_id,
        )
    )

    assert continued.structured_output.task_type == "analyze_jd"
    assert continued.invocation.provider_thread_id == result.invocation.provider_thread_id
    assert continued.invocation.provider_thread_mode == "continued"
    assert continued.invocation.command[1:3] == ("exec", "resume")


def test_managed_ollama_real_structured_invocation_requires_verified_runtime(
    tmp_path: Path,
):
    environment = dict(os.environ)
    mode = environment.get("CAREEROPS_OLLAMA_MANAGED_MODE", "").strip().casefold()
    if mode not in {"container", "windows_process"}:
        pytest.skip(
            "verified managed Ollama runtime fixture is not configured: "
            "CAREEROPS_OLLAMA_MANAGED_MODE is absent or unsupported"
        )
    expected_endpoint = (
        "http://ollama:11434"
        if mode == "container"
        else "http://127.0.0.1:11434"
    )
    if environment.get("CAREEROPS_OLLAMA_URL") != expected_endpoint:
        pytest.skip(
            "verified managed Ollama runtime fixture is unavailable: "
            "CAREEROPS_OLLAMA_URL does not match the managed mode"
        )
    if mode == "windows_process" and (
        not environment.get("CAREEROPS_OLLAMA_EXECUTABLE", "").strip()
        or not environment.get("CAREEROPS_OLLAMA_RUNTIME_ROOT", "").strip()
    ):
        pytest.skip(
            "verified Windows managed Ollama fixture requires explicit executable "
            "and runtime-root paths"
        )

    environment["CAREEROPS_CODEX_ENABLED"] = "false"
    repository = CareerOpsRepository(
        tmp_path / "data" / "careerops.db",
        tmp_path / "vault",
    )
    repository.initialize()
    app = create_app(
        repository,
        environment=environment,
        inherit_environment_auth=False,
        task_worker_count=1,
    )
    with TestClient(app):
        managed = app.state.managed_ollama_runtime
        if managed is None:
            reason = app.state.managed_ollama_error or "managed runtime startup failed"
            pytest.skip(f"verified managed Ollama runtime fixture is unavailable: {reason}")
        availability = managed.provider.availability()
        if not availability.available:
            pytest.skip(
                "verified managed Ollama runtime fixture failed live model availability"
            )
        assert availability.privacy_mode is PrivacyMode.LOCAL_PRIVATE
        assert availability.data_leaves_machine is False
        assert availability.cloud_disabled is True
        assert availability.egress_enforced is True
        assert availability.runtime_instance_id == managed.runtime_instance_id
        assert availability.runtime_handle == managed.runtime_handle

        result = managed.provider.run(synthetic_analyze_jd_request(tmp_path))

    assert result.structured_output.task_type == "analyze_jd"
    assert result.provider_kind == "ollama"
    assert result.privacy_mode is PrivacyMode.LOCAL_PRIVATE
    assert result.data_leaves_machine is False
    assert result.invocation.runtime_instance_id == managed.runtime_instance_id
    assert result.invocation.runtime_handle == managed.runtime_handle
    assert result.invocation.cloud_disabled is True
    assert result.invocation.egress_enforced is True
