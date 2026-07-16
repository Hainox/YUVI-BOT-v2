"""Интеграционные тесты duel_service (DUEL-01/DUEL-02) против живого Postgres
(фикстура `session` из tests/conftest.py) + юнит-тесты хендлеров bot/handlers/duel.py
(AsyncMock `bot` из tests/conftest.py). Доказывают:

- Эскроу ставки обеих сторон при create_duel/accept_duel только через
  economy_service (debit/credit_bank), выплата победителю — только через
  economy_service.pay_from_bank (D-06 bank-cap ядро уже покрыто 04.1-01).
- Точная формула комиссии D-04: fee = max(1, ceil(2*stake*transfer_fee_pct)).
- /duelbot (D-08): та же механика против банка (opponent_id = NULL), выигрыш/
  проигрыш чата.
- decline/cancel — полный рефанд, статус-переход как гард идемпотентности
  (форма markets_service.resolve_market/cancel_market).
- accept_duel идемпотентен на уже resolved-дуэли (повторный вызов — no-op,
  деньги не двигаются повторно).
- Исход дуэли — ТОЛЬКО через RNG-seam `duel_service._rng` (monkeypatched в
  тестах), никогда не клиентское значение (T-04.1-24).
- Хендлеры (bot/handlers/duel.py): /duel_accept и /duelbot накладывают мут на
  проигравшего через bot.restrict_chat_member(until_date≈+10мин) +
  bot.send_sticker(MUTE_STICKER_ID); /unmute админом лифтит мут
  (restrict_chat_member restoring permissions + send_sticker(UNMUTE_STICKER_ID));
  не-админ /unmute получает явный отказ без вызова restrict_chat_member (D-03).
"""

from __future__ import annotations

import math
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import bot.handlers.duel as duel_handlers
from bot.config import settings
from bot.services import duel_service
from bot.services import economy_service
from common.models.chat_bank import ChatBank
from common.models.duel import Duel
from common.models.user import User
from common.models.user_balance import UserBalance


# --- Хелперы (форма test_casino_service.py / test_economy_handlers.py) ------


async def _ensure_user(session, user_id: int, first_name: str = "Тест") -> None:
    session.add(User(id=user_id, first_name=first_name))
    await session.flush()


async def _fund(session, chat_id: int, user_id: int) -> int:
    """Заводит кошелёк (стартовый бонус economy_start_bonus) и коммитит."""
    return await economy_service.get_balance(session, chat_id, user_id)


async def _get_user_balance(session, chat_id: int, user_id: int) -> int:
    result = await session.execute(
        select(UserBalance.balance).where(
            UserBalance.chat_id == chat_id, UserBalance.user_id == user_id
        )
    )
    return result.scalar_one()


async def _get_bank_balance(session, chat_id: int) -> int:
    result = await session.execute(select(ChatBank.balance).where(ChatBank.chat_id == chat_id))
    return result.scalar_one_or_none() or 0


async def _get_duel(session, duel_id: int) -> Duel:
    return (await session.execute(select(Duel).where(Duel.id == duel_id))).scalar_one()


class _ForcedChoiceRng:
    """Тестовый RNG-стаб, monkeypatched вместо `duel_service._rng`. Форсирует
    детерминированный результат `.choice(seq)` вместо реальной случайности."""

    def __init__(self, choice_value):
        self._choice_value = choice_value

    def choice(self, seq):
        return self._choice_value


def _fake_message(
    chat_id: int,
    user_id: int,
    first_name: str,
    text: str,
    *,
    message_id: int = 1,
    reply_to_message=None,
    entities=None,
):
    """Минимальный aiogram-подобный Message для тестов тонких хендлеров duel.py
    (форма tests/test_economy_handlers.py::_fake_message)."""
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, first_name=first_name),
        message_id=message_id,
        text=text,
        reply_to_message=reply_to_message,
        entities=entities,
        answer=AsyncMock(),
        reply=AsyncMock(),
    )


# --- create_duel (эскроу ставки челленджера) --------------------------------


