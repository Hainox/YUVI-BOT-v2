<script lang="ts">
	// Roulette — European wheel (0-36), real casino betting table + wheel-spin
	// reveal (CASINO-01, 04.2-03). Backend already supports all 5 bet types
	// (number/color/parity/half/dozen) via bot/services/casino_service.py —
	// this file is a pure frontend rebuild: a real 0-36 table replaces the
	// old dropdown/radio picker, and the known server result is now revealed
	// through an animated wheel instead of an instant text flash. Same
	// structural pattern as games/coinflip and games/dice: BET_CHIPS amount
	// picker, apiFetch POST, server-authoritative result, haptic.
	//
	// Server is the sole source of truth for the spin (D-03/T-04.1-01) — the
	// red-number set / wheel order below are purely client-side display
	// mirrors of bot/services/casino_service.py's public D-03 color table,
	// used only to paint the table/wheel, never to compute an outcome. The
	// wheel animation is 100% cosmetic: it starts only after the server has
	// already returned `outcome.spin`, and simply spins forward to land on
	// that already-known number (same "informational reveal" spirit as the
	// blackjack card-flip work) — nothing about the round outcome is decided
	// or guessed client-side.
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	const BET_CHIPS = [10, 50, 100, 500, 1000];
	const RED_NUMBERS = new Set([
		1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36
	]);
	const NUMBERS = Array.from({ length: 36 }, (_, i) => i + 1);

	// Standard European (single-zero) wheel pocket order, clockwise from 0 —
	// used ONLY to lay out/animate the wheel graphic, not to pick a result.
	const EUROPEAN_WHEEL_ORDER = [
		0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10, 5, 24, 16, 33, 1, 20,
		14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26
	];
	const SECTOR_ANGLE = 360 / EUROPEAN_WHEEL_ORDER.length;
	const WHEEL_SPIN_TURNS = 6;
	const SPIN_DURATION_MS = 3400;
	const REVEAL_BUFFER_MS = 150;

	type BetType = 'number' | 'color' | 'parity' | 'half' | 'dozen';
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
	function spinColorLabel(c: 'red' | 'black' | 'green'): string {
		if (c === 'red') return 'красное';
		if (c === 'black') return 'чёрное';
		return 'зеро';
	}

	function betLabel(type: BetType, value: number | string): string {
		switch (type) {
			case 'number':
				return `число ${value}`;
			case 'color':
				return value === 'red' ? 'красное' : 'чёрное';
			case 'parity':
				return value === 'even' ? 'чётное' : 'нечётное';
			case 'half':
				return value === 'low' ? '1–18' : '19–36';
			case 'dozen':
				return value === 1 ? '1-я дюжина (1–12)' : value === 2 ? '2-я дюжина (13–24)' : '3-я дюжина (25–36)';
		}
	}

	// --- wheel geometry (pure display math, computed once, no reactivity) ---
	function polarToCartesian(cx: number, cy: number, r: number, angleDeg: number) {
		const rad = (angleDeg * Math.PI) / 180;
		return { x: cx + r * Math.sin(rad), y: cy - r * Math.cos(rad) };
	}
	function sectorPath(startAngle: number, endAngle: number, r: number): string {
		const p0 = polarToCartesian(100, 100, r, startAngle);
		const p1 = polarToCartesian(100, 100, r, endAngle);
		const largeArc = endAngle - startAngle > 180 ? 1 : 0;
		return `M 100 100 L ${p0.x.toFixed(2)} ${p0.y.toFixed(2)} A ${r} ${r} 0 ${largeArc} 1 ${p1.x.toFixed(2)} ${p1.y.toFixed(2)} Z`;
	}
	function sectorFill(c: 'red' | 'black' | 'green'): string {
		if (c === 'red') return 'var(--destructive-text)';
		if (c === 'green') return 'var(--accent-yellow)';
		return '#333';
	}
	function sectorTextFill(c: 'red' | 'black' | 'green'): string {
		return c === 'black' ? '#fff' : '#1a0a06';
	}

	type WheelSector = {
		number: number;
		color: 'red' | 'black' | 'green';
		path: string;
		labelX: number;
		labelY: number;
		labelRotate: number;
	};
	const WHEEL_SECTORS: WheelSector[] = EUROPEAN_WHEEL_ORDER.map((n, i) => {
		const start = i * SECTOR_ANGLE;
		const end = start + SECTOR_ANGLE;
		const mid = start + SECTOR_ANGLE / 2;
		const label = polarToCartesian(100, 100, 78, mid);
		return {
			number: n,
			color: spinColor(n),
			path: sectorPath(start, end, 92),
			labelX: label.x,
			labelY: label.y,
			labelRotate: mid
		};
	});

	let bet = $state(BET_CHIPS[0]);
	let selectedBetType = $state<BetType | null>(null);
	let selectedBetValue = $state<number | string | null>(null);
	let spinning = $state(false);
	let wheelRotation = $state(0);
	let result = $state<RouletteResult | null>(null);
	let error = $state<string | null>(null);

	const ctaSub = $derived(
		spinning
			? ''
			: selectedBetType !== null && selectedBetValue !== null
				? `${bet}¥ на ${betLabel(selectedBetType, selectedBetValue)}`
				: 'выбери ставку на столе'
	);

	function selectBet(type: BetType, value: number | string) {
		if (spinning) return;
		selectedBetType = type;
		selectedBetValue = value;
		haptic('tap');
	}
	function isSelectedCell(type: BetType, value: number | string): boolean {
		return selectedBetType === type && selectedBetValue === value;
	}
	// Lights up every number that would win the currently selected outside
	// bet (e.g. picking "красное" glows all red cells) — display-only, mirrors
	// the same win condition casino_service._roulette_win applies server-side.
	function isInSelection(n: number): boolean {
		if (selectedBetType === null || selectedBetValue === null) return false;
		if (selectedBetType === 'color') return spinColor(n) === selectedBetValue;
		if (selectedBetType === 'parity') return (n % 2 === 0 ? 'even' : 'odd') === selectedBetValue;
		if (selectedBetType === 'half') return (n <= 18 ? 'low' : 'high') === selectedBetValue;
		if (selectedBetType === 'dozen') return (n <= 12 ? 1 : n <= 24 ? 2 : 3) === selectedBetValue;
		return false;
	}

	// Spins the wheel forward (never backward/snapping) to land the given
	// server-confirmed number under the fixed top pointer.
	function spinWheelTo(spinNumber: number) {
		const idx = EUROPEAN_WHEEL_ORDER.indexOf(spinNumber);
		const targetMid = idx * SECTOR_ANGLE + SECTOR_ANGLE / 2;
		const current = ((wheelRotation % 360) + 360) % 360;
		const desiredFinalMod = ((360 - (targetMid % 360)) % 360 + 360) % 360;
		let delta = desiredFinalMod - current;
		if (delta <= 0) delta += 360;
		wheelRotation += WHEEL_SPIN_TURNS * 360 + delta;
	}

	async function spin() {
		if (spinning || selectedBetType === null || selectedBetValue === null) return;
		spinning = true;
		error = null;
		result = null;
		haptic('spin');
		try {
			const res = await apiFetch<RouletteResult>('/api/v1/games/roulette', {
				method: 'POST',
				body: JSON.stringify({
					bet,
					bet_type: selectedBetType,
					bet_value: selectedBetValue,
					idem_key: `roulette:${crypto.randomUUID()}`
				})
			});
			spinWheelTo(res.outcome.spin);
			setTimeout(() => {
				result = res;
				spinning = false;
				haptic(res.outcome.won ? 'win' : 'lose');
			}, SPIN_DURATION_MS + REVEAL_BUFFER_MS);
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
			haptic('error');
			spinning = false;
		}
	}
