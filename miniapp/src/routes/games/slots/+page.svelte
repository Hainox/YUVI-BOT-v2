<script lang="ts">
	// Slot ("Azumanga") — server-side port of webapp/slot-machine.jsx +
	// slot-reel.jsx (04.2-10). Server is the SOLE source of truth for the
	// grid/wins/freespins (D-03/T-04.1-01, slot_engine.py) — this screen
	// only animates a generic reel-spin CSS effect, then reveals whatever
	// POST /games/slots actually returned. No client-side RNG, no
	// client-side win computation (SLOT_SYMBOLS/SLOT_PAYLINES in lib/
	// slotData.ts are rendering metadata only — name/tint/cell-highlighting,
	// never probability/payout).
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';
	import { SLOT_SYMBOLS, SLOT_PAYLINES, symbolSrc } from '$lib/slotData';

	const BET_CHIPS = [10, 50, 100, 500, 1000]; // все кратны TOTAL_LINES=10 (slot_engine.py)
	const SPIN_BASE_MS = 700;
	const SPIN_PER_COL_MS = 140;
	const REVEAL_DELAY_MS = SPIN_BASE_MS + SPIN_PER_COL_MS * 4 + 80;

	type SlotWin = { line_index: number; symbol: string; count: number; payout: number };
	type SlotResult = {
		game: string;
		bet: number;
		payout: number;
		outcome: { grid: string[][]; wins: SlotWin[]; freespins: number; scatter: number };
	};

	let bet = $state(BET_CHIPS[0]);
	let spinning = $state(false);
	let grid = $state<string[][]>(_placeholderGrid());
	let wins = $state<SlotWin[]>([]);
	let freespins = $state(0);
	let scatterCount = $state(0);
	let lastPayout = $state<number | null>(null);
	let activeWinIdx = $state(0);
	let error = $state<string | null>(null);

	function _placeholderGrid(): string[][] {
		const ids = Object.keys(SLOT_SYMBOLS);
		return [0, 1, 2].map((r) => [0, 1, 2, 3, 4].map((c) => ids[(r * 5 + c) % ids.length]));
	}

	const highlightCells = $derived.by(() => {
		if (spinning || wins.length === 0) return new Set<string>();
		const win = wins[activeWinIdx];
		if (!win) return new Set<string>();
		const line = SLOT_PAYLINES[win.line_index];
		if (!line) return new Set<string>();
		return new Set(line.map((row, col) => `${row}:${col}`));
	});

	// Cycle through multiple line-wins every 1.3s (mirrors webapp/slot-machine.jsx).
	$effect(() => {
		if (spinning || wins.length <= 1) {
			activeWinIdx = 0;
			return;
		}
		const t = setInterval(() => {
			activeWinIdx = (activeWinIdx + 1) % wins.length;
		}, 1300);
		return () => clearInterval(t);
	});

	async function spin() {
		if (spinning) return;
		error = null;
		wins = [];
		lastPayout = null;
		scatterCount = 0;
		spinning = true;
		haptic('spin');

		let res: SlotResult;
		try {
			res = await apiFetch<SlotResult>('/api/v1/games/slots', {
				method: 'POST',
				body: JSON.stringify({ bet, idem_key: `slots:${crypto.randomUUID()}` })
			});
		} catch (err) {
			spinning = false;
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
			haptic('error');
			return;
		}

		window.setTimeout(() => {
			grid = res.outcome.grid;
			wins = res.outcome.wins;
			freespins = res.outcome.freespins;
			scatterCount = res.outcome.scatter;
			lastPayout = res.payout;
			spinning = false;
			haptic('reel-stop');

			if (scatterCount >= 3) {
				haptic('scatter');
			} else if (res.payout >= bet * 20) {
				haptic('big-win');
			} else if (res.payout > 0) {
				haptic('win');
			} else {
				haptic('lose');
			}
		}, REVEAL_DELAY_MS);
	}
</script>

