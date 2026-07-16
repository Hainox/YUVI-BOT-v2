<script lang="ts">
	// Roulette — European wheel (0-36), straight number / color / parity bets
	// (CASINO-01, 04.2-03 — scoped per must_haves, casino_service also
	// supports half/dozen bet types not exposed in this screen). Same
	// structural pattern as games/coinflip and games/dice: BET_CHIPS amount
	// picker, apiFetch POST, server-authoritative result render, haptic.
	//
	// Server is the sole source of truth for the spin (D-03/T-04.1-01) — the
	// red-number set below is purely a client-side display mirror of
	// bot/services/casino_service.py's public D-03 color table, used only to
	// paint the result badge, never to compute an outcome.
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	const BET_CHIPS = [10, 50, 100, 500, 1000];
	const RED_NUMBERS = new Set([
		1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36
	]);

	type BetType = 'number' | 'color' | 'parity';
	type RouletteResult = {
		game: string;
		bet: number;
		payout: number;
		outcome: { spin: number; bet_type: BetType; bet_value: number | string; won: boolean };
		bank_capped?: boolean;
	};

	function spinColor(spin: number): 'red' | 'black' | 'green' {
		if (spin === 0) return 'green';
		return RED_NUMBERS.has(spin) ? 'red' : 'black';
	}

	let bet = $state(BET_CHIPS[0]);
	let betType = $state<BetType>('color');
	let numberValue = $state(7);
	let colorValue = $state<'red' | 'black'>('red');
	let parityValue = $state<'even' | 'odd'>('even');
	let spinning = $state(false);
	let result = $state<RouletteResult | null>(null);
	let error = $state<string | null>(null);

	const betValue = $derived(
		betType === 'number' ? numberValue : betType === 'color' ? colorValue : parityValue
	);

	async function spin() {
		if (spinning) return;
		spinning = true;
		error = null;
		result = null;
		haptic('spin');
		try {
			const res = await apiFetch<RouletteResult>('/api/v1/games/roulette', {
				method: 'POST',
				body: JSON.stringify({
					bet,
					bet_type: betType,
					bet_value: betValue,
					idem_key: `roulette:${crypto.randomUUID()}`
				})
			});
			result = res;
			haptic(res.outcome.won ? 'win' : 'lose');
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
			haptic('error');
		} finally {
			spinning = false;
		}
	}
</script>

