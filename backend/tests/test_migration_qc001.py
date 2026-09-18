"""The fork's first migration: a self-link on transactions for category splits.

Loaded from disk like the other migration tests. The chain facts matter more
than the DDL: the revision must sit on its own labelled branch off upstream
085 so a later upstream rebase (which adds 086+ off the same 085) upgrades
cleanly with `alembic upgrade heads`.
"""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "qc001_transaction_parent_link.py"
)
_SPEC = importlib.util.spec_from_file_location("qc001_migration", _MIGRATION_PATH)
assert _SPEC is not None and _SPEC.loader is not None
migration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migration)


def test_revision_sits_on_the_fork_branch_off_upstream_head():
    assert migration.revision == "qc001"
    assert migration.down_revision == "085"
    assert migration.branch_labels == ("qc",)


def test_upgrade_adds_a_nullable_self_link_with_named_constraint_and_index(monkeypatch):
    add_column = Mock()
    create_foreign_key = Mock()
    create_index = Mock()
    monkeypatch.setattr(migration.op, "add_column", add_column)
    monkeypatch.setattr(migration.op, "create_foreign_key", create_foreign_key)
    monkeypatch.setattr(migration.op, "create_index", create_index)

    migration.upgrade()

    table, column = add_column.call_args.args
    assert table == "transactions"
    assert column.name == "parent_transaction_id"
    assert column.nullable is True
    create_foreign_key.assert_called_once_with(
        "fk_transactions_parent_transaction_id",
        "transactions",
        "transactions",
        ["parent_transaction_id"],
        ["id"],
        ondelete="CASCADE",
    )
    create_index.assert_called_once_with(
        "ix_transactions_parent_transaction_id",
        "transactions",
        ["parent_transaction_id"],
    )


def test_downgrade_removes_index_constraint_then_column(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(migration.op, "drop_index", lambda *a, **k: calls.append("index"))
    monkeypatch.setattr(migration.op, "drop_constraint", lambda *a, **k: calls.append("fk"))
    monkeypatch.setattr(migration.op, "drop_column", lambda *a, **k: calls.append("column"))

    migration.downgrade()

    assert calls == ["index", "fk", "column"]
