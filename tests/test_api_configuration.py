from pathlib import Path

from apps.api.careerops_api.main import default_repository


def test_default_repository_uses_environment_paths(tmp_path: Path, monkeypatch):
    database_path = tmp_path / "e2e-careerops.db"
    vault_path = tmp_path / "vault-e2e"
    monkeypatch.setenv("CAREEROPS_DB_PATH", str(database_path))
    monkeypatch.setenv("CAREEROPS_VAULT_PATH", str(vault_path))

    repository = default_repository()

    assert repository.database_path == database_path
    assert repository.vault_path == vault_path
    assert database_path.exists()
    assert (vault_path / "resumes").is_dir()
