"""Fixtures shared by the session_query test suite."""

from __future__ import annotations

import pytest

from deeppyro.session import SessionStore
from deeppyro.session_query import SessionQuery
from tests.session.support import FIXTURES, SESSIONS_DIR, load_manifest


@pytest.fixture(scope="session")
def fixture_store() -> SessionStore:
    if not FIXTURES.is_dir():
        pytest.skip("privacy: real-session fixtures are not committed")
    return SessionStore(root=SESSIONS_DIR)


@pytest.fixture
def query(fixture_store: SessionStore) -> SessionQuery:
    return SessionQuery(fixture_store)


@pytest.fixture(scope="session")
def manifest() -> dict:
    if not FIXTURES.is_dir():
        pytest.skip("privacy: real-session fixtures are not committed")
    return load_manifest()
