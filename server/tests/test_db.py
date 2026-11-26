"""Database engine pool/statement-timeout configuration."""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from rid_server.config import Settings
from rid_server.db import _connect_args, _pool_options


def test_pool_min_max_map_to_pool_and_overflow():
    settings = Settings(db_pool_min=3, db_pool_max=11)

    options = _pool_options(settings)

    assert options["pool_size"] == 3
    assert options["max_overflow"] == 8
    assert options["pool_pre_ping"] is True
    assert options["pool_use_lifo"] is True


def test_pool_max_cannot_be_below_min():
    with pytest.raises(
        ValidationError,
        match="RID_DB_POOL_MAX must be greater",
    ):
        Settings(db_pool_min=8, db_pool_max=4)


def test_zero_min_still_keeps_one_persistent_connection():
    settings = Settings(db_pool_min=0, db_pool_max=5)
    options = _pool_options(settings)

    assert options["pool_size"] == 1
    assert options["max_overflow"] == 4


def test_postgres_statement_timeout_is_forwarded():
    settings = Settings(db_statement_timeout_ms=1234)

    assert _connect_args(settings) == {
        "server_settings": {"statement_timeout": "1234"}
    }


def test_zero_statement_timeout_disables_setting():
    settings = Settings(db_statement_timeout_ms=0)

    assert _connect_args(settings) == {}
