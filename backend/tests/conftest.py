import pytest

from app.db import create_schema, make_engine, make_session_factory


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    create_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with make_session_factory(engine)() as session:
        yield session
