<script lang="ts">
	// Dice — over/under with a chosen target (CASINO-01, 04.2-03). Same
	// structural pattern as games/coinflip/+page.svelte: BET_CHIPS amount
	// picker, apiFetch POST, server-authoritative result render, haptic.
	//
	// Server is the sole source of truth for roll/win (D-03/T-04.1-01) — the
	// client-side multiplier readout below is purely informational (mirrors
	// bot/services/casino_service.py's public D-03 formula, does not affect
	// the actual settle) so the player understands what moving the target
	// slider does before committing to a bet.
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	const BET_CHIPS = [10, 50, 100, 500, 1000];
	const DICE_HOUSE_EDGE = 0.02; // casino_service.DICE_HOUSE_EDGE, informational mirror only

	type DiceResult = {
		game: string;
		bet: number;
		payout: number;
		outcome: { roll: number; target: number; direction: 'over' | 'under'; won: boolean };
		bank_capped?: boolean;
	};

	let bet = $state(BET_CHIPS[0]);
	let target = $state(50);
	let direction = $state<'over' | 'under'>('under');
	let rolling = $state(false);
	let result = $state<DiceResult | null>(null);
	let error = $state<string | null>(null);

	const winProb = $derived(
		direction === 'under' ? (target - 1) / 100 : (100 - target) / 100
	);
	const mult = $derived(winProb > 0 ? (1 - DICE_HOUSE_EDGE) / winProb : 0);

	async function roll() {
		if (rolling) return;
		rolling = true;
		error = null;
		result = null;
		haptic('spin');
		try {
			const res = await apiFetch<DiceResult>('/api/v1/games/dice', {
				method: 'POST',
				body: JSON.stringify({
					bet,
					target,
					direction,
					idem_key: `dice:${crypto.randomUUID()}`
				})
			});
			result = res;
			haptic(res.outcome.won ? 'win' : 'lose');
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
			haptic('error');
		} finally {
			rolling = false;
		}
	}
</script>

<div class="dice-screen">
	<div class="menu-head">
		<h1 class="menu-title">Кости</h1>
		<div class="menu-sub">больше/меньше · выбери множитель</div>
	</div>

	<div class="dice-picker">
		<button
			type="button"
			class={`chip dice-dir ${direction === 'under' ? 'chip-on' : ''}`}
			disabled={rolling}
			onclick={() => (direction = 'under')}
		>
			МЕНЬШЕ
		</button>
		<button
			type="button"
			class={`chip dice-dir ${direction === 'over' ? 'chip-on' : ''}`}
			disabled={rolling}
			onclick={() => (direction = 'over')}
		>
			БОЛЬШЕ
		</button>
	</div>

	<div class="dice-target">
		<div class="dice-target-row">
			<span class="dice-target-label">
				{direction === 'under' ? 'бросок <' : 'бросок >'}
			</span>
			<span class="dice-target-val">{target}</span>
			<span class="dice-target-mult">×{mult.toFixed(2)}</span>
		</div>
		<input
			type="range"
			class="dice-slider"
			min="2"
			max="99"
			bind:value={target}
			disabled={rolling}
		/>
	</div>

	{#if result}
		<div class={`dice-result ${result.outcome.won ? 'dice-win' : 'dice-lose'}`}>
			<div class="dice-result-flash">{result.outcome.roll}</div>
			<div class="dice-result-text">
				{result.outcome.won ? `+${result.payout}¥` : `−${result.bet}¥`}
			</div>
			{#if result.bank_capped}
				<div class="dice-capped-note">
					банк чата почти пуст — выплата урезана до {result.payout}¥ (не полный множитель).
					Баланс наверху мог не измениться, если урезанная выплата = твоей ставке.
				</div>
			{/if}
		</div>
	{/if}

	{#if error}
		<div class="dice-error">{error}</div>
	{/if}

	<div class="bet-row">
		<div class="bet-display">
			<span class="bet-label">ставка</span>
			<div class="bet-amount">{bet}<small>¥</small></div>
		</div>
		<div class="bet-chips">
			{#each BET_CHIPS as v (v)}
				<button
					type="button"
					class={`chip ${bet === v ? 'chip-on' : ''}`}
					disabled={rolling}
					onclick={() => (bet = v)}
				>
					{v}
				</button>
			{/each}
		</div>
	</div>

	<button type="button" class="dice-cta" disabled={rolling} onclick={roll}>
		<span class="dice-cta-label">{rolling ? 'бросаем…' : 'БРОСИТЬ КОСТИ'}</span>
		<span class="dice-cta-sub">{rolling ? '' : `ставка ${bet}¥`}</span>
	</button>
</div>

<style>
	.dice-screen {
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

	.dice-picker {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--space-sm);
	}
	.dice-dir {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		text-transform: uppercase;
		padding: var(--space-md);
	}

	.dice-target {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-md);
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.dice-target-row {
		display: flex;
		align-items: baseline;
		gap: var(--space-sm);
	}
	.dice-target-label {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		font-family: var(--font-body);
		text-transform: lowercase;
	}
	.dice-target-val {
		font-family: var(--font-numeric);
		font-size: var(--font-heading-size);
		font-weight: 900;
		color: var(--text-primary);
		margin-left: auto;
	}
	.dice-target-mult {
		font-family: var(--font-numeric);
		font-size: var(--font-body-size);
		color: var(--accent-pink);
	}
	.dice-slider {
		-webkit-appearance: none;
		appearance: none;
		width: 100%;
		height: 6px;
		border-radius: 3px;
		background: var(--border-secondary);
		accent-color: var(--accent-pink);
	}
	.dice-slider::-webkit-slider-thumb {
		-webkit-appearance: none;
		appearance: none;
		width: 20px;
		height: 20px;
		border-radius: 50%;
		background: var(--accent-pink);
		border: 2px solid #111;
		cursor: pointer;
	}
	.dice-slider::-moz-range-thumb {
		width: 20px;
		height: 20px;
		border-radius: 50%;
		background: var(--accent-pink);
		border: 2px solid #111;
		cursor: pointer;
	}
	.dice-slider:disabled {
		opacity: 0.5;
	}

	.dice-result {
		border-radius: 14px;
		padding: var(--space-lg);
		text-align: center;
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
	}
	.dice-result-flash {
		font-family: var(--font-numeric);
		font-size: var(--font-hero-size);
		font-weight: 900;
		line-height: 1;
		color: var(--text-primary);
	}
	.dice-result-text {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		margin-top: var(--space-sm);
	}
	.dice-win {
		border-color: var(--accent-pink);
	}
	.dice-win .dice-result-text {
		color: var(--accent-pink);
	}
	.dice-lose .dice-result-text {
		color: var(--destructive-text);
	}
	.dice-capped-note {
		margin-top: var(--space-sm);
		font-size: 12px;
		line-height: 1.4;
		color: var(--accent-yellow);
		font-family: var(--font-body);
	}

	.dice-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
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

	.dice-cta {
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
	.dice-cta:active:not(:disabled) {
		transform: translate(2px, 2px);
		box-shadow: 2px 2px 0 #111;
	}
	.dice-cta:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.dice-cta-label {
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		color: #1a0f12;
		letter-spacing: 0.04em;
	}
	.dice-cta-sub {
		font-size: 12px;
		color: #3a1420;
		font-family: var(--font-body);
	}
</style>
