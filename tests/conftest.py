import sys
from pathlib import Path

import pytest

# Make the project root importable when running `pytest` from the repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect, init_db  # noqa: E402
from app.web import create_app  # noqa: E402

SAMPLES = ROOT / "samples"


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(path)
    return path


@pytest.fixture()
def conn(db_path):
    connection = connect(db_path)
    yield connection
    connection.close()


@pytest.fixture()
def app(db_path):
    application = create_app(db_path=str(db_path))
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def sample_bytes(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()
