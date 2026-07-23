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
	//
	// Gauge animation: dice is over/under against a 1-100 target, not two
	// physical d6, so the payoff animation is a needle sweeping a 1-100 track
	// and decelerating onto the roll — never rolling cubes. The animation
	// dramatizes an ALREADY-KNOWN server result: POST resolves first, and only
	// then do outcome.roll/outcome.won drive the needle. Before the response
	// lands, the needle/readout jitter through cosmetic random 1-100 values
	// (same idea as games/slots' filler-symbol reel) that never preview or
	// determine the real outcome. Once the response is in, the needle is set
	// straight to the true roll/won and only the CSS transition is left to
	// visually settle, followed by a timed reveal of the payout panel — the
	// same "swap to true state, then timed reveal" idiom as games/slots'
	// SPIN_BASE_MS/REVEAL_DELAY_MS.
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	const BET_CHIPS = [10, 50, 100, 500, 1000];
	const DICE_HOUSE_EDGE = 0.02; // casino_service.DICE_HOUSE_EDGE, informational mirror only
	const SEEK_INTERVAL_MS = 55; // cosmetic jitter tick while the request is in flight
	const SETTLE_MS = 650; // needle deceleration duration before the payout panel reveals

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

	// Gauge animation state — purely cosmetic, never feeds back into
	// bet/target/direction or the settled result. `phase` sequences the
	// needle: idle (nothing rolled yet) -> seeking (cosmetic jitter while the
	// request is in flight) -> settling (eased CSS transition onto the true,
	// already-known roll) -> done (payout panel revealed).
	type GaugePhase = 'idle' | 'seeking' | 'settling' | 'done';
	let phase = $state<GaugePhase>('idle');
	let gaugeValue = $state(50);
	let gaugeWon = $state<boolean | null>(null);
	let prefersReducedMotion = $state(false);

	$effect(() => {
		const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
		prefersReducedMotion = mq.matches;
		const onChange = (e: MediaQueryListEvent) => (prefersReducedMotion = e.matches);
		mq.addEventListener('change', onChange);
		return () => mq.removeEventListener('change', onChange);
	});

	const winProb = $derived(
		direction === 'under' ? (target - 1) / 100 : (100 - target) / 100
	);
	const mult = $derived(winProb > 0 ? (1 - DICE_HOUSE_EDGE) / winProb : 0);

	// Maps a 1..100 roll/target value onto a 0..100% gauge track position.
	function gaugePct(v: number): number {
		return ((v - 1) / 99) * 100;
	}
	const targetPct = $derived(gaugePct(target));
	const zoneLeftPct = $derived(direction === 'under' ? 0 : targetPct);
	const zoneWidthPct = $derived(direction === 'under' ? targetPct : 100 - targetPct);
	const needlePct = $derived(gaugePct(gaugeValue));

	async function roll() {
		if (rolling) return;
		rolling = true;
		error = null;
		result = null;
		gaugeWon = null;
		phase = prefersReducedMotion ? 'idle' : 'seeking';
		haptic('spin');

		// Cosmetic jitter while awaiting the server — never determines the
		// outcome, only ever overwritten by the real roll below.
		let seekTimer: number | null = null;
		if (!prefersReducedMotion) {
			gaugeValue = Math.floor(Math.random() * 100) + 1;
			seekTimer = window.setInterval(() => {
				gaugeValue = Math.floor(Math.random() * 100) + 1;
			}, SEEK_INTERVAL_MS);
		}

		let res: DiceResult;
		try {
			res = await apiFetch<DiceResult>('/api/v1/games/dice', {
				method: 'POST',
				body: JSON.stringify({
					bet,
					target,
					direction,
					idem_key: `dice:${crypto.randomUUID()}`
				})
			});
		} catch (err) {
			if (seekTimer !== null) window.clearInterval(seekTimer);
			phase = 'idle';
			rolling = false;
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
			haptic('error');
			return;
		}

		if (seekTimer !== null) window.clearInterval(seekTimer);
		// Result is known now — set the needle straight to the true roll/won.
		// Everything from here is presentation timing only.
		gaugeValue = res.outcome.roll;
		gaugeWon = res.outcome.won;

		if (prefersReducedMotion) {
			phase = 'done';
			result = res;
			rolling = false;
			haptic(res.outcome.won ? 'win' : 'lose');
			return;
		}

		phase = 'settling';
		window.setTimeout(() => {
			phase = 'done';
			result = res;
			rolling = false;
			haptic(res.outcome.won ? 'win' : 'lose');
		}, SETTLE_MS);
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

	<div class="dice-gauge">
		<div class="dice-gauge-label">шкала 1–100</div>
		<div class="dice-gauge-track">
			<div class="dice-gauge-bar">
				<div
					class="dice-gauge-zone"
					style={`left:${zoneLeftPct}%;width:${zoneWidthPct}%;`}
				></div>
				<div class="dice-gauge-target-line" style={`left:${targetPct}%;`}></div>
			</div>
			{#if phase !== 'idle'}
				<div
					class="dice-gauge-needle
						{phase === 'settling' || phase === 'done' ? 'dice-needle-settling' : ''}
						{gaugeWon === true ? 'dice-needle-win' : ''}
						{gaugeWon === false ? 'dice-needle-lose' : ''}"
					style={`left:${needlePct}%;`}
				></div>
			{/if}
		</div>
		<div class="dice-gauge-scale">
			<span>1</span>
			<span>50</span>
			<span>100</span>
		</div>
		<div
			class="dice-gauge-readout
				{gaugeWon === true ? 'dice-readout-win' : ''}
				{gaugeWon === false ? 'dice-readout-lose' : ''}
				{phase === 'seeking' ? 'dice-readout-seeking' : ''}"
		>
			{phase === 'idle' ? '—' : gaugeValue}
		</div>
	</div>

	{#if result}
		<div class={`dice-result ${result.outcome.won ? 'dice-win' : 'dice-lose'}`}>
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

	/* ─── gauge — needle sweeping the 1-100 over/under range (see roll() for
	   the animation sequencing: cosmetic jitter -> settle onto the true,
	   already-known roll -> reveal). Win/lose is communicated exclusively via
	   --positive/--destructive; --accent-pink stays reserved for this screen's
	   neutral "in play" state (win-zone tint, seeking-phase needle). ────── */
	.dice-gauge {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-md);
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.dice-gauge-label {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		font-family: var(--font-body);
		text-transform: lowercase;
	}
	.dice-gauge-track {
		position: relative;
		height: 14px;
		margin: var(--space-sm) 10px 0;
	}
	.dice-gauge-bar {
		position: absolute;
		inset: 0;
		border-radius: 999px;
		background: var(--bg-dominant);
		border: 1px solid var(--border-secondary);
		overflow: hidden;
	}
	.dice-gauge-zone {
		position: absolute;
		top: 0;
		bottom: 0;
		background: color-mix(in srgb, var(--accent-pink) 22%, transparent);
	}
	.dice-gauge-target-line {
		position: absolute;
		top: -3px;
		bottom: -3px;
		width: 2px;
		background: var(--text-primary);
		opacity: 0.75;
	}
	.dice-gauge-needle {
		position: absolute;
		top: 50%;
		left: 0%;
		transform: translate(-50%, -50%);
		width: 18px;
		height: 18px;
		border-radius: 50%;
		background: var(--accent-pink);
		border: 2px solid #111;
		box-shadow: 2px 2px 0 #111;
		z-index: 1;
	}
	.dice-needle-win {
		background: var(--positive);
	}
	.dice-needle-lose {
		background: var(--destructive);
	}
	.dice-gauge-scale {
		display: flex;
		justify-content: space-between;
		font-family: var(--font-body);
		font-size: 11px;
		color: var(--text-muted);
		padding: 0 2px;
	}
	.dice-gauge-readout {
		font-family: var(--font-numeric);
		font-size: var(--font-hero-size);
		font-weight: 900;
		line-height: 1;
		text-align: center;
		color: var(--text-primary);
		min-height: 56px;
		display: flex;
		align-items: center;
		justify-content: center;
	}
	.dice-readout-win {
		color: var(--positive-text);
		text-shadow: 0 0 18px rgba(46, 224, 106, 0.4);
	}
	.dice-readout-lose {
		color: var(--destructive-text);
		text-shadow: 0 0 18px rgba(255, 56, 56, 0.35);
	}
	/* Motion only runs when the OS allows it — under reduced motion the needle
	   and readout still appear, but they pop straight into the final,
	   already-known state instead of sweeping/decelerating into it. */
	@media (prefers-reduced-motion: no-preference) {
		.dice-gauge-needle {
			transition: left 0.05s linear;
		}
		.dice-gauge-needle.dice-needle-settling {
			transition:
				left 0.65s cubic-bezier(0.16, 0.86, 0.32, 1),
				background 0.2s ease,
				box-shadow 0.2s ease;
		}
		.dice-readout-seeking {
			animation: dice-readout-flicker 0.1s steps(2) infinite;
		}
	}
	@keyframes dice-readout-flicker {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0.72;
		}
	}

	.dice-result {
		border-radius: 14px;
		padding: var(--space-lg);
		text-align: center;
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
	}
	.dice-result-text {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		margin-top: var(--space-sm);
	}
	.dice-win {
		border-color: var(--positive);
	}
	.dice-win .dice-result-text {
		color: var(--positive-text);
	}
	.dice-lose {
		border-color: var(--destructive);
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
