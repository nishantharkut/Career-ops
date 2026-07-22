from pathlib import Path

from fastapi.testclient import TestClient

from apps.api.careerops_api.main import BasicAuthConfig, create_app
from careerops.core.repository import CareerOpsRepository


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
