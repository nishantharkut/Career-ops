import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest

from apps.api.careerops_api.main import BasicAuthConfig, create_app
from careerops.core.repository import CareerOpsRepository


ROOT = Path(__file__).resolve().parents[1]


def compose_config() -> dict:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    environment = {
        **os.environ,
        "CAREEROPS_AUTH_USERNAME": "compose-test-user",
        "CAREEROPS_AUTH_PASSWORD": "compose-test-password-with-32-characters",
    }
    completed = subprocess.run(
        [docker, "compose", "-f", str(ROOT / "compose.yaml"), "config", "--format", "json"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def make_repo(tmp_path: Path) -> CareerOpsRepository:
    repository = CareerOpsRepository(tmp_path / "careerops.db", tmp_path / "vault")
    repository.initialize()
    return repository


def test_cors_allows_configured_hosted_origin(tmp_path: Path):
    client = TestClient(create_app(make_repo(tmp_path), allowed_origins=["https://careerops.example"]))

    response = client.options(
        "/applications",
        headers={
            "Origin": "https://careerops.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://careerops.example"


def test_basic_auth_protects_private_routes_and_leaves_health_public(tmp_path: Path):
    client = TestClient(create_app(make_repo(tmp_path), auth=BasicAuthConfig("nishant", "secret")))

    assert client.get("/health").status_code == 200
    blocked = client.get("/applications")
    allowed = client.get("/applications", auth=("nishant", "secret"))

    assert blocked.status_code == 401
    assert blocked.headers["www-authenticate"] == 'Basic realm="CareerOps"'
    assert allowed.status_code == 200


def test_basic_auth_allows_cors_preflight_for_hosted_frontend(tmp_path: Path):
    client = TestClient(
        create_app(
            make_repo(tmp_path),
            auth=BasicAuthConfig("nishant", "secret"),
            allowed_origins=["https://careerops.example"],
        )
    )

    response = client.options(
        "/applications",
        headers={
            "Origin": "https://careerops.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://careerops.example"


def test_create_app_can_explicitly_disable_ambient_auth(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv("CAREEROPS_REQUIRE_AUTH", "true")
    monkeypatch.setenv("CAREEROPS_AUTH_USERNAME", "ambient-user")
    monkeypatch.setenv("CAREEROPS_AUTH_PASSWORD", "ambient-password")

    client = TestClient(
        create_app(
            make_repo(tmp_path),
            provider_registry={},
            inherit_environment_auth=False,
        )
    )

    assert client.get("/applications").status_code == 200


def test_api_serves_built_react_app_from_same_origin(tmp_path: Path):
    static_dir = tmp_path / "dist"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<main>CareerOps app shell</main>", encoding="utf-8")
    (assets_dir / "app.js").write_text("console.log('careerops')", encoding="utf-8")

    client = TestClient(create_app(make_repo(tmp_path), static_dir=static_dir))

    root = client.get("/")
    client_route = client.get("/desk/app_1")
    asset = client.get("/assets/app.js")

    assert root.status_code == 200
    assert "CareerOps app shell" in root.text
    assert client_route.status_code == 200
    assert "CareerOps app shell" in client_route.text
    assert asset.status_code == 200
    assert "careerops" in asset.text


def test_compose_persists_private_state_backups_and_local_model_data():
    config = compose_config()
    services = config["services"]
    careerops = services["careerops"]
    ollama = services["ollama"]

    assert careerops["restart"] == "unless-stopped"
    assert ollama["restart"] == "unless-stopped"
    careerops_mounts = {item["target"]: item for item in careerops["volumes"]}
    assert set(careerops_mounts) >= {"/app/data", "/app/vault", "/app/backups"}
    assert all(careerops_mounts[path].get("read_only", False) is False for path in careerops_mounts)
    ollama_mounts = {item["target"]: item for item in ollama["volumes"]}
    assert "/root/.ollama" in ollama_mounts
    assert ollama_mounts["/root/.ollama"].get("read_only", False) is False


def test_compose_requires_auth_and_contains_no_source_credentials():
    config = compose_config()
    environment = config["services"]["careerops"]["environment"]
    compose_source = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    example_source = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert environment["CAREEROPS_REQUIRE_AUTH"] == "true"
    assert environment["CAREEROPS_AUTH_USERNAME"] == "compose-test-user"
    assert environment["CAREEROPS_AUTH_PASSWORD"] == "compose-test-password-with-32-characters"
    assert "nishant" not in compose_source.casefold()
    assert "nishant" not in example_source.casefold()
    assert "replace-with" not in compose_source.casefold()
    assert "replace-with" not in example_source.casefold()
    assert "CAREEROPS_AUTH_USERNAME: ${CAREEROPS_AUTH_USERNAME:?" in compose_source
    assert "CAREEROPS_AUTH_PASSWORD: ${CAREEROPS_AUTH_PASSWORD:?" in compose_source
    assert "CAREEROPS_AUTH_USERNAME=" in example_source
    assert "CAREEROPS_AUTH_PASSWORD=" in example_source


def test_compose_uses_explicit_fail_closed_provider_configuration():
    config = compose_config()
    environment = config["services"]["careerops"]["environment"]
    source = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert environment["CAREEROPS_CODEX_ENABLED"] == "false"
    assert environment["CAREEROPS_CODEX_COMMANDS"] == "codex2,codex"
    assert environment["CAREEROPS_OLLAMA_URL"] == "http://ollama:11434"
    assert environment["CAREEROPS_OLLAMA_MODEL"] == "qwen2.5:7b"
    assert environment["CAREEROPS_OLLAMA_MANAGED_MODE"] == "container"
    assert "AUTHORITY" not in source
    assert "CAPABILITY" not in source
    assert "ISOLATION_EVIDENCE" not in source
    assert "API_KEY" not in source
    assert "fallback" not in source.casefold()


def test_ollama_has_internal_no_egress_network_without_published_port():
    config = compose_config()
    services = config["services"]
    ollama = services["ollama"]
    careerops = services["careerops"]

    assert set(ollama["networks"]) == {"ollama_private"}
    assert config["networks"]["ollama_private"]["internal"] is True
    assert "ports" not in ollama
    assert "expose" in ollama and ollama["expose"] == ["11434"]
    assert set(careerops["networks"]) == {
        "attestation_private",
        "public_api",
        "ollama_private",
    }
    assert config["networks"]["public_api"].get("internal", False) is False
    assert ollama["environment"]["OLLAMA_NO_CLOUD"] == "1"
    assert ollama["environment"]["OLLAMA_HOST"] == "0.0.0.0:11434"


def test_compose_healthchecks_and_least_privilege_are_enabled():
    config = compose_config()
    for service_name in ("careerops", "ollama"):
        service = config["services"][service_name]
        assert service["healthcheck"]["test"]
        assert service["read_only"] is True
        assert "ALL" in service["cap_drop"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert any(item.startswith("/tmp:") for item in service["tmpfs"])
    assert config["services"]["careerops"]["depends_on"]["ollama"]["condition"] == "service_healthy"
    assert (
        config["services"]["careerops"]["depends_on"]["storage-init"]["condition"]
        == "service_completed_successfully"
    )
    assert (
        config["services"]["careerops"]["depends_on"]["runtime-inspector"]["condition"]
        == "service_healthy"
    )


def test_compose_runtime_inspector_uses_get_only_socket_proxy():
    config = compose_config()
    inspector = config["services"]["runtime-inspector"]
    proxy = config["services"]["docker-socket-proxy"]

    assert set(inspector["networks"]) == {"attestation_private"}
    assert config["networks"]["attestation_private"]["internal"] is True
    assert "ports" not in inspector
    assert "expose" not in inspector
    assert inspector["read_only"] is True
    assert inspector["user"] == "1000:1000"
    assert "ALL" in inspector["cap_drop"]
    assert "no-new-privileges:true" in inspector["security_opt"]
    assert not inspector.get("volumes")
    assert "environment" not in inspector
    assert inspector["depends_on"]["docker-socket-proxy"]["condition"] == "service_healthy"
    assert set(proxy["networks"]) == {"attestation_private"}
    assert proxy["environment"]["POST"] == "0"
    assert proxy["environment"]["CONTAINERS"] == "1"
    assert proxy["environment"]["NETWORKS"] == "1"
    assert proxy["environment"]["VERSION"] == "1"
    assert all(
        proxy["environment"][name] == "0"
        for name in ("AUTH", "BUILD", "COMMIT", "CONFIGS", "DISTRIBUTION", "EVENTS", "EXEC", "IMAGES", "INFO", "NODES", "PLUGINS", "SECRETS", "SERVICES", "SESSION", "SWARM", "SYSTEM", "TASKS", "VOLUMES")
    )
    assert re.fullmatch(
        r"tecnativa/docker-socket-proxy@sha256:[0-9a-f]{64}",
        proxy["image"],
    )
    socket_mount = next(item for item in proxy["volumes"] if item["target"] == "/var/run/docker.sock")
    assert socket_mount["read_only"] is True
    assert inspector["command"] == [
        "python",
        "-m",
        "careerops.operations.container_runtime_inspector",
    ]


def test_compose_remains_self_hosted_without_paid_managed_dependencies():
    config = compose_config()
    assert set(config["services"]) == {
        "careerops",
        "storage-init",
        "ollama-bootstrap",
        "ollama",
        "runtime-inspector",
        "docker-socket-proxy",
    }
    assert Path(config["services"]["careerops"]["build"]["context"]).resolve() == ROOT.resolve()
    image = config["services"]["ollama"]["image"]
    assert re.fullmatch(r"ollama/ollama@sha256:[0-9a-f]{64}", image)
    assert config["services"]["ollama-bootstrap"]["image"] == image
    assert "${CAREEROPS_OLLAMA_IMAGE" not in (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert not any(
        key in config["services"]["careerops"]["environment"]
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID")
    )


def test_compose_bootstraps_exact_model_before_no_egress_runtime():
    config = compose_config()
    services = config["services"]
    bootstrap = services["ollama-bootstrap"]
    ollama = services["ollama"]

    assert bootstrap["restart"] == "no"
    assert set(bootstrap["networks"]) == {"model_bootstrap_egress"}
    assert config["networks"]["model_bootstrap_egress"].get("internal", False) is False
    assert set(ollama["networks"]) == {"ollama_private"}
    assert config["networks"]["ollama_private"]["internal"] is True
    assert ollama["depends_on"]["ollama-bootstrap"]["condition"] == "service_completed_successfully"
    assert bootstrap["environment"]["CAREEROPS_OLLAMA_MODEL"] == "qwen2.5:7b"
    assert bootstrap["environment"]["OLLAMA_NO_CLOUD"] == "1"
    bootstrap_command = " ".join(bootstrap["command"])
    assert "attempts=0" in bootstrap_command
    assert "-ge 120" in bootstrap_command
    assert 'ollama show "$$CAREEROPS_OLLAMA_MODEL"' in bootstrap_command
    assert 'ollama pull "$$CAREEROPS_OLLAMA_MODEL"' in bootstrap_command
    health_command = " ".join(ollama["healthcheck"]["test"])
    assert "ollama list" in health_command and "exit 1" in health_command
    assert 'ollama show "$${CAREEROPS_OLLAMA_MODEL}"' in health_command
    assert "exit 2" in health_command


def test_compose_initializes_bind_storage_before_unprivileged_api():
    config = compose_config()
    services = config["services"]
    initializer = services["storage-init"]
    api = services["careerops"]

    assert initializer["restart"] == "no"
    assert initializer["network_mode"] == "none"
    assert initializer["user"] == "0:0"
    assert initializer["cap_drop"] == ["ALL"]
    assert initializer["cap_add"] == ["CHOWN"]
    assert "environment" not in initializer
    assert initializer["command"][:3] == [
        "python",
        "-m",
        "careerops.operations.storage_init",
    ]
    init_mounts = {item["target"]: item for item in initializer["volumes"]}
    assert set(init_mounts) == {"/app/data", "/app/vault", "/app/backups"}
    assert api["user"] == "1000:1000"
    assert api["depends_on"]["storage-init"]["condition"] == "service_completed_successfully"
