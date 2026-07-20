from pathlib import Path

from careerops.core.database import connect_database, initialize_database


def test_database_uses_local_durability_pragmas(tmp_path: Path):
    database_path = tmp_path / "careerops.db"

    initialize_database(database_path)

    with connect_database(database_path) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
