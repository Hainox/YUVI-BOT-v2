<script lang="ts">
	// Coinflip — first playable game (CASINO-01 vertical slice). Two large
	// tappable chip-style heads/tails toggles (04-UI-SPEC.md §Component
	// Inventory: "chip/chip-on pattern"), BET_CHIPS amount picker reused from
	// the slot screen's bet-controls, primary CTA copy locked verbatim
	// ("ПОДКИНУТЬ МОНЕТУ", 04-UI-SPEC.md Copywriting Contract).
	//
	// Server is the sole source of truth for the outcome (D-03/T-04.1-01) —
	// this screen only renders whatever POST /games/coinflip returns. Balance
	// updates arrive via lib/api.ts's balance-sniffing (instant, this tab) AND
	// the SSE stream seeded in +layout.svelte (other tabs/actions) — no local
	// balance mutation here.
	//
	// Coin animation (this pass): a CSS 3D flip-card coin (rotateY +
	// backface-visibility, same technique as any flip-card) dramatizes the
	// already-known server result — it never decides it. Sequence: toss
	// (brief lift/scale cue) -> spin (several full rotateY turns landing
	// exactly on the server's face) -> land (short squash) -> only THEN is
	// `result` assigned, which is what reveals the existing text/payout panel
	// below. `flipping` still gates the whole sequence exactly like before,
	// it just now spans toss+spin+land instead of a single instant fetch.
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	const BET_CHIPS = [10, 50, 100, 500, 1000];

	// Animation timings (ms) — kept as named constants so the JS sequencing
	// and the CSS transition durations (bound in via inline custom
	// properties below) share one source of truth.
	const TOSS_MS = 260;
	const SPIN_MS = 900;
	const LAND_MS = 320;
	const SPIN_TURNS = 4; // extra full rotations before landing, purely cosmetic

	type CoinflipResult = {
		game: string;
		bet: number;
		payout: number;
		outcome: { result: 'heads' | 'tails'; won: boolean };
		// D-06: chat_bank couldn't cover the full bet*1.98 payout, so the win
		// was capped (in the worst case down to exactly `bet`, i.e. balance
		// doesn't change even though the round was won) — see api/routes/games.py.
		bank_capped?: boolean;
	};

	let bet = $state(BET_CHIPS[0]);
	let choice = $state<'heads' | 'tails'>('heads');
	let flipping = $state(false);
	let result = $state<CoinflipResult | null>(null);
	let error = $state<string | null>(null);

	// Coin visual state. `rotation` accumulates (never resets) so every flip
	// keeps spinning forward from wherever the coin currently sits — no
	// backward snap between rounds. `phase` drives which CSS animation class
	// is active on the coin markup below.
	let rotation = $state(0);
	let phase = $state<'idle' | 'toss' | 'spin' | 'land'>('idle');

	function sleep(ms: number): Promise<void> {
		return new Promise((resolve) => window.setTimeout(resolve, ms));
	}

	function prefersReducedMotion(): boolean {
		return typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
	}

	// Smallest forward rotation from `current` that (a) lands the coin's
	// rotateY on the correct face (0deg = heads, 180deg = tails, mod 360) and
	// (b) always includes SPIN_TURNS full extra turns so it visibly spins
	// even when the coin already happens to be resting on the right face.
	function nextRotation(current: number, face: 'heads' | 'tails'): number {
		const targetMod = face === 'heads' ? 0 : 180;
		const currentMod = ((current % 360) + 360) % 360;
		let delta = targetMod - currentMod;
		if (delta < 0) delta += 360;
		return current + SPIN_TURNS * 360 + delta;
	}

	async function flip() {
		if (flipping) return;
		flipping = true;
		error = null;
		result = null;
		const reduced = prefersReducedMotion();

		phase = 'toss';
		haptic('spin');
		try {
			// Toss cue plays before we even know the outcome — purely a "the
			// coin left your hand" cue, does not depend on the server.
			await sleep(reduced ? 0 : TOSS_MS);

			const res = await apiFetch<CoinflipResult>('/api/v1/games/coinflip', {
				method: 'POST',
				body: JSON.stringify({
					bet,
					choice,
					idem_key: `coinflip:${crypto.randomUUID()}`
				})
			});

			// Server result is known now — the spin's landing angle is derived
			// from it, but the multi-turn spin itself is cosmetic dramatization.
			rotation = nextRotation(rotation, res.outcome.result);
			phase = 'spin';
			await sleep(reduced ? 0 : SPIN_MS);

			phase = 'land';
			result = res;
			haptic(res.outcome.won ? 'win' : 'lose');
			await sleep(reduced ? 0 : LAND_MS);
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
			haptic('error');
		} finally {
			phase = 'idle';
			flipping = false;
		}
	}
</script>

