import pytest

from app.db import migrate, session_factory
from app.services.sources import seed_sources


@pytest.fixture
def db_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'test.sqlite'}"
    migrate(url)
    return url


@pytest.fixture
def s(db_url):
    session = session_factory(db_url)()
    seed_sources(session)
    session.commit()
    yield session
    session.close()
