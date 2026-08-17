from __future__ import annotations

import inspect
from pathlib import Path

from bot.services import arena_award_service
from common.models.arena import ArenaDailyAward
from common.models.arena import ArenaWeeklyAward


def test_award_settlement_merges_sources_without_losing_partial_progress() -> None:
    assert arena_award_service._merge_source(None, "arena_fund") == "arena_fund"
    assert arena_award_service._merge_source("arena_fund", "arena_fund") == "arena_fund"
    assert arena_award_service._merge_source("arena_fund", "chat_bank") == "mixed"
    assert arena_award_service._merge_source("mixed", "chat_bank") == "mixed"


def test_award_rows_have_explicit_settlement_state_and_paid_amount() -> None:
    for model in (ArenaDailyAward, ArenaWeeklyAward):
        assert model.__table__.c.settlement_status.server_default is not None
        assert model.__table__.c.paid_amount.server_default is not None
        assert model.__table__.c.payment_source.nullable is True
        assert model.__table__.c.paid_amount.nullable is False


def test_award_category_is_unique_even_if_nomination_winner_changes() -> None:
    assert "uq_arena_daily_awards_category" in {
        constraint.name for constraint in ArenaDailyAward.__table__.constraints
    }
    assert "uq_arena_weekly_awards_category" in {
        constraint.name for constraint in ArenaWeeklyAward.__table__.constraints
    }


def test_award_settlement_uses_unique_attempt_refs_and_fund_first() -> None:
    source = inspect.getsource(arena_award_service.settle_award)
    assert "fund_ledger_key}:{already_paid}:fund" in source
    assert "fund_ledger_key}:{already_paid}:bank" in source
    assert source.index("_debit_arena_fund") < source.index("pay_from_bank")


def test_award_settlement_migration_is_latest_chain_revision() -> None:
    path = Path(__file__).parents[2] / "migrations" / "versions" / "0023_arena_award_settlement.py"
    source = path.read_text(encoding="utf-8")
    assert "revision = \"0023_arena_award_settlement\"" in source
    assert "down_revision = \"0022_arena_digest_publication\"" in source
    assert "paid_amount" in source
    assert "settlement_status" in source
    assert "uq_arena_daily_awards_category" in source
    assert "uq_arena_weekly_awards_category" in source