</script>

<div class="rl-screen">
	<div class="menu-head">
		<h1 class="menu-title">Рулетка</h1>
		<div class="menu-sub">европейское колесо · 0–36</div>
	</div>

	<div class={`rl-wheel-wrap ${spinning ? 'rl-wheel-spinning' : ''}`}>
		<svg viewBox="0 0 200 200" class="rl-wheel-svg" aria-hidden="true">
			<g
				style={`transform-origin:100px 100px; transform:rotate(${wheelRotation}deg); transition: transform ${SPIN_DURATION_MS}ms cubic-bezier(0.19, 0.68, 0.24, 1);`}
			>
				{#each WHEEL_SECTORS as s (s.number)}
					<path d={s.path} fill={sectorFill(s.color)} stroke="#111" stroke-width="0.6" />
				{/each}
				{#each WHEEL_SECTORS as s (s.number)}
					<text
						x={s.labelX}
						y={s.labelY}
						transform={`rotate(${s.labelRotate} ${s.labelX} ${s.labelY})`}
						text-anchor="middle"
						dominant-baseline="middle"
						class="rl-wheel-label"
						fill={sectorTextFill(s.color)}>{s.number}</text
					>
				{/each}
			</g>
			<circle cx="100" cy="100" r="94" fill="none" stroke="#111" stroke-width="4" />
			<polygon points="90,2 110,2 100,22" fill="var(--accent-cyan)" stroke="#111" stroke-width="2" />
		</svg>
		<div class="rl-hub">
			{#if spinning}
				<span class="rl-hub-dot"></span>
			{:else if result}
				<span class={`rl-hub-num rl-spin-${spinColor(result.outcome.spin)}`}>{result.outcome.spin}</span>
				<span class="rl-hub-color-label">{spinColorLabel(spinColor(result.outcome.spin))}</span>
			{:else}
				<span class="rl-hub-idle">?</span>
			{/if}
		</div>
	</div>

	{#if result}
		<div class={`rl-result ${result.outcome.won ? 'rl-win' : 'rl-lose'}`}>
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

	<div class="rl-selection">
		{#if selectedBetType !== null && selectedBetValue !== null}
			<span class="rl-selection-label">ставка на столе:</span>
			<span class="rl-selection-value">{betLabel(selectedBetType, selectedBetValue)}</span>
		{:else}
			<span class="rl-selection-empty">выбери число или сектор на столе ниже</span>
		{/if}
	</div>

	<div class="rl-table">
		<button
			type="button"
			class={`rl-num rl-num-zero rl-num-green ${isSelectedCell('number', 0) ? 'rl-num-selected' : ''}`}
			disabled={spinning}
			onclick={() => selectBet('number', 0)}
		>
			0
		</button>

		<div class="rl-grid">
			{#each NUMBERS as n (n)}
				<button
					type="button"
					class={`rl-num rl-num-${spinColor(n)} ${isSelectedCell('number', n) ? 'rl-num-selected' : ''} ${isInSelection(n) ? 'rl-num-lit' : ''}`}
					disabled={spinning}
					onclick={() => selectBet('number', n)}
				>
					{n}
				</button>
			{/each}
		</div>

		<div class="rl-outside-row">
			<button
				type="button"
				class={`chip rl-outside ${isSelectedCell('dozen', 1) ? 'rl-outside-on' : ''}`}
				disabled={spinning}
				onclick={() => selectBet('dozen', 1)}
			>
				1–12
			</button>
			<button
				type="button"
				class={`chip rl-outside ${isSelectedCell('dozen', 2) ? 'rl-outside-on' : ''}`}
				disabled={spinning}
				onclick={() => selectBet('dozen', 2)}
			>
				13–24
			</button>
			<button
				type="button"
				class={`chip rl-outside ${isSelectedCell('dozen', 3) ? 'rl-outside-on' : ''}`}
				disabled={spinning}
				onclick={() => selectBet('dozen', 3)}
			>
				25–36
			</button>
		</div>

		<div class="rl-outside-row">
			<button
				type="button"
				class={`chip rl-outside ${isSelectedCell('half', 'low') ? 'rl-outside-on' : ''}`}
				disabled={spinning}
				onclick={() => selectBet('half', 'low')}
			>
				1–18
			</button>
			<button
				type="button"
				class={`chip rl-outside ${isSelectedCell('parity', 'even') ? 'rl-outside-on' : ''}`}
				disabled={spinning}
				onclick={() => selectBet('parity', 'even')}
			>
				чёт
			</button>
			<button
				type="button"
				class={`chip rl-outside rl-outside-red ${isSelectedCell('color', 'red') ? 'rl-outside-on' : ''}`}
				disabled={spinning}
				onclick={() => selectBet('color', 'red')}
			>
				красное
			</button>
			<button
				type="button"
				class={`chip rl-outside ${isSelectedCell('half', 'high') ? 'rl-outside-on' : ''}`}
				disabled={spinning}
				onclick={() => selectBet('half', 'high')}
			>
				19–36
			</button>
			<button
				type="button"
				class={`chip rl-outside ${isSelectedCell('parity', 'odd') ? 'rl-outside-on' : ''}`}
				disabled={spinning}
				onclick={() => selectBet('parity', 'odd')}
			>
				нечет
			</button>
			<button
				type="button"
				class={`chip rl-outside rl-outside-black ${isSelectedCell('color', 'black') ? 'rl-outside-on' : ''}`}
				disabled={spinning}
				onclick={() => selectBet('color', 'black')}
			>
				чёрное
			</button>
		</div>
	</div>

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

	<button type="button" class="rl-cta" disabled={spinning || selectedBetType === null} onclick={spin}>
		<span class="rl-cta-label">{spinning ? 'крутим…' : 'КРУТИТЬ РУЛЕТКУ'}</span>
		<span class="rl-cta-sub">{ctaSub}</span>
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

	/* ─── wheel ─────────────────────────────────────────────────────────── */
	.rl-wheel-wrap {
		position: relative;
		width: min(64vw, 260px);
		aspect-ratio: 1;
		margin: 0 auto;
		border-radius: 50%;
		border: 3px solid #111;
		box-shadow: 6px 6px 0 #111;
		background: var(--bg-secondary-2);
		transition: box-shadow 0.2s;
	}
	.rl-wheel-spinning {
		box-shadow:
			6px 6px 0 #111,
			0 0 24px 4px rgba(123, 230, 255, 0.45);
	}
	.rl-wheel-svg {
		display: block;
		width: 100%;
		height: 100%;
	}
	.rl-wheel-label {
		font-family: var(--font-numeric);
		font-size: 7.5px;
		letter-spacing: -0.02em;
	}
	.rl-hub {
		position: absolute;
		top: 50%;
		left: 50%;
		transform: translate(-50%, -50%);
		width: 36%;
		height: 36%;
		border-radius: 50%;
		background: var(--bg-secondary-1);
		border: 3px solid #111;
		box-shadow: 2px 2px 0 #111;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 2px;
	}
	.rl-hub-idle {
		font-family: var(--font-chrome);
		font-size: 22px;
		color: var(--text-muted);
	}
	.rl-hub-dot {
		width: 14px;
		height: 14px;
		border-radius: 50%;
		background: var(--accent-cyan);
		animation: tokens-pulse 0.9s ease-in-out infinite;
	}
	.rl-hub-num {
		font-family: var(--font-numeric);
		font-size: 30px;
		font-weight: 900;
		line-height: 1;
	}
	.rl-hub-color-label {
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		font-family: var(--font-body);
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

	/* ─── result / error ────────────────────────────────────────────────── */
	.rl-result {
		border-radius: 14px;
		padding: var(--space-lg);
		text-align: center;
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
	}
	.rl-result-text {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
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

	/* ─── current selection strip ───────────────────────────────────────── */
	.rl-selection {
		display: flex;
		align-items: baseline;
		gap: var(--space-sm);
		background: var(--bg-secondary-1);
		border: 1px solid var(--border-secondary);
		border-radius: 10px;
		padding: var(--space-sm) var(--space-md);
	}
	.rl-selection-label {
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--text-muted);
		font-family: var(--font-body);
	}
	.rl-selection-value {
		font-family: var(--font-chrome);
		font-size: var(--font-body-size);
		color: var(--accent-cyan);
	}
	.rl-selection-empty {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		font-family: var(--font-body);
		font-style: italic;
	}

	/* ─── betting table ─────────────────────────────────────────────────── */
	.rl-table {
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-sm);
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}

	.rl-num {
		border: 2px solid #111;
		border-radius: 6px;
		box-shadow: 2px 2px 0 #111;
		font-family: var(--font-numeric);
		font-weight: 900;
		font-size: 15px;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 0;
		cursor: pointer;
		transition:
			transform 0.08s,
			box-shadow 0.08s;
	}
	.rl-num:active:not(:disabled) {
		transform: translate(1px, 1px);
		box-shadow: 1px 1px 0 #111;
	}
	.rl-num:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.rl-num-red {
		background: var(--destructive-text);
		color: #1a0a06;
	}
	.rl-num-black {
		background: #333;
		color: #fff;
	}
	.rl-num-green {
		background: var(--accent-yellow);
		color: #241a02;
	}
	.rl-num-zero {
		width: 100%;
		min-height: 52px;
		font-size: 18px;
	}
	.rl-num-selected {
		outline: 3px solid var(--accent-cyan);
		outline-offset: 1px;
		box-shadow: 2px 2px 0 #111, 0 0 10px rgba(123, 230, 255, 0.7);
		z-index: 1;
	}
	.rl-num-lit:not(.rl-num-selected) {
		outline: 2px dashed rgba(123, 230, 255, 0.65);
		outline-offset: -2px;
	}

	.rl-grid {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 4px;
	}

	.rl-outside-row {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 4px;
	}
	.rl-outside {
		text-align: center;
		font-family: var(--font-chrome);
		font-size: 12px;
		text-transform: uppercase;
	}
	.rl-outside-on {
		background: var(--accent-cyan);
		color: #062028;
	}
	.rl-outside-red.rl-outside-on {
		background: var(--destructive-text);
		color: #1a0a06;
	}
	.rl-outside-black.rl-outside-on {
		background: #333;
		color: #fff;
	}

	/* ─── bet amount + CTA ──────────────────────────────────────────────── */
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