@pytest.mark.asyncio
async def test_create_escrows_challenger_stake(session):
    chat_id = -100910001
    challenger_id, opponent_id = 910001, 910002
    await _ensure_user(session, challenger_id, "Челленджер")
    await _ensure_user(session, opponent_id, "Оппонент")
    challenger_before = await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)
    bank_before = await _get_bank_balance(session, chat_id)

    stake = 100
    duel = await duel_service.create_duel(
        session, chat_id, challenger_id, opponent_id, stake, "test_create_duel_escrow"
    )

    assert duel.status == "pending"
    assert duel.opponent_id == opponent_id
    assert duel.challenger_id == challenger_id
    assert duel.stake == stake

    assert await _get_user_balance(session, chat_id, challenger_id) == challenger_before - stake
    # WR-04 (04.1-REVIEW): ставка НЕ заходит в общий chat_bank, пока дуэль
    # pending — иначе рефанд отмены/отклонения зависел бы от текущего
    # остатка банка, просаженного несвязанными выплатами (см. тест ниже).
    assert await _get_bank_balance(session, chat_id) == bank_before


@pytest.mark.asyncio
async def test_create_below_min_bet_raises(session):
    chat_id = -100910003
    challenger_id, opponent_id = 910003, 910004
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    balance_before = await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    with pytest.raises(duel_service.DuelError):
        await duel_service.create_duel(
            session,
            chat_id,
            challenger_id,
            opponent_id,
            settings.casino_min_bet - 1,
            "test_create_duel_below_min",
        )

    assert await _get_user_balance(session, chat_id, challenger_id) == balance_before
    duels = (
        await session.execute(select(Duel).where(Duel.challenger_id == challenger_id))
    ).scalars().all()
    assert duels == []


@pytest.mark.asyncio
async def test_create_self_duel_raises_and_does_not_escrow(session):
    """WR-01 (04.2-REVIEW): opponent_id == challenger_id must be rejected at
    the service layer (previously only guarded client-side in
    bot/handlers/duel.py, so the Mini App API had no protection at all)."""
    chat_id = -100910005
    challenger_id = 910005
    await _ensure_user(session, challenger_id)
    balance_before = await _fund(session, chat_id, challenger_id)

    with pytest.raises(duel_service.DuelError):
        await duel_service.create_duel(
            session,
            chat_id,
            challenger_id,
            challenger_id,
            100,
            "test_create_duel_self",
        )

    assert await _get_user_balance(session, chat_id, challenger_id) == balance_before
    duels = (
        await session.execute(select(Duel).where(Duel.challenger_id == challenger_id))
    ).scalars().all()
    assert duels == []


# --- accept_duel (coinflip + 5% fee, D-04) -----------------------------------


@pytest.mark.asyncio
async def test_accept_resolves_coinflip_with_fee(session, monkeypatch):
    chat_id = -100910005
    challenger_id, opponent_id = 910005, 910006
    await _ensure_user(session, challenger_id, "Челленджер")
    await _ensure_user(session, opponent_id, "Оппонент")
    challenger_before = await _fund(session, chat_id, challenger_id)
    opponent_before = await _fund(session, chat_id, opponent_id)

    stake = 100
    duel = await duel_service.create_duel(
        session, chat_id, challenger_id, opponent_id, stake, "test_accept_create"
    )

    monkeypatch.setattr(duel_service, "_rng", _ForcedChoiceRng(challenger_id))

    result = await duel_service.accept_duel(
        session, chat_id, duel.id, opponent_id, "test_accept_ref"
    )

    expected_fee = max(1, math.ceil(2 * stake * settings.transfer_fee_pct))
    expected_pot = 2 * stake - expected_fee

    assert result["status"] == "resolved"
    assert result["winner_id"] == challenger_id
    assert result["loser_id"] == opponent_id
    assert result["fee"] == expected_fee
    assert result["pot"] == expected_pot

    duel_row = await _get_duel(session, duel.id)
    assert duel_row.status == "resolved"
    assert duel_row.winner_id == challenger_id
    assert duel_row.loser_id == opponent_id
    assert duel_row.fee == expected_fee

    # net: winner +stake-fee, loser -stake
    assert (
        await _get_user_balance(session, chat_id, challenger_id)
        == challenger_before - stake + expected_pot
    )
    assert await _get_user_balance(session, chat_id, opponent_id) == opponent_before - stake


