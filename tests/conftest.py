"""Shared pytest fixtures.

## Why this exists

`run_exploration` calls `federation.bind`, which writes `LONG_EXPOSURE_OPERATOR`
into the process environment on purpose — that is how the harness's own ledger
appends and its agent subprocesses agree on one operator name. In a pytest
session that means the first test to run a cycle leaks its operator into every
later test, and a test that asks `operator_name(config)` then gets the leaked
value instead of the config's.

That surfaced as a genuine failure rather than a nuisance: it is the same
"ambient identity" mechanism the split-identity defect came from, so leaving the
suite order-dependent here would hide a regression in exactly the code most
likely to regress. Cleared around every test, so each one starts with no
ambient operator.
"""

import os

import pytest

from long_exposure import federation as _federation


@pytest.fixture(autouse=True)
def _no_ambient_operator():
    saved = os.environ.get(_federation.ENV_OPERATOR)
    _federation._reset_binding()
    try:
        yield
    finally:
        _federation._reset_binding()
        if saved is not None:
            os.environ[_federation.ENV_OPERATOR] = saved
