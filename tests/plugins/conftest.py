"""Shared fixtures for the external plugin tests.

Discovery appends plugin directories to sys.path so their modules can be
imported by validate(); tests must not leak those paths or imported modules
into the next test.
"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def restore_import_state() -> None:
    """Restore sys.path and remove modules imported by the test."""
    paths = list(sys.path)
    modules = set(sys.modules)
    yield
    sys.path[:] = paths
    for name in set(sys.modules) - modules:
        sys.modules.pop(name, None)