@pytest.mark.asyncio
async def test_accept_duel_bank_receives_only_fee(session, monkeypatch):
    """WR-04 (04.1-REVIEW): обе ставки временно заходят в chat_bank ровно в
    момент accept_duel (challenger'а — отложенно из create_duel, оппонента —
    через _escrow_stake), полностью выплачиваются победителю через
    pay_from_bank — банк должен прирасти РОВНО на комиссию fee, эскроу не
    должен "застревать" в банке."""
    chat_id = -100910027
    challenger_id, opponent_id = 910027, 910028
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)
    bank_before = await _get_bank_balance(session, chat_id)

    stake = 100
    duel = await duel_service.create_duel(
        session, chat_id, challenger_id, opponent_id, stake, "test_bank_fee_create"
    )

    monkeypatch.setattr(duel_service, "_rng", _ForcedChoiceRng(challenger_id))
    result = await duel_service.accept_duel(
        session, chat_id, duel.id, opponent_id, "test_bank_fee_accept"
    )

    assert await _get_bank_balance(session, chat_id) == bank_before + result["fee"]


@pytest.mark.asyncio
async def test_coinflip_is_server_side(session, monkeypatch):
    """Исход дуэли определяется ИСКЛЮЧИТЕЛЬНО RNG-seam `_rng`, не клиентским
    значением (T-04.1-24) — форсируем оппонента победителем и проверяем, что
    именно он выигрывает независимо от того, кто вызвал accept_duel."""
    chat_id = -100910007
    challenger_id, opponent_id = 910007, 910008
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    stake = 50
    duel = await duel_service.create_duel(
        session, chat_id, challenger_id, opponent_id, stake, "test_server_rng_create"
    )

    monkeypatch.setattr(duel_service, "_rng", _ForcedChoiceRng(opponent_id))

    result = await duel_service.accept_duel(
        session, chat_id, duel.id, opponent_id, "test_server_rng_accept"
    )

    assert result["winner_id"] == opponent_id
    assert result["loser_id"] == challenger_id


@pytest.mark.asyncio
async def test_accept_idempotent_on_resolved(session, monkeypatch):
    chat_id = -100910009
    challenger_id, opponent_id = 910009, 910010
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    stake = 100
    duel = await duel_service.create_duel(
        session, chat_id, challenger_id, opponent_id, stake, "test_idempotent_create"
    )

    monkeypatch.setattr(duel_service, "_rng", _ForcedChoiceRng(challenger_id))

    first = await duel_service.accept_duel(
        session, chat_id, duel.id, opponent_id, "test_idempotent_accept_1"
    )
    balance_after_first = await _get_user_balance(session, chat_id, challenger_id)

    second = await duel_service.accept_duel(
        session, chat_id, duel.id, opponent_id, "test_idempotent_accept_2"
    )

    assert second["status"] == first["status"] == "resolved"
    assert second["winner_id"] == first["winner_id"]
    assert second["loser_id"] == first["loser_id"]
    assert second["fee"] == first["fee"]
    assert second["pot"] == first["pot"]

    # деньги не двинулись повторно
    assert await _get_user_balance(session, chat_id, challenger_id) == balance_after_first


# --- duelbot (D-08: против банка) --------------------------------------------


@pytest.mark.asyncio
async def test_duelbot_vs_bank_challenger_wins(session, monkeypatch):
    chat_id = -100910011
    challenger_id = 910011
    await _ensure_user(session, challenger_id)
    challenger_before = await _fund(session, chat_id, challenger_id)
    await economy_service.credit_bank(
        session, chat_id, 100_000, kind="test_seed", ref_id="test_duelbot_win_seed_bank"
    )
    await session.commit()

    monkeypatch.setattr(duel_service, "_rng", _ForcedChoiceRng(True))

    stake = 100
    result = await duel_service.duelbot(session, chat_id, challenger_id, stake, "test_duelbot_win")

    expected_fee = max(1, math.ceil(2 * stake * settings.transfer_fee_pct))
    expected_pot = 2 * stake - expected_fee

    assert result["winner_id"] == challenger_id
    assert result["loser_id"] is None
    assert result["pot"] == expected_pot

    duel_row = await _get_duel(session, result["duel_id"])
    assert duel_row.opponent_id is None
    assert duel_row.status == "resolved"

    assert (
        await _get_user_balance(session, chat_id, challenger_id)
        == challenger_before - stake + expected_pot
    )


