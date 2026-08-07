<script lang="ts">
	// Автобитва (GACHA-06) — PvP на ставку, взвешенное силой гача-коллекции,
	// не честный коинфлип. Форма miniapp/src/routes/duel/+page.svelte (читай
	// её комментарий за разбором конвенций UserPicker/BetControl/resolution
	// reveal) — здесь два реальных отличия:
	//
	// 1. Нет вкладки "дуэльбот" (вс банк): win_probability бессмысленна против
	//    оппонента без коллекции с бесконечной "силой" — autobattle_service
	//    вообще не резолвит бой без реального opponent_id.
	// 2. Превью своей силы (GET /api/v1/autobattle/power) ДО вызова — здесь
	//    сила реально влияет на исход (в отличие от честного 50/50 Duel),
	//    игрок должен видеть её перед тем, как ставить.
	// 3. Нет мут-эффекта проигравшего — resolution reveal не показывает
	//    duel-мут-нотис.
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic, user } from '$lib/tg';
	import UserPicker from '$lib/components/UserPicker.svelte';
	import BetControl from '$lib/components/BetControl.svelte';

	type Tab = 'challenge' | 'manage';

	type AutoBattleCreateResult = {
		autobattle_id: number;
		status: string;
		challenger_id: number;
		opponent_id: number;
		stake: number;
	};

	type AutoBattleResolution = {
		status: string;
		autobattle_id: number;
		winner_id: number | null;
		loser_id: number | null;
		challenger_power: number;
		opponent_power: number;
		fee: number;
		pot: number;
	};

	type AutoBattleActionResult = {
		status: string;
		autobattle_id: number;
		refunded?: number;
	};

	let tab = $state<Tab>('challenge');
	const myId = user?.id ?? null;

	function describeError(err: unknown): string {
		return err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
	}

	function isResolution(
		result: AutoBattleResolution | AutoBattleActionResult
	): result is AutoBattleResolution {
		return result.status === 'resolved';
	}

	// --- Своя сила (GET /autobattle/power) ---------------------------------

	let myPower = $state<number | null>(null);

	onMount(async () => {
		try {
			const res = await apiFetch<{ power: number }>('/api/v1/autobattle/power');
			myPower = res.power;
		} catch {
			// Некритичное превью — на любую ошибку просто не показываем цифру,
			// сам экран не должен падать из-за неё.
			myPower = null;
		}
	});

	// --- Challenge (POST /autobattle) ---------------------------------------

	let opponentId = $state<number | null>(null);
	let challengeStake = $state(10);
	let challenging = $state(false);
	let challengeResult = $state<AutoBattleCreateResult | null>(null);
	let challengeError = $state<string | null>(null);

	async function challenge() {
		if (challenging || opponentId === null) {
			challengeError = 'Выбери соперника (по @нику или ID).';
			return;
		}
		challenging = true;
		challengeError = null;
		challengeResult = null;
		try {
			const res = await apiFetch<AutoBattleCreateResult>('/api/v1/autobattle', {
				method: 'POST',
				body: JSON.stringify({
					opponent_id: opponentId,
					stake: challengeStake,
					ref_id: `autobattle:${crypto.randomUUID()}`
				})
			});
			challengeResult = res;
			haptic('tap');
		} catch (err) {
			challengeError = describeError(err);
			haptic('error');
		} finally {
			challenging = false;
		}
	}

	// --- Manage (accept / decline / cancel an existing autobattle_id) -------

	let manageId = $state('');
	let managing = $state(false);
	let manageError = $state<string | null>(null);
	let manageResult = $state<AutoBattleResolution | AutoBattleActionResult | null>(null);

	function parsedId(): number | null {
		const id = Number(manageId);
		return manageId.trim() && Number.isInteger(id) && id > 0 ? id : null;
	}

	async function manageAction(action: 'accept' | 'decline' | 'cancel') {
		const autobattleId = parsedId();
		if (managing) return;
		if (autobattleId === null) {
			manageError = 'Введи корректный номер автобитвы (число).';
			return;
		}
		managing = true;
		manageError = null;
		manageResult = null;
		try {
			const body =
				action === 'accept' ? { ref_id: `autobattle_accept:${crypto.randomUUID()}` } : undefined;
			const res = await apiFetch<AutoBattleResolution | AutoBattleActionResult>(
				`/api/v1/autobattle/${autobattleId}/${action}`,
				{
					method: 'POST',
					...(body ? { body: JSON.stringify(body) } : {})
				}
			);
			manageResult = res;
			if (isResolution(res)) {
				const won = myId != null && res.winner_id === myId;
				haptic(won ? 'big-win' : 'lose');
			} else {
				haptic('tap');
			}
		} catch (err) {
			manageError = describeError(err);
			haptic('error');
		} finally {
			managing = false;
		}
	}
</script>

