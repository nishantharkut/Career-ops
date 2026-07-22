from __future__ import annotations

import hashlib
import inspect
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from threading import Event, enumerate as enumerate_threads
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

import tests.e2e_api_app as e2e_app


CONTROL_HEADERS = {"X-CareerOps-E2E-Control": "careerops-e2e-control-v1"}
RUN_NAME_PATTERN = re.compile(r"careerops-e2e-[0-9]+-[0-9a-f]{32}")


def _remove_owned_test_root(root: Path) -> None:
    if not root.exists():
        return
    resolved = root.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    assert resolved.parent == temp_root
    assert RUN_NAME_PATTERN.fullmatch(resolved.name)
    assert not resolved.is_symlink()
    shutil.rmtree(resolved)


def _new_e2e_root() -> Path:
    return Path(tempfile.gettempdir()).resolve() / (
        f"careerops-e2e-{os.getpid()}-{uuid4().hex}"
    )


def _remove_directory_link(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if os.name == "nt":
        os.rmdir(path)
    else:
        path.unlink()


@pytest.fixture
def isolated_e2e_environment(monkeypatch: pytest.MonkeyPatch):
    temp_root = Path(tempfile.gettempdir()).resolve()
    run_root = temp_root / f"careerops-e2e-{os.getpid()}-{uuid4().hex}"
    run_root.mkdir()
    monkeypatch.setenv("CAREEROPS_E2E_RUN_ROOT", str(run_root))
    monkeypatch.setenv("CAREEROPS_DB_PATH", str(run_root / "careerops.db"))
    monkeypatch.setenv("CAREEROPS_VAULT_PATH", str(run_root / "vault"))
    try:
        yield run_root
    finally:
        _remove_owned_test_root(run_root)


def test_e2e_factory_has_no_repository_injection_or_fallback():
    assert inspect.signature(e2e_app.create_e2e_app).parameters == {}


def test_e2e_factory_rejects_missing_run_root_before_creating_state(
    isolated_e2e_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("CAREEROPS_E2E_RUN_ROOT")
    database = isolated_e2e_environment / "careerops.db"

    with pytest.raises(RuntimeError, match="CAREEROPS_E2E_RUN_ROOT"):
        e2e_app.create_e2e_app()

    assert not database.exists()


def test_e2e_factory_rejects_ordinary_non_e2e_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    ordinary = tmp_path / "ordinary-career-data"
    monkeypatch.setenv("CAREEROPS_E2E_RUN_ROOT", str(ordinary))
    monkeypatch.setenv("CAREEROPS_DB_PATH", str(ordinary / "careerops.db"))
    monkeypatch.setenv("CAREEROPS_VAULT_PATH", str(ordinary / "vault"))

    with pytest.raises(RuntimeError, match="isolated OS temp directory"):
        e2e_app.create_e2e_app()

    assert not ordinary.exists()


def test_e2e_factory_rejects_traversal_even_when_it_resolves_to_valid_root(
    isolated_e2e_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    traversal = isolated_e2e_environment / ".." / isolated_e2e_environment.name
    monkeypatch.setenv("CAREEROPS_E2E_RUN_ROOT", str(traversal))

    with pytest.raises(RuntimeError, match="traversal"):
        e2e_app.create_e2e_app()


def test_e2e_factory_rejects_temp_prefix_collision(monkeypatch: pytest.MonkeyPatch):
    temp_root = Path(tempfile.gettempdir()).resolve()
    collision_parent = temp_root.parent / f"{temp_root.name}-collision-{uuid4().hex}"
    run_root = collision_parent / f"careerops-e2e-{os.getpid()}-{uuid4().hex}"
    monkeypatch.setenv("CAREEROPS_E2E_RUN_ROOT", str(run_root))
    monkeypatch.setenv("CAREEROPS_DB_PATH", str(run_root / "careerops.db"))
    monkeypatch.setenv("CAREEROPS_VAULT_PATH", str(run_root / "vault"))
    try:
        with pytest.raises(RuntimeError, match="isolated OS temp directory"):
            e2e_app.create_e2e_app()
        assert not run_root.exists()
    finally:
        if collision_parent.exists():
            assert collision_parent.parent == temp_root.parent
            assert collision_parent.name.startswith(f"{temp_root.name}-collision-")
            shutil.rmtree(collision_parent)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_e2e_factory_and_cleanup_reject_junction_without_deleting_target(
    monkeypatch: pytest.MonkeyPatch,
):
    temp_root = Path(tempfile.gettempdir()).resolve()
    target = _new_e2e_root()
    junction = _new_e2e_root()
    marker = target / "must-survive.txt"
    target.mkdir()
    marker.write_text("owned target", encoding="utf-8")
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        _remove_owned_test_root(target)
        pytest.fail(f"Could not create Windows junction: {created.stderr or created.stdout}")

    original_resolve = Path.resolve

    def reject_junction_resolution(path: Path, *args, **kwargs):
        if path == junction:
            raise AssertionError("raw junction was resolved before rejection")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", reject_junction_resolution)
    monkeypatch.setenv("CAREEROPS_E2E_RUN_ROOT", str(junction))
    monkeypatch.setenv("CAREEROPS_DB_PATH", str(junction / "careerops.db"))
    monkeypatch.setenv("CAREEROPS_VAULT_PATH", str(junction / "vault"))
    paths = e2e_app.E2ePaths(
        temp_root=temp_root,
        run_root=junction,
        database_path=junction / "careerops.db",
        vault_path=junction / "vault",
    )
    try:
        with pytest.raises(RuntimeError, match="symlink or junction"):
            e2e_app.create_e2e_app()
        with pytest.raises(RuntimeError, match="E2E run root"):
            e2e_app._remove_validated_run_root(paths)
        assert marker.read_text(encoding="utf-8") == "owned target"
    finally:
        _remove_directory_link(junction)
        _remove_owned_test_root(target)


def test_e2e_factory_rejects_symlink_without_deleting_target(
    monkeypatch: pytest.MonkeyPatch,
):
    temp_root = Path(tempfile.gettempdir()).resolve()
    target = _new_e2e_root()
    symlink = _new_e2e_root()
    marker = target / "must-survive.txt"
    target.mkdir()
    marker.write_text("owned target", encoding="utf-8")
    try:
        try:
            symlink.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"Directory symlinks are unavailable: {exc}")
        monkeypatch.setenv("CAREEROPS_E2E_RUN_ROOT", str(symlink))
        monkeypatch.setenv("CAREEROPS_DB_PATH", str(symlink / "careerops.db"))
        monkeypatch.setenv("CAREEROPS_VAULT_PATH", str(symlink / "vault"))
        paths = e2e_app.E2ePaths(
            temp_root=temp_root,
            run_root=symlink,
            database_path=symlink / "careerops.db",
            vault_path=symlink / "vault",
        )

        with pytest.raises(RuntimeError, match="symlink or junction"):
            e2e_app.create_e2e_app()
        with pytest.raises(RuntimeError, match="E2E run root"):
            e2e_app._remove_validated_run_root(paths)
        assert marker.read_text(encoding="utf-8") == "owned target"
    finally:
        _remove_directory_link(symlink)
        _remove_owned_test_root(target)


@pytest.mark.parametrize(
    ("variable", "relative_path"),
    [
        ("CAREEROPS_DB_PATH", "other.db"),
        ("CAREEROPS_VAULT_PATH", "other-vault"),
    ],
)
def test_e2e_factory_rejects_mismatched_database_or_vault(
    isolated_e2e_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    relative_path: str,
):
    monkeypatch.setenv(variable, str(isolated_e2e_environment / relative_path))

    with pytest.raises(RuntimeError, match="exact E2E location"):
        e2e_app.create_e2e_app()


def test_e2e_factory_accepts_only_valid_isolated_paths(
    isolated_e2e_environment: Path,
):
    app = e2e_app.create_e2e_app()

    with TestClient(app) as client:
        providers = client.get("/ai/providers").json()
        control = client.get("/__e2e__/control", headers=CONTROL_HEADERS)

    assert any(item["provider_kind"] == "mock" for item in providers)
    assert control.status_code == 200
    assert control.json() == {"run_name": isolated_e2e_environment.name}


def test_e2e_factory_ignores_ambient_auth_and_origins(
    isolated_e2e_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CAREEROPS_REQUIRE_AUTH", "true")
    monkeypatch.setenv("CAREEROPS_AUTH_USERNAME", "ambient-user")
    monkeypatch.setenv("CAREEROPS_AUTH_PASSWORD", "ambient-password")
    monkeypatch.setenv("CAREEROPS_ALLOWED_ORIGINS", "https://ambient.example")
    app = e2e_app.create_e2e_app()

    with TestClient(app) as client:
        applications = client.get("/applications")
        local_preflight = client.options(
            "/applications",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        ambient_preflight = client.options(
            "/applications",
            headers={
                "Origin": "https://ambient.example",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert applications.status_code == 200
    assert local_preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert "access-control-allow-origin" not in ambient_preflight.headers


def test_e2e_cleanup_requires_control_header_and_removes_only_validated_root(
    isolated_e2e_environment: Path,
):
    app = e2e_app.create_e2e_app()

    with TestClient(app) as client:
        blocked = client.post("/__e2e__/cleanup")
        assert blocked.status_code == 404
        assert isolated_e2e_environment.exists()
        cleaned = client.post("/__e2e__/cleanup", headers=CONTROL_HEADERS)

    assert cleaned.status_code == 204
    assert not isolated_e2e_environment.exists()


def test_e2e_cleanup_fails_closed_until_blocked_worker_quiesces(
    isolated_e2e_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    entered = Event()
    release = Event()
    original_output = e2e_app._mock_output

    def blocked_output(request):
        entered.set()
        if not release.wait(timeout=10):
            raise RuntimeError("blocked E2E provider timed out")
        return original_output(request)

    monkeypatch.setattr(e2e_app, "_mock_output", blocked_output)
    app = e2e_app.create_e2e_app()
    repository = app.state.repository
    application = repository.create_application(
        company="Example Labs",
        role_title="Test Engineer",
        location="Bengaluru",
        source_url="https://example.com/job",
        jd_text="Company: Example Labs\nTitle: Test Engineer\n",
    )
    provider_config = repository.list_ai_provider_configs()[0]

    with TestClient(app) as client:
        launched = client.post(
            f"/applications/{application.id}/ai-tasks",
            json={
                "task_type": "analyze_jd",
                "provider_config_id": provider_config.id,
            },
        )
        assert launched.status_code == 202
        assert entered.wait(timeout=2)

        blocked = client.post("/__e2e__/cleanup", headers=CONTROL_HEADERS)
        root_survived = isolated_e2e_environment.exists()
        worker_survived = any(
            thread.is_alive()
            and thread.name.startswith(app.state.task_runner.thread_name_prefix)
            and "worker" in thread.name
            for thread in enumerate_threads()
        )
        release.set()

        assert blocked.status_code == 503
        assert blocked.json() == {"detail": "E2E task runner did not quiesce"}
        assert root_survived
        assert worker_survived

        cleaned = client.post("/__e2e__/cleanup", headers=CONTROL_HEADERS)

        assert cleaned.status_code == 204
        assert not isolated_e2e_environment.exists()


def _create_selected_resume(app) -> tuple[str, Path, str]:
    repository = app.state.repository
    application = repository.create_application(
        company="Example Labs",
        role_title="Test Engineer",
        location="Bengaluru",
        source_url="https://example.com/job",
        jd_text="Company: Example Labs\nTitle: Test Engineer\n",
    )
    source_dir = repository.vault_path / "resumes" / application.id
    source_dir.mkdir(parents=True)
    source = source_dir / "resume.tex"
    source_bytes = b"verified parent source"
    source.write_bytes(source_bytes)
    digest = hashlib.sha256(source_bytes).hexdigest()
    resume = repository.create_resume_version(
        name="Parent resume",
        role_family="software",
        tex_path=source,
        tex_sha256=digest,
    )
    repository.select_resume(application.id, resume.id)
    return resume.id, source, digest


def test_e2e_source_probe_hashes_actual_owned_bytes_and_detects_tampering(
    isolated_e2e_environment: Path,
):
    app = e2e_app.create_e2e_app()
    resume_id, source, expected_digest = _create_selected_resume(app)

    with TestClient(app) as client:
        before = client.get(
            f"/__e2e__/resumes/{resume_id}/source-sha256",
            headers=CONTROL_HEADERS,
        )
        source.write_bytes(b"tampered parent source")
        assert app.state.repository.get_resume_version(resume_id).tex_sha256 == expected_digest
        tampered = client.get(
            f"/__e2e__/resumes/{resume_id}/source-sha256",
            headers=CONTROL_HEADERS,
        )

    assert before.status_code == 200
    assert before.json() == {"sha256": expected_digest}
    assert tampered.status_code == 409
    assert tampered.json() == {"detail": "Resume source integrity check failed"}


def test_e2e_source_probe_rejects_source_outside_vault(
    isolated_e2e_environment: Path,
):
    app = e2e_app.create_e2e_app()
    resume_id, _source, _digest = _create_selected_resume(app)
    outside = isolated_e2e_environment / "outside.tex"
    outside.write_bytes(b"outside source")
    with app.state.repository.connection() as connection:
        connection.execute(
            "UPDATE resume_versions SET tex_path = ? WHERE id = ?",
            (str(outside), resume_id),
        )

    with TestClient(app) as client:
        response = client.get(
            f"/__e2e__/resumes/{resume_id}/source-sha256",
            headers=CONTROL_HEADERS,
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "Resume source integrity check failed"}


def test_playwright_timeout_is_scoped_to_long_workflow():
    root = Path(__file__).resolve().parents[1]
    config = (root / "playwright.config.ts").read_text(encoding="utf-8")
    workflow = (root / "tests" / "e2e" / "ai-application-flow.spec.ts").read_text(
        encoding="utf-8"
    )

    assert "timeout: 120_000" in config
    assert "timeout: 240_000" not in config
    assert "test.setTimeout(240_000)" in workflow


def test_e2e_launcher_sets_exact_validated_sentinel_and_paths():
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "run-e2e-api.ps1").read_text(encoding="utf-8")

    assert '$env:CAREEROPS_E2E_RUN_ROOT = $RunRoot' in script
    assert '$env:CAREEROPS_DB_PATH = Join-Path $RunRoot "careerops.db"' in script
    assert '$env:CAREEROPS_VAULT_PATH = Join-Path $RunRoot "vault"' in script
    assert "^careerops-e2e-[0-9]+-[0-9a-f]{32}$" in script


def test_playwright_config_owns_root_baseline_and_cleanup_hooks():
    root = Path(__file__).resolve().parents[1]
    config = (root / "playwright.config.ts").read_text(encoding="utf-8")

    assert 'globalSetup: "./tests/e2e/global-setup.ts"' in config
    assert 'globalTeardown: "./tests/e2e/global-teardown.ts"' in config
    assert (root / "tests" / "e2e" / "global-setup.ts").is_file()
    assert (root / "tests" / "e2e" / "global-teardown.ts").is_file()
    assert (root / "tests" / "e2e" / "cleanup-failure-probe.spec.ts").is_file()


def test_resume_fixtures_are_written_only_to_playwright_output_paths():
    root = Path(__file__).resolve().parents[1]
    fixtures = (root / "tests" / "e2e" / "fixtures.ts").read_text(encoding="utf-8")
    ai_flow = (root / "tests" / "e2e" / "ai-application-flow.spec.ts").read_text(
        encoding="utf-8"
    )
    desk = (root / "tests" / "e2e" / "application-desk.spec.ts").read_text(
        encoding="utf-8"
    )

    assert "mkdtemp" not in fixtures
    assert "tmpdir" not in fixtures
    assert "testInfo.outputPath" in ai_flow
    assert "testInfo.outputPath" in desk


def test_long_workflow_uses_guarded_actual_source_hash_probe():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / "tests" / "e2e" / "ai-application-flow.spec.ts").read_text(
        encoding="utf-8"
    )

    assert "/__e2e__/resumes/" in workflow
    assert "parentSourceSha256" in workflow