@pytest.mark.asyncio
async def test_duelbot_vs_bank_challenger_loses(session, monkeypatch):
    chat_id = -100910012
    challenger_id = 910012
    await _ensure_user(session, challenger_id)
    challenger_before = await _fund(session, chat_id, challenger_id)
    bank_before = await _get_bank_balance(session, chat_id)

    monkeypatch.setattr(duel_service, "_rng", _ForcedChoiceRng(False))

    stake = 100
    result = await duel_service.duelbot(session, chat_id, challenger_id, stake, "test_duelbot_loss")

    assert result["winner_id"] is None
    assert result["loser_id"] == challenger_id
    assert result["pot"] == 0

    duel_row = await _get_duel(session, result["duel_id"])
    assert duel_row.opponent_id is None
    assert duel_row.loser_id == challenger_id

    assert await _get_user_balance(session, chat_id, challenger_id) == challenger_before - stake
    assert await _get_bank_balance(session, chat_id) == bank_before + stake


# --- decline_duel / cancel_duel (полный рефанд) ------------------------------


@pytest.mark.asyncio
async def test_decline_refunds_challenger(session):
    chat_id = -100910013
    challenger_id, opponent_id = 910013, 910014
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    challenger_before = await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    stake = 100
    duel = await duel_service.create_duel(
        session, chat_id, challenger_id, opponent_id, stake, "test_decline_create"
    )

    result = await duel_service.decline_duel(session, chat_id, duel.id, opponent_id)

    assert result["status"] == "declined"
    duel_row = await _get_duel(session, duel.id)
    assert duel_row.status == "declined"
    assert await _get_user_balance(session, chat_id, challenger_id) == challenger_before


@pytest.mark.asyncio
async def test_cancel_refunds_challenger(session):
    chat_id = -100910015
    challenger_id, opponent_id = 910015, 910016
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    challenger_before = await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    stake = 100
    duel = await duel_service.create_duel(
        session, chat_id, challenger_id, opponent_id, stake, "test_cancel_create"
    )

    result = await duel_service.cancel_duel(session, chat_id, duel.id, challenger_id)

    assert result["status"] == "cancelled"
    duel_row = await _get_duel(session, duel.id)
    assert duel_row.status == "cancelled"
    assert await _get_user_balance(session, chat_id, challenger_id) == challenger_before


@pytest.mark.asyncio
async def test_decline_refund_full_even_when_shared_bank_is_empty(session):
    """WR-04 (04.1-REVIEW) regression: до фикса рефанд шёл через
    pay_from_bank, капнутый ОБЩИМ остатком chat_bank (D-06) — раз ставка
    challenger'а заходила в банк уже на create_duel, любая несвязанная
    выплата, просадившая банк между эскроу и отменой, оставляла игрока,
    который вообще не играл, без части его собственных денег. Теперь ставка
    challenger'а не заходит в chat_bank, пока дуэль pending (создаём дуэль и
    явно проверяем, что банк остаётся на нуле), поэтому рефанд гарантированно
    полный независимо от состояния банка."""
    chat_id = -100910023
    challenger_id, opponent_id = 910023, 910024
    await _ensure_user(session, challenger_id)
    await _ensure_user(session, opponent_id)
    challenger_before = await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    stake = 100
    duel = await duel_service.create_duel(
        session, chat_id, challenger_id, opponent_id, stake, "test_drain_create"
    )
    assert await _get_bank_balance(session, chat_id) == 0

    result = await duel_service.decline_duel(session, chat_id, duel.id, opponent_id)

    assert result["refunded"] == stake
    assert await _get_user_balance(session, chat_id, challenger_id) == challenger_before


# --- Хендлеры: мут проигравшего + /unmute (D-01/D-02/D-03) -------------------