{#snippet resolutionCard(res: AutoBattleResolution)}
	{@const won = myId != null && res.winner_id === myId}
	<div class={`autobattle-reveal ${won ? 'autobattle-win' : 'autobattle-lose'}`}>
		<div class="autobattle-reveal-flash">{won ? 'ПОБЕДА' : 'ПОРАЖЕНИЕ'}</div>
		<div class="autobattle-reveal-amount">
			{won ? `+${res.pot}¥` : 'ставка потеряна'}
		</div>
		<div class="autobattle-reveal-power">
			сила {res.challenger_power} vs {res.opponent_power}
		</div>
		<div class="autobattle-reveal-sub">
			комиссия в банк чата: {res.fee}¥ · банк {res.pot}¥ достался {won ? 'тебе' : 'победителю'}
		</div>
	</div>
{/snippet}

<div class="autobattle-screen">
	<div class="menu-head">
		<h1 class="menu-title">Автобитва</h1>
		<div class="menu-sub">коллекция решает, но не гарантирует</div>
	</div>

	{#if myPower !== null}
		<div class="autobattle-power-badge">
			твоя сила коллекции: <span class="autobattle-power-value">{myPower}</span>
		</div>
	{/if}

	<div class="autobattle-tabs">
		<button
			type="button"
			class={`chip autobattle-tab ${tab === 'challenge' ? 'chip-on' : ''}`}
			onclick={() => (tab = 'challenge')}
		>
			вызвать
		</button>
		<button
			type="button"
			class={`chip autobattle-tab ${tab === 'manage' ? 'chip-on' : ''}`}
			onclick={() => (tab = 'manage')}
		>
			мои автобитвы
		</button>
	</div>

	{#if tab === 'challenge'}
		<div class="autobattle-panel">
			<div class="autobattle-hint">
				Ставка списывается сразу. При принятии считается сила гача-коллекции обеих сторон —
				сильнее коллекция даёт реальный перевес (от 5% до 95% шанса), но не гарантирует победу.
				Победитель забирает банк минус 5% комиссии.
			</div>

			<div class="autobattle-vs">
				<div class="autobattle-vs-side">
					<div
						class="autobattle-vs-avatar"
						style="background: linear-gradient(135deg, #2a2438, var(--accent-cyan))"
					>
						Я
					</div>
					<span class="autobattle-vs-label">ты</span>
				</div>
				<div class="autobattle-vs-mid">VS</div>
				<div class="autobattle-vs-side">
					<div
						class="autobattle-vs-avatar"
						style="background: linear-gradient(135deg, #2a2438, var(--accent-pink))"
					>
						?
					</div>
					<span class="autobattle-vs-label">{opponentId ? `id${opponentId}` : 'соперник'}</span>
				</div>
			</div>

			<UserPicker bind:value={opponentId} label="Соперник" placeholder="@ник, имя или ID" />

			<BetControl bind:bet={challengeStake} disabled={challenging} />

			{#if challengeError}
				<div class="autobattle-error">{challengeError}</div>
			{/if}

			{#if challengeResult}
				<div class="autobattle-created">
					<div class="autobattle-created-title">Автобитва #{challengeResult.autobattle_id} создана</div>
					<div class="autobattle-created-sub">
						Скажи сопернику этот номер — пусть введёт его во вкладке «Мои автобитвы» и нажмёт
						«Принять».
					</div>
				</div>
			{/if}

			<button type="button" class="autobattle-cta" disabled={challenging} onclick={challenge}>
				<span class="autobattle-cta-label">{challenging ? 'вызываем…' : 'ВЫЗВАТЬ НА АВТОБИТВУ'}</span>
				<span class="autobattle-cta-sub">{challenging ? '' : `ставка ${challengeStake}¥`}</span>
			</button>
		</div>
	{:else}
		<div class="autobattle-panel">
			<div class="autobattle-hint">Введи номер автобитвы (его называет вызывающий) и выбери действие.</div>

			<label class="autobattle-field">
				<span class="autobattle-field-label">Номер автобитвы</span>
				<input
					class="autobattle-input"
					type="text"
					inputmode="numeric"
					placeholder="например 42"
					bind:value={manageId}
					disabled={managing}
				/>
			</label>

			{#if manageError}
				<div class="autobattle-error">{manageError}</div>
			{/if}

			{#if manageResult}
				{#if isResolution(manageResult)}
					{@render resolutionCard(manageResult)}
				{:else}
					<div class="autobattle-created">
						<div class="autobattle-created-title">
							Автобитва #{manageResult.autobattle_id}: {manageResult.status}
						</div>
						{#if manageResult.refunded}
							<div class="autobattle-created-sub">Ставка возвращена: {manageResult.refunded}¥</div>
						{/if}
					</div>
				{/if}
			{/if}

			<div class="autobattle-actions">
				<button
					type="button"
					class="chip chip-all autobattle-action"
					disabled={managing}
					onclick={() => manageAction('accept')}
				>
					принять
				</button>
				<button
					type="button"
					class="chip autobattle-action"
					disabled={managing}
					onclick={() => manageAction('decline')}
				>
					отклонить
				</button>
				<button
					type="button"
					class="chip autobattle-action"
					disabled={managing}
					onclick={() => manageAction('cancel')}
				>
					отменить
				</button>
			</div>
		</div>
	{/if}
</div>

<style>
	.autobattle-screen {
		padding: 24px 18px 32px;
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
	}
	.menu-head {
		margin-bottom: var(--space-xs);
	}
	.menu-title {
		font-family: var(--font-chrome);
		font-size: var(--font-display-size);
		font-weight: 700;
		margin: 0;
		color: var(--text-primary);
	}
	.menu-sub {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		margin-top: var(--space-xs);
		letter-spacing: 0.04em;
		font-family: var(--font-body);
	}

	.autobattle-power-badge {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		font-family: var(--font-body);
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 10px;
		padding: var(--space-sm) var(--space-md);
	}
	.autobattle-power-value {
		font-family: var(--font-numeric);
		font-weight: 900;
		color: var(--accent-yellow);
	}

	.autobattle-tabs {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: var(--space-xs);
	}
	.autobattle-tab {
		text-align: center;
	}

	.autobattle-panel {
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
	}

	.autobattle-vs {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: var(--space-md);
	}
	.autobattle-vs-side {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 4px;
	}
	.autobattle-vs-avatar {
		width: 56px;
		height: 56px;
		border-radius: 12px;
		display: flex;
		align-items: center;
		justify-content: center;
		font-family: var(--font-chrome);
		font-size: 18px;
		font-weight: 700;
		color: var(--text-primary);
	}
	.autobattle-vs-label {
		font-size: 11px;
		color: var(--text-muted);
		font-family: var(--font-body);
		max-width: 90px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.autobattle-vs-mid {
		font-family: var(--font-numeric);
		font-size: 20px;
		font-weight: 900;
		color: var(--accent-yellow);
	}

	.autobattle-hint {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		line-height: 1.5;
		font-family: var(--font-body);
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 10px;
		padding: var(--space-sm) var(--space-md);
	}

	.autobattle-field {
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.autobattle-field-label {
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
	}
	.autobattle-input {
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 10px;
		padding: var(--space-sm) var(--space-md);
		font-family: var(--font-numeric);
		font-size: var(--font-heading-size);
		color: var(--text-primary);
	}
	.autobattle-input:focus {
		outline: none;
		border-color: var(--accent-pink);
	}

	.autobattle-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.autobattle-created {
		background: var(--bg-secondary-2);
		border: 2px solid var(--accent-cyan);
		border-radius: 14px;
		padding: var(--space-md);
		text-align: center;
	}
	.autobattle-created-title {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		color: var(--accent-cyan);
	}
	.autobattle-created-sub {
		font-size: 12px;
		color: var(--text-muted);
		margin-top: var(--space-xs);
		font-family: var(--font-body);
		line-height: 1.4;
	}

	.autobattle-actions {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: var(--space-xs);
	}
	.autobattle-action {
		text-align: center;
	}

	.autobattle-cta {
		background: var(--accent-pink);
		border: none;
		border-radius: 14px;
		padding: var(--space-md);
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--space-xs);
		cursor: pointer;
		box-shadow: 4px 4px 0 #111;
		transition: transform 0.08s;
	}
	.autobattle-cta:active:not(:disabled) {
		transform: translate(2px, 2px);
		box-shadow: 2px 2px 0 #111;
	}
	.autobattle-cta:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.autobattle-cta-label {
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		color: #1a0f12;
		letter-spacing: 0.04em;
	}
	.autobattle-cta-sub {
		font-size: 12px;
		color: #3a1420;
		font-family: var(--font-body);
	}

	/* Resolution reveal — та же Hero-tier ("jackpot theater") трактовка, что
	   у Duel/казино-игр (04-UI-SPEC.md Typography). */
	.autobattle-reveal {
		border-radius: 14px;
		padding: var(--space-lg);
		text-align: center;
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
	}
	.autobattle-reveal-flash {
		font-family: var(--font-numeric);
		font-size: var(--font-hero-size);
		font-weight: 900;
		text-transform: uppercase;
		line-height: 1;
		color: var(--text-primary);
	}
	.autobattle-reveal-amount {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		margin-top: var(--space-sm);
	}
	.autobattle-reveal-power {
		font-size: 13px;
		color: var(--text-muted);
		margin-top: var(--space-xs);
		font-family: var(--font-numeric);
		font-weight: 700;
	}
	.autobattle-reveal-sub {
		font-size: 12px;
		color: var(--text-muted);
		margin-top: var(--space-xs);
		font-family: var(--font-body);
	}
	.autobattle-win {
		border-color: var(--accent-pink);
	}
	.autobattle-win .autobattle-reveal-flash,
	.autobattle-win .autobattle-reveal-amount {
		color: var(--accent-pink);
	}
	.autobattle-lose {
		border-color: var(--destructive-text);
		background: var(--destructive-bg);
	}
	.autobattle-lose .autobattle-reveal-flash,
	.autobattle-lose .autobattle-reveal-amount {
		color: var(--destructive-text);
	}
</style>