<div class="slot-screen">
	<div class="menu-head">
		<h1 class="menu-title">Слот</h1>
		<div class="menu-sub">3×5 · 10 линий · вайлд/скаттер/фриспины</div>
	</div>

	{#if freespins > 0}
		<div class="slot-freespins-pill">✦ {freespins} фриспинов доиграно автоматически</div>
	{/if}

	<div class="slot-reels {spinning ? 'slot-spinning' : ''}">
		{#each [0, 1, 2, 3, 4] as col (col)}
			<div class="slot-col" style={`--col-delay: ${col * SPIN_PER_COL_MS}ms`}>
				{#each [0, 1, 2] as row (row)}
					{@const symId = grid[row][col]}
					{@const sym = SLOT_SYMBOLS[symId]}
					{@const hit = highlightCells.has(`${row}:${col}`)}
					<div class="slot-cell {hit ? 'slot-cell-hit' : ''}" style={`--tint: ${sym?.tint ?? '#333'}`}>
						<img src={symbolSrc(symId)} alt={sym?.name ?? symId} draggable="false" />
						{#if sym?.role === 'wild'}<span class="slot-badge slot-badge-wild">WILD</span>{/if}
						{#if sym?.role === 'scatter'}<span class="slot-badge slot-badge-scatter"
								>SCATTER</span
							>{/if}
					</div>
				{/each}
			</div>
		{/each}
	</div>

	<div class="slot-ticker">
		{#if spinning}
			<span class="slot-ticker-spin">крутимся…</span>
		{:else if scatterCount >= 3}
			<span class="slot-ticker-scatter"
				>СКАТТЕР ×{scatterCount}! → {freespins} фриспинов отыграно</span
			>
		{:else if wins.length > 0}
			{@const w = wins[activeWinIdx]}
			<span class="slot-ticker-win">
				{SLOT_SYMBOLS[w.symbol]?.name ?? w.symbol} ×{w.count} · линия {w.line_index + 1} · +{w.payout}¥
			</span>
		{:else if lastPayout !== null}
			<span class="slot-ticker-lose">не в этот раз — крути ещё</span>
		{:else}
			<span class="slot-ticker-idle">жми · крути барабаны</span>
		{/if}
	</div>

	{#if lastPayout !== null && !spinning}
		<div class={`slot-result ${lastPayout > 0 ? 'slot-win' : 'slot-lose'}`}>
			{lastPayout > 0 ? `+${lastPayout}¥` : `−${bet}¥`}
		</div>
	{/if}

	{#if error}
		<div class="slot-error">{error}</div>
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

	<button type="button" class="slot-cta" disabled={spinning} onclick={spin}>
		<span class="slot-cta-label">{spinning ? 'крутим…' : 'КРУТИТЬ'}</span>
		<span class="slot-cta-sub">{spinning ? '' : `ставка ${bet}¥`}</span>
	</button>
</div>

<style>
	.slot-screen {
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

	.slot-freespins-pill {
		align-self: flex-start;
		background: var(--accent-yellow);
		color: #1a0f12;
		border-radius: 999px;
		padding: 4px 12px;
		font-size: 12px;
		font-weight: 700;
		font-family: var(--font-body);
	}

	.slot-reels {
		display: flex;
		gap: 6px;
		padding: 12px;
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 14px;
		/* Pitfall 4: keep animated reel content padded off the screen edge so
		   it never collides with Telegram's own edge-swipe-to-close gesture. */
		margin: 0 2px;
		overflow: hidden;
	}
	.slot-col {
		display: flex;
		flex-direction: column;
		gap: 6px;
		flex: 1;
		min-width: 0;
	}
	.slot-cell {
		position: relative;
		aspect-ratio: 1;
		border-radius: 8px;
		overflow: hidden;
		background: color-mix(in srgb, var(--tint) 25%, var(--bg-secondary-2));
		border: 1px solid var(--border-secondary);
	}
	.slot-cell img {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
	}
	.slot-cell-hit {
		border-color: var(--accent-pink);
		box-shadow: 0 0 0 2px var(--accent-pink);
	}
	.slot-badge {
		position: absolute;
		bottom: 2px;
		left: 2px;
		right: 2px;
		font-size: 8px;
		text-align: center;
		font-weight: 900;
		border-radius: 4px;
		padding: 1px 0;
		font-family: var(--font-body);
	}
	.slot-badge-wild {
		background: #ffd84a;
		color: #1a0f12;
	}
	.slot-badge-scatter {
		background: #ff5b8d;
		color: #1a0f12;
	}
	.slot-spinning .slot-cell {
		animation: reelBlur 900ms ease-in-out;
		animation-delay: var(--col-delay);
	}
	@keyframes reelBlur {
		0% {
			filter: blur(0);
			transform: translateY(0);
		}
		45% {
			filter: blur(4px);
			transform: translateY(-6px);
		}
		100% {
			filter: blur(0);
			transform: translateY(0);
		}
	}

	.slot-ticker {
		min-height: 22px;
		text-align: center;
		font-family: var(--font-body);
		font-size: var(--font-body-size);
	}
	.slot-ticker-win {
		color: var(--accent-pink);
		font-weight: 700;
	}
	.slot-ticker-scatter {
		color: var(--accent-yellow);
		font-weight: 700;
	}
	.slot-ticker-idle,
	.slot-ticker-lose {
		color: var(--text-muted);
	}

	.slot-result {
		text-align: center;
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
	}
	.slot-win {
		color: var(--accent-pink);
	}
	.slot-lose {
		color: var(--destructive-text);
	}

	.slot-error {
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

	.slot-cta {
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
	.slot-cta:active:not(:disabled) {
		transform: translate(2px, 2px);
		box-shadow: 2px 2px 0 #111;
	}
	.slot-cta:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.slot-cta-label {
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		color: #1a0f12;
		letter-spacing: 0.04em;
	}
	.slot-cta-sub {
		font-size: 12px;
		color: #3a1420;
		font-family: var(--font-body);
	}
</style>