<div class="rl-screen">
	<div class="menu-head">
		<h1 class="menu-title">Рулетка</h1>
		<div class="menu-sub">европейское колесо · 0–36</div>
	</div>

	<div class="rl-type-picker">
		<button
			type="button"
			class={`chip rl-type ${betType === 'number' ? 'chip-on' : ''}`}
			disabled={spinning}
			onclick={() => (betType = 'number')}
		>
			число
		</button>
		<button
			type="button"
			class={`chip rl-type ${betType === 'color' ? 'chip-on' : ''}`}
			disabled={spinning}
			onclick={() => (betType = 'color')}
		>
			цвет
		</button>
		<button
			type="button"
			class={`chip rl-type ${betType === 'parity' ? 'chip-on' : ''}`}
			disabled={spinning}
			onclick={() => (betType = 'parity')}
		>
			чёт/нечет
		</button>
	</div>

	{#if betType === 'number'}
		<div class="rl-value-row">
			<span class="rl-value-label">номер</span>
			<input
				type="number"
				class="rl-number-input"
				min="0"
				max="36"
				bind:value={numberValue}
				disabled={spinning}
			/>
		</div>
	{:else if betType === 'color'}
		<div class="rl-value-row rl-color-row">
			<button
				type="button"
				class={`chip rl-color rl-color-red ${colorValue === 'red' ? 'chip-on' : ''}`}
				disabled={spinning}
				onclick={() => (colorValue = 'red')}
			>
				красное
			</button>
			<button
				type="button"
				class={`chip rl-color rl-color-black ${colorValue === 'black' ? 'chip-on' : ''}`}
				disabled={spinning}
				onclick={() => (colorValue = 'black')}
			>
				чёрное
			</button>
		</div>
	{:else}
		<div class="rl-value-row rl-color-row">
			<button
				type="button"
				class={`chip ${parityValue === 'even' ? 'chip-on' : ''}`}
				disabled={spinning}
				onclick={() => (parityValue = 'even')}
			>
				чётное
			</button>
			<button
				type="button"
				class={`chip ${parityValue === 'odd' ? 'chip-on' : ''}`}
				disabled={spinning}
				onclick={() => (parityValue = 'odd')}
			>
				нечётное
			</button>
		</div>
	{/if}

	{#if result}
		<div class={`rl-result ${result.outcome.won ? 'rl-win' : 'rl-lose'}`}>
			<div class={`rl-result-flash rl-spin-${spinColor(result.outcome.spin)}`}>
				{result.outcome.spin}
			</div>
			<div class="rl-result-text">
				{result.outcome.won ? `+${result.payout}¥` : `−${result.bet}¥`}
			</div>
			{#if result.bank_capped}
				<div class="rl-capped-note">
					банк чата почти пуст — выплата урезана до {result.payout}¥ (не полный множитель).
					Баланс наверху мог не измениться, если урезанная выплата = твоей ставке.
				</div>
			{/if}
		</div>
	{/if}

	{#if error}
		<div class="rl-error">{error}</div>
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
					disabled={spinning}
					onclick={() => (bet = v)}
				>
					{v}
				</button>
			{/each}
		</div>
	</div>

	<button type="button" class="rl-cta" disabled={spinning} onclick={spin}>
		<span class="rl-cta-label">{spinning ? 'крутим…' : 'КРУТИТЬ РУЛЕТКУ'}</span>
		<span class="rl-cta-sub">{spinning ? '' : `ставка ${bet}¥`}</span>
	</button>
</div>

<style>
	.rl-screen {
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

	.rl-type-picker {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: var(--space-xs);
	}
	.rl-type {
		font-family: var(--font-chrome);
		font-size: var(--font-body-size);
		text-transform: uppercase;
		padding: var(--space-sm);
	}

	.rl-value-row {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-md);
		display: flex;
		align-items: center;
		gap: var(--space-md);
	}
	.rl-value-label {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		font-family: var(--font-body);
		text-transform: lowercase;
	}
	.rl-number-input {
		font-family: var(--font-numeric);
		font-size: var(--font-heading-size);
		font-weight: 900;
		background: var(--bg-dominant);
		color: var(--text-primary);
		border: 2px solid var(--border-secondary);
		border-radius: 8px;
		padding: var(--space-xs) var(--space-sm);
		width: 80px;
		margin-left: auto;
	}
	.rl-color-row {
		gap: var(--space-sm);
	}
	.rl-color {
		flex: 1;
		text-align: center;
		font-family: var(--font-chrome);
		text-transform: uppercase;
	}
	.rl-color-red.chip-on {
		background: var(--destructive-text);
	}
	.rl-color-black.chip-on {
		background: #333;
		color: #fff;
	}

	.rl-result {
		border-radius: 14px;
		padding: var(--space-lg);
		text-align: center;
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
	}
	.rl-result-flash {
		font-family: var(--font-numeric);
		font-size: var(--font-hero-size);
		font-weight: 900;
		line-height: 1;
	}
	.rl-spin-red {
		color: var(--destructive-text);
	}
	.rl-spin-black {
		color: var(--text-primary);
	}
	.rl-spin-green {
		color: var(--accent-yellow);
	}
	.rl-result-text {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		margin-top: var(--space-sm);
	}
	.rl-win {
		border-color: var(--accent-cyan);
	}
	.rl-win .rl-result-text {
		color: var(--accent-cyan);
	}
	.rl-lose .rl-result-text {
		color: var(--destructive-text);
	}
	.rl-capped-note {
		margin-top: var(--space-sm);
		font-size: 12px;
		line-height: 1.4;
		color: var(--accent-yellow);
		font-family: var(--font-body);
	}

	.rl-error {
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
		color: var(--accent-cyan);
		margin-left: 1px;
	}
	.bet-chips {
		display: grid;
		grid-template-columns: repeat(5, 1fr);
		gap: var(--space-xs);
		flex: 1;
	}

	.rl-cta {
		background: var(--accent-cyan);
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
	.rl-cta:active:not(:disabled) {
		transform: translate(2px, 2px);
		box-shadow: 2px 2px 0 #111;
	}
	.rl-cta:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.rl-cta-label {
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		color: #071820;
		letter-spacing: 0.04em;
	}
	.rl-cta-sub {
		font-size: 12px;
		color: #0d2530;
		font-family: var(--font-body);
	}
</style>
