import pytest

from app.config import Settings
from app.db import create_schema, make_engine, make_session_factory

# Tests run on the code's defaults, never on the developer's .env (which may say
# LLM_PROVIDER=replay or hold a real key): the same conditions as CI. Runs before any test
# module builds Settings.
Settings.model_config["env_file"] = None


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
