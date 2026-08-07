from __future__ import annotations

import importlib.util
from pathlib import Path


_MIGRATION_PATH = (
    Path(__file__).parents[2] / "migrations" / "versions" / "0019_arena_phase_2.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("arena_phase_2_migration", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_arena_migration_is_attached_to_current_head_chain() -> None:
    migration = _load_migration()

    assert migration.revision == "0019_arena_phase_2"
    assert migration.down_revision == "0018_exchange_stuck_alert"
    assert migration.branch_labels is None
    assert migration.depends_on is None


def test_arena_migration_declares_all_phase_two_tables() -> None:
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    expected_tables = {
        "arena_profiles",
        "arena_fighter_progress",
        "arena_funds",
        "arena_matches",
        "arena_fund_ledger",
        "arena_match_events",
        "arena_leaderboard_snapshots",
        "arena_daily_awards",
        "arena_weekly_awards",
        "arena_admin_audit",
    }

    for table_name in expected_tables:
        assert f'"{table_name}"' in source


def test_arena_migration_drops_dependents_before_parents() -> None:
    source = _MIGRATION_PATH.read_text(encoding="utf-8")

    assert source.index('op.drop_table("arena_admin_audit")') < source.index(
        'op.drop_table("arena_matches")'
    )
    assert source.index('op.drop_table("arena_match_events")') < source.index(
        'op.drop_table("arena_matches")'
    )
    assert source.index('op.drop_table("arena_fund_ledger")') < source.index(
        'op.drop_table("arena_matches")'
    )
    assert source.index('op.drop_table("arena_matches")') < source.index(
        'op.drop_table("arena_profiles")'
    )
