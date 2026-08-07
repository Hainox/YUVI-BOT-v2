<script lang="ts">
	// Crash — real-time round: start (debit stake, server picks a hidden
	// crash point) -> ONE possible follow-up action, cashOut (settle). Same
	// two-request shape as blackjack's deal->action (see its +page.svelte
	// header comment for the full "server-authoritative state machine"
	// rationale), just with a single follow-up action instead of three, and
	// that action isn't chosen from client state (hit/stand/double) — it's
	// "whatever real wall-clock time has elapsed", computed entirely by the
	// server (bot/services/casino_service.py::crash_cash_out /
	// bot/services/crash_engine.py::multiplier_at).
	//
	// D-03/T-04.1-01: the server is the SOLE source of truth for the outcome.
	// The multiplier ticking up on screen between start and cashOut is a
	// PURELY COSMETIC animation reconstructing crash_engine.multiplier_at's
	// same growth curve (e^(CRASH_GROWTH_RATE*t)) client-side for visual
	// smoothness — it is never read to decide win/loss, only cashOut()'s
	// response (outcome.result/crash_point/cashed_out_multiplier) does that.
	// If this screen's local clock drifts from the server's, the WORST case
	// is the number on screen looking briefly wrong for a frame before the
	// real response snaps it straight (see _applySettled below) — never a
	// wrong payout, because no payout math happens here at all.
	//
	// Balance hold (lib/balance.ts): unlike blackjack/dice/roulette (which
	// release the hold immediately once an action leaves them in a
	// non-settled state, because nothing about their "still in progress"
	// state involves money changing further), crash's hold spans the WHOLE
	// round: from the instant start() fires until cashOut()'s settle reveal
	// actually lands on screen. Rationale: start() already debits the stake
	// (balance drops immediately), and the round can run for several real
	// seconds before settling (unlike blackjack's active phase, which is
	// just waiting on the PLAYER's next tap) — releasing the hold right
	// after start would let the header show the debited balance, sit there
	// for however long the player watches the multiplier climb, then jump
	// again at cashout. Holding through the whole thing keeps the header
	// silent until there's an actual result to reveal, which is the whole
	// point of a "hold" (see lib/balance.ts's docstring). Released in the
	// catch block of BOTH start() and cashOut(), in the settle-reveal timer,
	// and again as a safety net in onDestroy.
	import { onDestroy } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';
	import { holdBalanceUpdates } from '$lib/balance';
	import { haptic } from '$lib/tg';
	import BetControl from '$lib/components/BetControl.svelte';

	// Mirrors bot/services/crash_engine.py exactly (CRASH_GROWTH_RATE,
	// CRASH_MAX_MULTIPLIER) — confirmed against that file, not guessed.
	// CRASH_GROWTH_RATE only feeds the COSMETIC ticker below; CRASH_MAX_MULTIPLIER
	// caps it too, purely so the animation never displays a value the server
	// could never actually report (real crash points are capped there).
	const CRASH_GROWTH_RATE = 0.173; // e^(0.173*t) -> 2x at t≈4.0066s
	const CRASH_MAX_MULTIPLIER = 100;

	// Settle-then-reveal pause before the win/bust panel appears — same
	// "SETTLE_MS"-style idiom as games/dice's SETTLE_MS (its +page.svelte),
	// same value, so cashing out reads with the same weight as any other
	// instant-result game's payoff beat.
	const SETTLE_MS = 650;

	function prefersReducedMotion(): boolean {
		return typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
	}

	type CrashOutcome = { result: 'cashed_out' | 'busted'; multiplier: string };
	type CrashView = {
		id: number;
		status: 'active' | 'settled';
		bet: number;
		started_at?: string | null;
		// crash_point/cashed_out_multiplier arrive as strings (server-side
		// Decimal serialized to string, api/routes/games.py) — parseFloat'd
		// for display only in _applySettled, never used for payout math.
		crash_point?: string;
		cashed_out_multiplier?: string | null;
		payout?: number;
		outcome?: CrashOutcome;
		user_balance_after?: number;
		bank_capped?: boolean;
	};

	let phase = $state<'idle' | 'active' | 'settled'>('idle');
	let bet = $state(10);
	let busy = $state(false); // start() request in flight
	let cashingOut = $state(false); // cashOut() request in flight
	let error = $state<string | null>(null);

	let gameId = $state<number | null>(null);
	let roundBet = $state(0); // server-confirmed bet for the ACTIVE round (v.bet), not the editable `bet` above
	let outcome = $state<CrashOutcome | null>(null);
	let payout = $state<number | null>(null);
	let crashPoint = $state<number | null>(null);
	let cashedOutMultiplier = $state<number | null>(null);
	let bankCapped = $state(false);
	let busted = $state(false);
	let resultVisible = $state(false);

	// Cosmetic ticker state ONLY — never read by start()/cashOut(), never
	// feeds a request body. `liveMultiplier` is what's rendered; `clientStartMs`
	// is the local performance.now() anchor captured the instant start()'s
	// response arrived (deliberately NOT the server's `started_at` — this
	// loop never talks to the server again until the player cashes out).
	let liveMultiplier = $state(1);
	let clientStartMs: number | null = null;
	let rafId: number | null = null;
	let settleTimer: ReturnType<typeof setTimeout> | null = null;

	function _tick() {
		if (phase !== 'active' || clientStartMs === null) {
			rafId = null;
			return;
		}
		const elapsedSec = (performance.now() - clientStartMs) / 1000;
		const raw = Math.exp(CRASH_GROWTH_RATE * elapsedSec);
		const capped = Math.min(raw, CRASH_MAX_MULTIPLIER);
		// Floor to 2 decimals, same rounding discipline as
		// crash_engine.multiplier_at (ROUND_DOWN) — purely so the cosmetic
		// number ticks in the same discrete steps the real curve would,
		// not because this value is ever compared against anything.
		liveMultiplier = Math.floor(capped * 100) / 100;
		rafId = requestAnimationFrame(_tick);
	}

	function _startTicker() {
		if (rafId !== null) cancelAnimationFrame(rafId);
		rafId = requestAnimationFrame(_tick);
	}

	function _stopTicker() {
		if (rafId !== null) {
			cancelAnimationFrame(rafId);
			rafId = null;
		}
	}

	function _clearSettleTimer() {
		if (settleTimer !== null) {
			clearTimeout(settleTimer);
			settleTimer = null;
		}
	}

	// Informational mirror only (same spirit as dice's client-side `mult`
	// readout) — bet*liveMultiplier off the COSMETIC number, shown so the
	// player has a sense of what tapping ЗАБРАТЬ right now would roughly be
	// worth. The real payout is whatever cashOut()'s response says, always.
	const potentialPayout = $derived(Math.floor(roundBet * liveMultiplier));
	// 0-100% climb indicator for the meter bar, log-shaped so early growth
	// (where most rounds actually end) still reads as visible movement —
	// caps at 10x, purely decorative.
	const meterPct = $derived(Math.min(100, ((liveMultiplier - 1) / 9) * 100));

	function _applyStart(v: CrashView) {
		gameId = v.id;
		roundBet = v.bet;
		outcome = null;
		payout = null;
		crashPoint = null;
		cashedOutMultiplier = null;
		bankCapped = false;
		busted = false;
		resultVisible = false;
		liveMultiplier = 1;
		phase = 'active';
		clientStartMs = performance.now();
		_startTicker();
	}

	function _applySettled(v: CrashView) {
		phase = 'settled';
		outcome = v.outcome ?? null;
		payout = v.payout ?? 0;
		bankCapped = v.bank_capped ?? false;
		crashPoint = v.crash_point !== undefined ? parseFloat(v.crash_point) : null;
		cashedOutMultiplier = v.cashed_out_multiplier ? parseFloat(v.cashed_out_multiplier) : null;
		busted = outcome?.result === 'busted';
		_stopTicker();
		// Snap the displayed number to the server's actual truth — never leave
		// it wherever the local cosmetic loop happened to be sitting when this
		// landed. Covers BOTH ways busted can be discovered (cashed out too
		// late, or the round had already been force-settled and this same
		// idempotent cashOut() call is just the way of finding out) — this
		// function doesn't care how the settle response arrived, only what it
		// says: busted explodes at crash_point, cashed_out freezes at
		// cashed_out_multiplier.
		liveMultiplier = (busted ? crashPoint : cashedOutMultiplier) ?? liveMultiplier;

		const reveal = () => {
			resultVisible = true;
			haptic(busted ? 'lose' : 'win');
			// The settle reveal (exploded/frozen number + result panel) just
			// actually landed on screen — safe to let the header catch up now.
			// See the module doc comment above for why the hold spans the
			// whole round instead of releasing right after start().
			holdBalanceUpdates(false);
			cashingOut = false;
		};
		if (prefersReducedMotion()) {
			reveal();
		} else {
			settleTimer = setTimeout(reveal, SETTLE_MS);
		}
	}

	async function start() {
		if (busy) return;
		busy = true;
		error = null;
		haptic('spin');
		holdBalanceUpdates(true);
		try {
			const res = await apiFetch<CrashView>('/api/v1/games/crash', {
				method: 'POST',
				body: JSON.stringify({ bet, idem_key: `crash:${crypto.randomUUID()}` })
			});
			gameId = res.id;
			roundBet = res.bet;
			if (res.status === 'active') {
				_applyStart(res);
			} else {
				// api/routes/games.py's post_crash_start docstring: an idempotent
				// idem_key replay (client never saw the original response, e.g. a
				// dropped connection, and retried) CAN land on a round the 15s
				// timeout job already force-settled while orphaned — the only
				// outcome reachable this way is "busted" (there's no game_id to
				// have ever called cashout with on this client). No ticker to
				// start; go straight to the settled render.
				_applySettled(res);
			}
		} catch (err) {
			holdBalanceUpdates(false);
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
			haptic('error');
		} finally {
			busy = false;
		}
	}

	async function cashOut() {
		if (phase !== 'active' || cashingOut || gameId === null) return;
		cashingOut = true;
		error = null;
		haptic('tap');
		// Fires immediately — does NOT wait for any rAF/animation-frame
		// boundary of the cosmetic ticker above (spec requirement: the
		// player's actual cash-out moment is whenever they tap, not whenever
		// the local loop next redraws). Re-asserts the hold set in start()
		// (idempotent — holdBalanceUpdates just sets a flag) so a retry after
		// a failed attempt below re-acquires it.
		holdBalanceUpdates(true);
		try {
			const res = await apiFetch<CrashView>(`/api/v1/games/crash/${gameId}/cashout`, {
				method: 'POST'
			});
			_applySettled(res);
		} catch (err) {
			holdBalanceUpdates(false);
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
			haptic('error');
			cashingOut = false;
			// Round is still active server-side — nothing here changed that —
			// so the ticker (gated on phase === 'active') just keeps climbing
			// and the player can tap ЗАБРАТЬ again.
		}
	}

	function playAgain() {
		phase = 'idle';
		gameId = null;
		roundBet = 0;
		outcome = null;
		payout = null;
		crashPoint = null;
		cashedOutMultiplier = null;
		bankCapped = false;
		busted = false;
		resultVisible = false;
		liveMultiplier = 1;
		error = null;
	}

	onDestroy(() => {
		_stopTicker();
		_clearSettleTimer();
		// Screen navigated away mid-round (active, or between cashOut()'s
		// response and its own settle-reveal timer firing) — don't leave the
		// hold stuck forever for other screens reading the shared balance
		// store. Same safety net every other casino screen has, see
		// lib/balance.ts.
		holdBalanceUpdates(false);
	});
