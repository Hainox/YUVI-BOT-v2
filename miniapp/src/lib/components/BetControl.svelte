<script lang="ts">
	// Shared bet-amount picker for the 5 casino screens (dice/coinflip/
	// roulette/slots/blackjack) — chip presets + free-form custom amount +
	// all-in. Backend already accepts any bet (casino_service._validate_bet:
	// min=casino_min_bet, max=balance*casino_max_bet_pct) — this is a pure
	// frontend affordance, server stays the sole source of truth for what's
	// actually allowed (an out-of-range value just surfaces the existing
	// InvalidBet message via the caller's error state, same as an over-balance
	// chip pick already did).
	//
	// `step` rounds custom/all-in amounts down to a valid multiple — slots
	// requires bet % TOTAL_LINES(10) == 0 (bot/services/casino_service.py),
	// other games pass step=1 (the default).
	import { balance } from '$lib/balance';

	let {
		bet = $bindable(),
		disabled = false,
		step = 1,
		chips = [10, 50, 100, 500, 1000],
		label = 'ставка'
	}: {
		bet: number;
		disabled?: boolean;
		step?: number;
		chips?: number[];
		label?: string;
	} = $props();

	let customText = $state('');

	function pickChip(v: number) {
		bet = v;
		customText = '';
	}

	// Live-parse while typing so the top "ставка" readout tracks keystrokes;
	// rounding to `step` only happens on blur (see onCustomBlur) so mid-typing
	// values like "15" aren't clobbered before the user reaches "150".
	$effect(() => {
		const n = parseInt(customText, 10);
		if (customText !== '' && Number.isFinite(n) && n > 0) {
			bet = n;
		}
	});

	function onCustomBlur() {
		if (customText === '') return;
		const rounded = Math.max(step, Math.floor(bet / step) * step);
		bet = rounded;
		customText = String(rounded);
	}

	function setAllIn() {
		const rounded = Math.max(step, Math.floor(($balance ?? 0) / step) * step);
		bet = rounded;
		customText = String(rounded);
	}
</script>

<div class="bet-row">
	<div class="bet-display">
		<span class="bet-label">{label}</span>
		<div class="bet-amount">{bet}<small>¥</small></div>
	</div>
	<div class="bet-chips">
		{#each chips as v (v)}
			<button
				type="button"
				class={`chip ${bet === v && customText === '' ? 'chip-on' : ''}`}
				{disabled}
				onclick={() => pickChip(v)}
			>
				{v}
			</button>
		{/each}
	</div>
</div>
<div class="bet-custom-row">
	<input
		type="number"
		inputmode="numeric"
		class="bet-custom-input"
		placeholder="своя сумма"
		min={step}
		{step}
		{disabled}
		bind:value={customText}
		onblur={onCustomBlur}
	/>
	<button type="button" class="chip chip-all bet-allin-btn" {disabled} onclick={setAllIn}>
		ВСЕ
	</button>
</div>

<style>
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
	.bet-custom-row {
		display: flex;
		align-items: center;
		gap: var(--space-xs);
		margin-top: var(--space-xs);
	}
	.bet-custom-input {
		flex: 1;
		min-width: 0;
		background: #fff;
		border: 2px solid #111;
		border-radius: 8px;
		padding: var(--space-xs) var(--space-sm);
		font-family: var(--font-numeric);
		font-size: var(--font-body-size);
		font-weight: 700;
		color: #111;
		min-height: 44px;
	}
	.bet-custom-input:disabled {
		opacity: 0.4;
	}
	.bet-allin-btn {
		flex-shrink: 0;
		padding-left: var(--space-md);
		padding-right: var(--space-md);
	}
</style>