@pytest.mark.asyncio
async def test_duel_accept_handler_applies_mute_and_sticker(session, bot, monkeypatch):
    chat_id = -100910017
    challenger_id, opponent_id = 910017, 910018
    await _ensure_user(session, challenger_id, "Челленджер")
    await _ensure_user(session, opponent_id, "Оппонент")
    await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    stake = 100
    duel = await duel_service.create_duel(
        session, chat_id, challenger_id, opponent_id, stake, "test_handler_accept_create"
    )

    # Челленджер выигрывает -> оппонент (принимающий) проигрывает и должен быть замучен.
    monkeypatch.setattr(duel_service, "_rng", _ForcedChoiceRng(challenger_id))

    message = _fake_message(
        chat_id, opponent_id, "Оппонент", f"/duel_accept {duel.id}", message_id=42
    )

    before = datetime.utcnow()
    await duel_handlers.duel_accept_command(message, session, bot)

    bot.restrict_chat_member.assert_awaited_once()
    call = bot.restrict_chat_member.await_args
    assert call.args[0] == chat_id
    assert call.args[1] == opponent_id
    permissions = call.kwargs["permissions"]
    assert permissions.can_send_messages is False
    until_date = call.kwargs["until_date"]
    assert (until_date - before).total_seconds() == pytest.approx(600, abs=5)

    bot.send_sticker.assert_any_await(chat_id, duel_handlers.MUTE_STICKER_ID)


@pytest.mark.asyncio
async def test_duel_accept_handler_survives_mute_failure(session, bot, monkeypatch):
    """WR-05 (04.1-REVIEW) regression: restrict_chat_member падает (например,
    проигравший — админ чата, Telegram отвергает попытку его замутить) —
    деньги уже двинулись (duel_service.accept_duel закоммитил ДО вызова
    _apply_mute), поэтому пользователь всё равно должен получить
    подтверждение результата дуэли, а не необработанное исключение."""
    chat_id = -100910029
    challenger_id, opponent_id = 910029, 910030
    await _ensure_user(session, challenger_id, "Челленджер")
    await _ensure_user(session, opponent_id, "Оппонент")
    await _fund(session, chat_id, challenger_id)
    await _fund(session, chat_id, opponent_id)

    stake = 100
    duel = await duel_service.create_duel(
        session, chat_id, challenger_id, opponent_id, stake, "test_mute_fail_create"
    )

    monkeypatch.setattr(duel_service, "_rng", _ForcedChoiceRng(challenger_id))
    bot.restrict_chat_member.side_effect = Exception("CHAT_ADMIN_REQUIRED")

    message = _fake_message(
        chat_id, opponent_id, "Оппонент", f"/duel_accept {duel.id}", message_id=42
    )

    await duel_handlers.duel_accept_command(message, session, bot)

    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_unmute_admin_lifts_mute_and_sticker(session, bot, monkeypatch):
    chat_id = -100910019
    admin_id, target_id = 910019, 910020
    await _ensure_user(session, target_id, "Проигравший")

    monkeypatch.setattr(
        duel_handlers.admin_service, "is_chat_admin", AsyncMock(return_value=True)
    )

    reply_to = SimpleNamespace(from_user=SimpleNamespace(id=target_id, first_name="Проигравший"))
    message = _fake_message(
        chat_id, admin_id, "Админ", "/unmute", message_id=1, reply_to_message=reply_to
    )

    await duel_handlers.unmute_command(message, session, bot)

    bot.restrict_chat_member.assert_awaited_once()
    call = bot.restrict_chat_member.await_args
    assert call.args[0] == chat_id
    assert call.args[1] == target_id
    permissions = call.kwargs["permissions"]
    assert permissions.can_send_messages is True

    bot.send_sticker.assert_any_await(chat_id, duel_handlers.UNMUTE_STICKER_ID)


@pytest.mark.asyncio
async def test_unmute_non_admin_refused_no_restrict(session, bot, monkeypatch):
    chat_id = -100910021
    non_admin_id, target_id = 910021, 910022
    await _ensure_user(session, target_id, "Проигравший")

    monkeypatch.setattr(
        duel_handlers.admin_service, "is_chat_admin", AsyncMock(return_value=False)
    )

    reply_to = SimpleNamespace(from_user=SimpleNamespace(id=target_id, first_name="Проигравший"))
    message = _fake_message(
        chat_id, non_admin_id, "НеАдмин", "/unmute", message_id=1, reply_to_message=reply_to
    )

    await duel_handlers.unmute_command(message, session, bot)

    bot.restrict_chat_member.assert_not_awaited()
    message.reply.assert_awaited_once()
