<script lang="ts">
	// Duel — challenge / accept / decline / cancel / duelbot (DUEL-01/02,
	// D-04). 04-UI-SPEC.md §Component Inventory "Duel challenge/accept
	// flow": two-state screen (outgoing challenge card -> accept/decline),
	// resolution reveal uses the same Hero-tier ("jackpot theater")
	// full-bleed treatment as every other new-game reveal in this phase
	// (04-UI-SPEC.md Typography: "duel win/loss reveal" is explicitly listed
	// alongside roulette/blackjack/dice/coinflip/gacha as a Hero (64px) tier
	// moment), with a muted destructive-tinted variant on loss + mute notice.
	//
	// Reachable from the "Дуэль" hub tile AND the `?startapp=<chat_id>_duel`
	// deep-link (+layout.svelte already resolves `parsed.route` to `/duel`).
	//
	// Scope note (Claude's Discretion, 04.2-06-PLAN.md): this plan's backend
	// surface is exactly 5 POST routes (create/accept/decline/cancel/
	// duelbot) — no GET /duel/{id} lookup route exists in this phase, so
	// there is no server-side way to list "my pending duels". The manage
	// tab still takes a duel_id the same way the bot commands already do
	// (`/duel_accept <id>` — the challenger announces this id to the
	// opponent when creating the duel).
	//
	// Opponent selection (feedback #8, resolved 2026-07-23): GET
	// /api/v1/members now exists, so the challenge form uses UserPicker
	// (search by @username/name) instead of a raw numeric-ID text field —
	// manual ID entry still works as a fallback inside that same component.
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic, user } from '$lib/tg';
	import UserPicker from '$lib/components/UserPicker.svelte';

	const BET_CHIPS = [10, 50, 100, 500, 1000];

	type Tab = 'challenge' | 'manage' | 'duelbot';

	type DuelCreateResult = {
		duel_id: number;
		status: string;
		challenger_id: number;
		opponent_id: number;
		stake: number;
	};

	type DuelResolution = {
		status: string;
		duel_id: number;
		winner_id: number | null;
		loser_id: number | null;
		fee: number;
		pot: number;
		mute_seconds: number;
	};

	type DuelActionResult = {
		status: string;
		duel_id: number;
		refunded?: number;
	};

	let tab = $state<Tab>('challenge');
	const myId = user?.id ?? null;

	function describeError(err: unknown): string {
		return err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
	}

	function isResolution(result: DuelResolution | DuelActionResult): result is DuelResolution {
		return result.status === 'resolved';
	}

	// --- Challenge (POST /duel) ------------------------------------------

	let opponentId = $state<number | null>(null);
	let challengeStake = $state(BET_CHIPS[0]);
	let challenging = $state(false);
	let challengeResult = $state<DuelCreateResult | null>(null);
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
			const res = await apiFetch<DuelCreateResult>('/api/v1/duel', {
				method: 'POST',
				body: JSON.stringify({
					opponent_id: opponentId,
					stake: challengeStake,
					ref_id: `duel:${crypto.randomUUID()}`
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

	// --- Manage (accept / decline / cancel an existing duel_id) ----------

	let manageDuelId = $state('');
	let managing = $state(false);
	let manageError = $state<string | null>(null);
	let manageResult = $state<DuelResolution | DuelActionResult | null>(null);

	function parsedDuelId(): number | null {
		const id = Number(manageDuelId);
		return manageDuelId.trim() && Number.isInteger(id) && id > 0 ? id : null;
	}

	async function manageAction(action: 'accept' | 'decline' | 'cancel') {
		const duelId = parsedDuelId();
		if (managing) return;
		if (duelId === null) {
			manageError = 'Введи корректный номер дуэли (число).';
			return;
		}
		managing = true;
		manageError = null;
		manageResult = null;
		try {
			const body = action === 'accept' ? { ref_id: `duel_accept:${crypto.randomUUID()}` } : undefined;
			const res = await apiFetch<DuelResolution | DuelActionResult>(
				`/api/v1/duel/${duelId}/${action}`,
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

	// --- Duelbot (POST /duelbot) ------------------------------------------

	let duelbotStake = $state(BET_CHIPS[0]);
	let duelbotting = $state(false);
	let duelbotResult = $state<DuelResolution | null>(null);
	let duelbotError = $state<string | null>(null);

	async function playDuelbot() {
		if (duelbotting) return;
		duelbotting = true;
		duelbotError = null;
		duelbotResult = null;
		try {
			const res = await apiFetch<DuelResolution>('/api/v1/duelbot', {
				method: 'POST',
				body: JSON.stringify({ stake: duelbotStake, ref_id: `duelbot:${crypto.randomUUID()}` })
			});
			duelbotResult = res;
			const won = myId != null && res.winner_id === myId;
			haptic(won ? 'big-win' : 'lose');
		} catch (err) {
			duelbotError = describeError(err);
			haptic('error');
		} finally {
			duelbotting = false;
		}
	}
</script>

{#snippet resolutionCard(res: DuelResolution)}
	{@const won = myId != null && res.winner_id === myId}
	<div class={`duel-reveal ${won ? 'duel-win' : 'duel-lose'}`}>
		<div class="duel-reveal-flash">{won ? 'ПОБЕДА' : 'ПРОИГРЫШ'}</div>
		<div class="duel-reveal-amount">
			{won ? `+${res.pot}¥` : 'ставка потеряна'}
		</div>
		<div class="duel-reveal-sub">
			комиссия в банк чата: {res.fee}¥ · банк {res.pot}¥ достался {won ? 'тебе' : 'победителю'}
		</div>
		{#if !won}
			<div class="duel-mute-note">
				Мут на {Math.round(res.mute_seconds / 60)} мин — не сможешь писать в чат.
			</div>
		{/if}
	</div>
{/snippet}

<div class="duel-screen">
	<div class="menu-head">
		<h1 class="menu-title">Дуэль</h1>
		<div class="menu-sub">вызови кого-нибудь на бабки</div>
	</div>

	<div class="duel-tabs">
		<button
			type="button"
			class={`chip duel-tab ${tab === 'challenge' ? 'chip-on' : ''}`}
			onclick={() => (tab = 'challenge')}
		>
			вызвать
		</button>
		<button
			type="button"
			class={`chip duel-tab ${tab === 'manage' ? 'chip-on' : ''}`}
			onclick={() => (tab = 'manage')}
		>
			мои дуэли
		</button>
		<button
			type="button"
			class={`chip duel-tab ${tab === 'duelbot' ? 'chip-on' : ''}`}
			onclick={() => (tab = 'duelbot')}
		>
			дуэльбот
		</button>
	</div>

	{#if tab === 'challenge'}
		<div class="duel-panel">
			<div class="duel-hint">
				Ставка списывается сразу. При принятии — коинфлип, победитель забирает банк минус 5%
				комиссии, проигравший получает мут.
			</div>

			<UserPicker bind:value={opponentId} label="Соперник" placeholder="@ник, имя или ID" />

			<div class="bet-row">
				<div class="bet-display">
					<span class="bet-label">ставка</span>
					<div class="bet-amount">{challengeStake}<small>¥</small></div>
				</div>
				<div class="bet-chips">
					{#each BET_CHIPS as v (v)}
						<button
							type="button"
							class={`chip ${challengeStake === v ? 'chip-on' : ''}`}
							disabled={challenging}
							onclick={() => (challengeStake = v)}
						>
							{v}
						</button>
					{/each}
				</div>
			</div>

			{#if challengeError}
				<div class="duel-error">{challengeError}</div>
			{/if}

			{#if challengeResult}
				<div class="duel-created">
					<div class="duel-created-title">Дуэль #{challengeResult.duel_id} создана</div>
					<div class="duel-created-sub">
						Скажи сопернику этот номер — пусть введёт его во вкладке «Мои дуэли» и нажмёт «Принять».
					</div>
				</div>
			{/if}

			<button type="button" class="duel-cta" disabled={challenging} onclick={challenge}>
				<span class="duel-cta-label">{challenging ? 'вызываем…' : 'ВЫЗВАТЬ НА ДУЭЛЬ'}</span>
				<span class="duel-cta-sub">{challenging ? '' : `ставка ${challengeStake}¥`}</span>
			</button>
		</div>
	{:else if tab === 'manage'}
		<div class="duel-panel">
			<div class="duel-hint">
				Введи номер дуэли (его называет вызывающий) и выбери действие.
			</div>

			<label class="duel-field">
				<span class="duel-field-label">Номер дуэли</span>
				<input
					class="duel-input"
					type="text"
					inputmode="numeric"
					placeholder="например 42"
					bind:value={manageDuelId}
					disabled={managing}
				/>
			</label>

			{#if manageError}
				<div class="duel-error">{manageError}</div>
			{/if}

			{#if manageResult}
				{#if isResolution(manageResult)}
					{@render resolutionCard(manageResult)}
				{:else}
					<div class="duel-created">
						<div class="duel-created-title">Дуэль #{manageResult.duel_id}: {manageResult.status}</div>
						{#if manageResult.refunded}
							<div class="duel-created-sub">Ставка возвращена: {manageResult.refunded}¥</div>
						{/if}
					</div>
				{/if}
			{/if}

			<div class="duel-actions">
				<button
					type="button"
					class="chip chip-all duel-action"
					disabled={managing}
					onclick={() => manageAction('accept')}
				>
					принять
				</button>
				<button
					type="button"
					class="chip duel-action"
					disabled={managing}
					onclick={() => manageAction('decline')}
				>
					отклонить
				</button>
				<button
					type="button"
					class="chip duel-action"
					disabled={managing}
					onclick={() => manageAction('cancel')}
				>
					отменить
				</button>
			</div>
		</div>
	{:else}
		<div class="duel-panel">
			<div class="duel-hint">
				Дуэль против банка чата — тот же коинфлип, но соперник не нужен. Автопринятие.
			</div>

			<div class="bet-row">
				<div class="bet-display">
					<span class="bet-label">ставка</span>
					<div class="bet-amount">{duelbotStake}<small>¥</small></div>
				</div>
				<div class="bet-chips">
					{#each BET_CHIPS as v (v)}
						<button
							type="button"
							class={`chip ${duelbotStake === v ? 'chip-on' : ''}`}
							disabled={duelbotting}
							onclick={() => (duelbotStake = v)}
						>
							{v}
						</button>
					{/each}
				</div>
			</div>

			{#if duelbotError}
				<div class="duel-error">{duelbotError}</div>
			{/if}

			{#if duelbotResult}
				{@render resolutionCard(duelbotResult)}
			{/if}

			<button type="button" class="duel-cta" disabled={duelbotting} onclick={playDuelbot}>
				<span class="duel-cta-label">{duelbotting ? 'крутим…' : 'ВЫЗВАТЬ БАНК'}</span>
				<span class="duel-cta-sub">{duelbotting ? '' : `ставка ${duelbotStake}¥`}</span>
			</button>
		</div>
	{/if}
</div>

<style>
	.duel-screen {
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

	.duel-tabs {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: var(--space-xs);
	}
	.duel-tab {
		text-align: center;
	}

	.duel-panel {
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
	}

	.duel-hint {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		line-height: 1.5;
		font-family: var(--font-body);
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 10px;
		padding: var(--space-sm) var(--space-md);
	}

	.duel-field {
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.duel-field-label {
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
	}
	.duel-input {
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 10px;
		padding: var(--space-sm) var(--space-md);
		font-family: var(--font-numeric);
		font-size: var(--font-heading-size);
		color: var(--text-primary);
	}
	.duel-input:focus {
		outline: none;
		border-color: var(--accent-pink);
	}

	.duel-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.duel-created {
		background: var(--bg-secondary-2);
		border: 2px solid var(--accent-cyan);
		border-radius: 14px;
		padding: var(--space-md);
		text-align: center;
	}
	.duel-created-title {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		color: var(--accent-cyan);
	}
	.duel-created-sub {
		font-size: 12px;
		color: var(--text-muted);
		margin-top: var(--space-xs);
		font-family: var(--font-body);
		line-height: 1.4;
	}

	.duel-actions {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: var(--space-xs);
	}
	.duel-action {
		text-align: center;
	}

	.bet-row {
		display: flex;
		align-items: center;
		gap: var(--space-md);
	}
	.bet-display {
		display: flex;
		flex-direction: column;
	}
	.bet-label {
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
	}
	.bet-amount {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		color: var(--text-primary);
	}
	.bet-amount small {
		font-size: 12px;
		color: var(--accent-pink);
		margin-left: 1px;
	}
	.bet-chips {
		display: grid;
		grid-template-columns: repeat(5, 1fr);
		gap: var(--space-xs);
		flex: 1;
	}

	.duel-cta {
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
	.duel-cta:active:not(:disabled) {
		transform: translate(2px, 2px);
		box-shadow: 2px 2px 0 #111;
	}
	.duel-cta:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.duel-cta-label {
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		color: #1a0f12;
		letter-spacing: 0.04em;
	}
	.duel-cta-sub {
		font-size: 12px;
		color: #3a1420;
		font-family: var(--font-body);
	}

	/* Resolution reveal — Hero-tier "jackpot theater" moment (04-UI-SPEC.md
	   Typography: duel win/loss reveal is explicitly listed at the 64px Hero
	   tier alongside coinflip/dice/roulette/blackjack/gacha reveals), with a
	   muted destructive-tinted variant on loss + mute notice. */
	.duel-reveal {
		border-radius: 14px;
		padding: var(--space-lg);
		text-align: center;
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
	}
	.duel-reveal-flash {
		font-family: var(--font-numeric);
		font-size: var(--font-hero-size);
		font-weight: 900;
		text-transform: uppercase;
		line-height: 1;
		color: var(--text-primary);
	}
	.duel-reveal-amount {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		margin-top: var(--space-sm);
	}
	.duel-reveal-sub {
		font-size: 12px;
		color: var(--text-muted);
		margin-top: var(--space-xs);
		font-family: var(--font-body);
	}
	.duel-win {
		border-color: var(--accent-pink);
	}
	.duel-win .duel-reveal-flash,
	.duel-win .duel-reveal-amount {
		color: var(--accent-pink);
	}
	.duel-lose {
		border-color: var(--destructive-text);
		background: var(--destructive-bg);
	}
	.duel-lose .duel-reveal-flash,
	.duel-lose .duel-reveal-amount {
		color: var(--destructive-text);
	}
	.duel-mute-note {
		margin-top: var(--space-sm);
		font-size: 12px;
		line-height: 1.4;
		color: var(--accent-yellow);
		font-family: var(--font-body);
	}
</style>