</script>

<div class="crash-screen">
	<div class="menu-head">
		<h1 class="menu-title">Краш</h1>
		<div class="menu-sub">множитель растёт с каждой секундой — жми ЗАБРАТЬ до краша</div>
	</div>

	{#if phase !== 'idle'}
		<div
			class={`crash-table ${phase === 'settled' && resultVisible ? (busted ? 'crash-table-lose' : 'crash-table-win') : ''}`}
		>
			<div
				class={`crash-readout
					${phase === 'active' ? 'crash-readout-active' : ''}
					${phase === 'settled' && resultVisible && busted ? 'crash-readout-busted' : ''}
					${phase === 'settled' && resultVisible && !busted ? 'crash-readout-win' : ''}`}
			>
				×{liveMultiplier.toFixed(2)}
			</div>
			<div class="crash-readout-sub">
				{#if phase === 'active'}
					потенциальный выигрыш {potentialPayout}¥
				{:else if phase === 'settled' && resultVisible}
					{busted
						? `сгорело на ×${(crashPoint ?? 0).toFixed(2)}`
						: `забрано на ×${(cashedOutMultiplier ?? 0).toFixed(2)}`}
				{/if}
			</div>
			{#if phase === 'active'}
				<div class="crash-meter">
					<div class="crash-meter-fill" style={`width:${meterPct}%`}></div>
				</div>
			{/if}
		</div>
	{/if}

	{#if phase === 'settled' && resultVisible}
		<div class={`crash-result ${busted ? 'crash-result-lose' : 'crash-result-win'}`}>
			<div class="crash-result-label">{busted ? 'СГОРЕЛО' : 'ЗАБРАЛ'}</div>
			<div class="crash-result-amount">{busted ? `−${roundBet}¥` : `+${payout}¥`}</div>
			{#if bankCapped}
				<div class="crash-capped-note">
					банк чата почти пуст — выплата урезана до {payout ?? 0}¥ (не по полному множителю).
				</div>
			{/if}
		</div>
	{/if}

	{#if error}
		<div class="crash-error">{error}</div>
	{/if}

	{#if phase === 'idle'}
		<BetControl bind:bet disabled={busy} />

		<button type="button" class="crash-cta" disabled={busy} onclick={start}>
			<span class="crash-cta-label">{busy ? 'запускаем…' : 'СТАРТ'}</span>
			<span class="crash-cta-sub">{busy ? '' : `ставка ${bet}¥`}</span>
		</button>
	{:else if phase === 'active'}
		<button type="button" class="crash-cashout" disabled={cashingOut} onclick={cashOut}>
			<span class="crash-cashout-label">{cashingOut ? 'забираем…' : 'ЗАБРАТЬ'}</span>
			<span class="crash-cashout-sub"
				>{cashingOut ? '' : `×${liveMultiplier.toFixed(2)} · ${potentialPayout}¥`}</span
			>
		</button>
	{:else}
		<button type="button" class="crash-cta" onclick={playAgain}>
			<span class="crash-cta-label">ЕЩЁ РАУНД</span>
		</button>
	{/if}
</div>

<style>
	.crash-screen {
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

	.crash-table {
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-lg) var(--space-md);
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--space-sm);
		transition:
			border-color 0.3s ease-out,
			box-shadow 0.3s ease-out;
	}
	/* Same "positive vs destructive" table-wide color grade as blackjack's
	   .bj-table-win/.bj-table-lose, gated on resultVisible the same way. */
	.crash-table-win {
		border-color: var(--positive);
		box-shadow:
			0 0 0 2px var(--positive),
			0 0 26px rgba(46, 224, 106, 0.28);
	}
	.crash-table-lose {
		border-color: var(--destructive);
		box-shadow:
			0 0 0 2px var(--destructive),
			0 0 22px rgba(255, 56, 56, 0.22);
	}

	.crash-readout {
		font-family: var(--font-numeric);
		font-size: var(--font-hero-size);
		font-weight: 900;
		line-height: 1;
		text-align: center;
		color: var(--text-primary);
	}
	.crash-readout-active {
		color: var(--accent-yellow);
	}
	@media (prefers-reduced-motion: no-preference) {
		.crash-readout-active {
			animation: crashPulse 900ms ease-in-out infinite;
		}
	}
	@keyframes crashPulse {
		0%,
		100% {
			transform: scale(1);
			text-shadow: 0 0 18px rgba(255, 216, 74, 0.35);
		}
		50% {
			transform: scale(1.035);
			text-shadow: 0 0 30px rgba(255, 216, 74, 0.55);
		}
	}
	.crash-readout-win {
		color: var(--positive-text);
	}
	.crash-readout-busted {
		color: var(--destructive-text);
	}
	/* The "explosion" — a sharp shake/scale burst landing back at rest, so
	   busting reads as an actual event instead of the number just silently
	   stopping. Runs once (settle-reveal only fires once per round). */
	@media (prefers-reduced-motion: no-preference) {
		.crash-readout-busted {
			animation: crashExplode 480ms cubic-bezier(0.36, 0.07, 0.19, 0.97) both;
		}
	}
	@keyframes crashExplode {
		0% {
			transform: scale(1) rotate(0deg);
		}
		20% {
			transform: scale(1.25) rotate(-2deg);
		}
		40% {
			transform: scale(0.92) rotate(2deg);
		}
		60% {
			transform: scale(1.08) rotate(-1deg);
		}
		100% {
			transform: scale(1) rotate(0deg);
		}
	}

	.crash-readout-sub {
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		color: var(--text-muted);
		text-align: center;
		min-height: 18px;
	}

	.crash-meter {
		width: 100%;
		height: 8px;
		border-radius: 999px;
		background: var(--bg-dominant);
		border: 1px solid var(--border-secondary);
		overflow: hidden;
	}
	.crash-meter-fill {
		height: 100%;
		background: linear-gradient(90deg, var(--accent-yellow), var(--accent-pink));
		border-radius: 999px;
	}
	@media (prefers-reduced-motion: no-preference) {
		.crash-meter-fill {
			transition: width 0.05s linear;
		}
	}

	.crash-result {
		text-align: center;
		border-radius: 14px;
		padding: var(--space-lg);
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		animation: crashResultPop 420ms cubic-bezier(0.2, 0.9, 0.25, 1.2) both;
	}
	@keyframes crashResultPop {
		0% {
			opacity: 0;
			transform: scale(0.82) translateY(10px);
		}
		60% {
			opacity: 1;
			transform: scale(1.04) translateY(0);
		}
		100% {
			opacity: 1;
			transform: scale(1) translateY(0);
		}
	}
	.crash-result-win {
		border-color: var(--positive);
	}
	.crash-result-lose {
		border-color: var(--destructive);
	}
	.crash-result-label {
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-primary);
	}
	.crash-result-amount {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		margin-top: var(--space-sm);
	}
	.crash-result-win .crash-result-amount {
		color: var(--positive-text);
	}
	.crash-result-lose .crash-result-amount {
		color: var(--destructive-text);
	}
	.crash-capped-note {
		margin-top: var(--space-sm);
		font-size: 12px;
		line-height: 1.4;
		color: var(--accent-yellow);
		font-family: var(--font-body);
	}

	.crash-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.crash-cta,
	.crash-cashout {
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
	.crash-cta {
		background: var(--accent-pink);
	}
	.crash-cashout {
		background: var(--accent-yellow);
	}
	.crash-cta:active:not(:disabled),
	.crash-cashout:active:not(:disabled) {
		transform: translate(2px, 2px);
		box-shadow: 2px 2px 0 #111;
	}
	.crash-cta:disabled,
	.crash-cashout:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.crash-cta-label,
	.crash-cashout-label {
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		color: #1a0f12;
		letter-spacing: 0.04em;
	}
	.crash-cta-sub,
	.crash-cashout-sub {
		font-size: 12px;
		color: #3a1420;
		font-family: var(--font-body);
		min-height: 14px;
	}

	/* Collapse every animation/transition on this screen to effectively
	   instant for prefers-reduced-motion — the readout/meter/result still end
	   up in the correct final state, just without visible motion. JS-side
	   settle-reveal timing (SETTLE_MS) is skipped the same way in the script
	   above via prefersReducedMotion(). */
	@media (prefers-reduced-motion: reduce) {
		.crash-screen * {
			animation-duration: 0.01ms !important;
			animation-delay: 0s !important;
			transition-duration: 0.01ms !important;
		}
	}
</style>