<div class="cf-screen">
	<div class="menu-head">
		<h1 class="menu-title">Монетка</h1>
		<div class="menu-sub">орёл/решка · 50/50</div>
	</div>

	<div class="cf-picker">
		<button
			type="button"
			class={`chip cf-side ${choice === 'heads' ? 'chip-on' : ''}`}
			disabled={flipping}
			onclick={() => (choice = 'heads')}
		>
			орёл
		</button>
		<button
			type="button"
			class={`chip cf-side ${choice === 'tails' ? 'chip-on' : ''}`}
			disabled={flipping}
			onclick={() => (choice = 'tails')}
		>
			решка
		</button>
	</div>

	<div class="cf-coin-stage" aria-hidden="true">
		<div
			class="cf-coin-wrap {phase === 'toss' ? 'cf-toss' : ''} {phase === 'land' ? 'cf-land' : ''}"
			style={`--toss-dur: ${TOSS_MS}ms`}
		>
			<div class="cf-coin" style={`--rotate: ${rotation}deg; --spin-dur: ${SPIN_MS}ms`}>
				<div class="cf-face cf-face-heads">
					<span class="cf-face-glyph">О</span>
					<span class="cf-face-label">орёл</span>
				</div>
				<div class="cf-face cf-face-tails">
					<span class="cf-face-glyph">Р</span>
					<span class="cf-face-label">решка</span>
				</div>
			</div>
		</div>
	</div>

	{#if result}
		<div class={`cf-result ${result.outcome.won ? 'cf-win' : 'cf-lose'}`}>
			<div class="cf-result-flash">
				{result.outcome.result === 'heads' ? 'орёл' : 'решка'}
			</div>
			<div class="cf-result-text">
				{result.outcome.won ? `+${result.payout}¥` : `−${result.bet}¥`}
			</div>
			{#if result.bank_capped}
				<div class="cf-capped-note">
					банк чата почти пуст — выплата урезана до {result.payout}¥ (не полные ×1.98).
					Баланс наверху мог не измениться, если урезанная выплата = твоей ставке.
				</div>
			{/if}
		</div>
	{/if}

	{#if error}
		<div class="cf-error">{error}</div>
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
					disabled={flipping}
					onclick={() => (bet = v)}
				>
					{v}
				</button>
			{/each}
		</div>
	</div>

	<button type="button" class="cf-cta" disabled={flipping} onclick={flip}>
		<span class="cf-cta-label">{flipping ? 'подкидываем…' : 'ПОДКИНУТЬ МОНЕТУ'}</span>
		<span class="cf-cta-sub">{flipping ? '' : `ставка ${bet}¥`}</span>
	</button>
</div>

<style>
	.cf-screen {
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

	.cf-picker {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--space-sm);
	}
	.cf-side {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		text-transform: uppercase;
		padding: var(--space-md);
	}

	/* ─── coin — CSS 3D flip-card technique (rotateY + backface-visibility).
	   Purely cosmetic: `rotation`'s landing angle is derived from an already-
	   known server result, this never decides the outcome. ───────────────── */
	.cf-coin-stage {
		display: flex;
		justify-content: center;
		padding: var(--space-sm) 0 var(--space-xs);
		perspective: 900px;
	}
	.cf-coin-wrap {
		width: 128px;
		height: 128px;
		transition: transform var(--toss-dur, 260ms) cubic-bezier(0.34, 1.4, 0.64, 1);
	}
	/* Pre-flip toss cue: the coin lifts and grows slightly before it starts
	   spinning, so it reads as an actual physical toss upward, not a spin
	   appearing in place. */
	.cf-coin-wrap.cf-toss {
		transform: translateY(-16px) scale(1.08);
	}
	.cf-coin-wrap.cf-land {
		animation: cfLandSquash 320ms ease-out;
	}
	@keyframes cfLandSquash {
		0% {
			transform: scale(1, 1);
		}
		35% {
			transform: scale(0.9, 1.1);
		}
		65% {
			transform: scale(1.06, 0.94);
		}
		100% {
			transform: scale(1, 1);
		}
	}
	.cf-coin {
		position: relative;
		width: 100%;
		height: 100%;
		transform-style: preserve-3d;
		transform: rotateY(var(--rotate, 0deg));
		transition: transform var(--spin-dur, 900ms) cubic-bezier(0.22, 0.61, 0.36, 1);
	}
	.cf-face {
		position: absolute;
		inset: 0;
		border-radius: 50%;
		backface-visibility: hidden;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 2px;
		background: var(--bg-secondary-2);
		border: 3px solid var(--accent-pink);
		box-shadow:
			0 6px 0 #111,
			0 6px 18px rgba(0, 0, 0, 0.35);
	}
	.cf-face-tails {
		transform: rotateY(180deg);
	}
	.cf-face-glyph {
		font-family: var(--font-numeric);
		font-size: 42px;
		font-weight: 900;
		line-height: 1;
		color: var(--accent-pink);
	}
	.cf-face-label {
		font-family: var(--font-body);
		font-size: 11px;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--text-secondary);
	}
	@media (prefers-reduced-motion: reduce) {
		.cf-coin-wrap,
		.cf-coin-wrap.cf-toss,
		.cf-coin-wrap.cf-land,
		.cf-coin {
			transition: none !important;
			animation: none !important;
			transform: rotateY(var(--rotate, 0deg)) !important;
		}
	}

	.cf-result {
		border-radius: 14px;
		padding: var(--space-lg);
		text-align: center;
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
	}
	.cf-result-flash {
		font-family: var(--font-numeric);
		font-size: var(--font-hero-size);
		font-weight: 900;
		text-transform: uppercase;
		line-height: 1;
		color: var(--text-primary);
	}
	.cf-result-text {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		margin-top: var(--space-sm);
	}
	.cf-win {
		border-color: var(--accent-pink);
	}
	.cf-win .cf-result-text {
		color: var(--accent-pink);
	}
	.cf-lose .cf-result-text {
		color: var(--destructive-text);
	}
	.cf-capped-note {
		margin-top: var(--space-sm);
		font-size: 12px;
		line-height: 1.4;
		color: var(--accent-yellow);
		font-family: var(--font-body);
	}

	.cf-error {
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

	.cf-cta {
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
	.cf-cta:active:not(:disabled) {
		transform: translate(2px, 2px);
		box-shadow: 2px 2px 0 #111;
	}
	.cf-cta:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.cf-cta-label {
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		color: #1a0f12;
		letter-spacing: 0.04em;
	}
	.cf-cta-sub {
		font-size: 12px;
		color: #3a1420;
		font-family: var(--font-body);
	}
</style>
