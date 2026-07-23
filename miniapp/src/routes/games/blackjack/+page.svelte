<script lang="ts">
	// Blackjack — multi-step server-side state machine (04.2-10): start
	// (deal) + hit/stand/double, each step just renders whatever
	// casino_service.start_blackjack/blackjack_action (04.1-03) returns.
	// Server owns the deck/hands entirely (T-04.1-08) — this screen never
	// computes card values to decide the outcome, only to LABEL the
	// player's/dealer's current hand for the player's convenience (same
	// "informational mirror" pattern as dice's multiplier readout).
	//
	// enableClosingConfirmation (tg.ts::init, already on) guards against an
	// accidental swipe-close mid-hand — the stake would otherwise sit on an
	// abandoned "active" game until the 60s server-side auto-stand timeout
	// (D-07/D-08, resolve_blackjack_timeouts) settles it for the player.
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';
	import { parseCard, cardImage, SUIT_THEME } from '$lib/blackjackTheme';

	const BET_CHIPS = [10, 50, 100, 500, 1000];

	type BjOutcome = { result: 'natural' | 'win' | 'push' | 'lose' | 'bust'; player: string[]; dealer: string[] };
	type BjView = {
		id: number;
		status: 'active' | 'settled';
		bet: number;
		player: string[];
		dealer_upcard?: string | null;
		dealer?: string[];
		payout?: number;
		outcome?: BjOutcome;
	};

	const OUTCOME_LABEL: Record<BjOutcome['result'], string> = {
		natural: 'БЛЭКДЖЕК! ×2.5',
		win: 'ПОБЕДА ×2',
		push: 'НИЧЬЯ — ставка возвращена',
		lose: 'ПРОИГРЫШ',
		bust: 'ПЕРЕБОР'
	};

	let phase = $state<'idle' | 'active' | 'settled'>('idle');
	let bet = $state(BET_CHIPS[0]);
	let busy = $state(false);
	let error = $state<string | null>(null);

	let gameId = $state<number | null>(null);
	let player = $state<string[]>([]);
	let dealerUpcard = $state<string | null>(null);
	let dealer = $state<string[] | null>(null);
	let outcome = $state<BjOutcome | null>(null);
	let payout = $state<number | null>(null);

	function _rankValue(rank: string): number {
		if (rank === 'A') return 11;
		if (rank === 'J' || rank === 'Q' || rank === 'K') return 10;
		return Number(rank);
	}
	// Informational mirror of blackjack_engine.hand_value — label only,
	// never used to decide anything server-authoritative. Cards are
	// "rank+suit" tokens (e.g. "A♠") since the Miku/Teto redesign, so the
	// suit glyph is stripped before rank lookup.
	function handTotal(cards: string[]): { value: number; soft: boolean } {
		const ranks = cards.map((c) => parseCard(c).rank);
		let total = ranks.reduce((s, r) => s + _rankValue(r), 0);
		let aces = ranks.filter((r) => r === 'A').length;
		while (total > 21 && aces > 0) {
			total -= 10;
			aces -= 1;
		}
		return { value: total, soft: aces > 0 };
	}

	function _applyView(v: BjView) {
		gameId = v.id;
		player = v.player;
		if (v.status === 'active') {
			phase = 'active';
			dealerUpcard = v.dealer_upcard ?? null;
			dealer = null;
			outcome = null;
			payout = null;
		} else {
			phase = 'settled';
			dealer = v.dealer ?? null;
			outcome = v.outcome ?? null;
			payout = v.payout ?? 0;
			if (outcome) {
				if (outcome.result === 'natural' || outcome.result === 'win') haptic('win');
				else if (outcome.result === 'push') haptic('tap');
				else haptic('lose');
			}
		}
	}

	async function deal() {
		if (busy) return;
		busy = true;
		error = null;
		haptic('spin');
		try {
			const res = await apiFetch<BjView>('/api/v1/games/blackjack', {
				method: 'POST',
				body: JSON.stringify({ bet, idem_key: `blackjack:${crypto.randomUUID()}` })
			});
			_applyView(res);
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
			haptic('error');
		} finally {
			busy = false;
		}
	}

	async function act(action: 'hit' | 'stand' | 'double') {
		if (busy || gameId === null) return;
		busy = true;
		error = null;
		haptic('tap');
		try {
			const res = await apiFetch<BjView>(`/api/v1/games/blackjack/${gameId}/action`, {
				method: 'POST',
				body: JSON.stringify({ action })
			});
			_applyView(res);
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
			haptic('error');
		} finally {
			busy = false;
		}
	}

	function playAgain() {
		phase = 'idle';
		gameId = null;
		player = [];
		dealerUpcard = null;
		dealer = null;
		outcome = null;
		payout = null;
		error = null;
	}
</script>

<svelte:head>
	<link rel="preconnect" href="https://fonts.googleapis.com" />
	<link
		href="https://fonts.googleapis.com/css2?family=Fredoka:wght@600;700&display=swap"
		rel="stylesheet"
	/>
</svelte:head>

{#snippet themedCard(token: string)}
	{@const { rank, suit } = parseCard(token)}
	{@const theme = SUIT_THEME[suit]}
	<span class="bj-card" style={`border-color:${theme.main}`}>
		<img class="bj-card-img" src={cardImage(rank, suit)} alt={`${rank}${suit}`} />
		<span class="bj-card-badge bj-card-badge-tl" style={`background:${theme.main}`}>
			<span class="bj-card-rank">{rank}</span>
			<span class="bj-card-suit">{suit}</span>
		</span>
		<span class="bj-card-badge bj-card-badge-br" style={`background:${theme.main}`}>
			<span class="bj-card-rank">{rank}</span>
			<span class="bj-card-suit">{suit}</span>
		</span>
	</span>
{/snippet}

{#snippet cardBack()}
	<span class="bj-card bj-card-back">
		<img class="bj-card-img" src="/blackjack/back.webp" alt="рубашка карты" />
	</span>
{/snippet}

<div class="bj-screen">
	<div class="menu-head">
		<h1 class="menu-title">Блэкджек</h1>
		<div class="menu-sub">Мику ♠♣ · Тето ♥♦ · натурал 2.5× · дилер бьёт с мягких 17</div>
	</div>

	{#if phase !== 'idle'}
		<div class="bj-table">
			<div class="bj-hand">
				<div class="bj-hand-label">
					дилер {#if phase === 'settled' && dealer}<span class="bj-hand-total"
							>{handTotal(dealer).value}</span
						>{/if}
				</div>
				<div class="bj-cards">
					{#if phase === 'active'}
						{#if dealerUpcard}
							{@render themedCard(dealerUpcard)}
						{/if}
						{@render cardBack()}
					{:else if dealer}
						{#each dealer as c, i (i)}
							{@render themedCard(c)}
						{/each}
					{/if}
				</div>
			</div>

			<div class="bj-hand">
				<div class="bj-hand-label">
					игрок <span class="bj-hand-total">{handTotal(player).value}{handTotal(player).soft ? ' (мягкое)' : ''}</span>
				</div>
				<div class="bj-cards">
					{#each player as c, i (i)}
						{@render themedCard(c)}
					{/each}
				</div>
			</div>
		</div>
	{/if}

	{#if phase === 'settled' && outcome}
		<div class={`bj-result bj-result-${outcome.result}`}>
			<div class="bj-result-label">{OUTCOME_LABEL[outcome.result]}</div>
			<div class="bj-result-amount">
				{(payout ?? 0) > 0 ? `+${payout}¥` : outcome.result === 'push' ? '±0¥' : `−${bet}¥`}
			</div>
		</div>
	{/if}

	{#if error}
		<div class="bj-error">{error}</div>
	{/if}

	{#if phase === 'idle'}
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
						disabled={busy}
						onclick={() => (bet = v)}
					>
						{v}
					</button>
				{/each}
			</div>
		</div>

		<button type="button" class="bj-cta" disabled={busy} onclick={deal}>
			<span class="bj-cta-label">{busy ? 'раздаём…' : 'РАЗДАТЬ'}</span>
			<span class="bj-cta-sub">{busy ? '' : `ставка ${bet}¥`}</span>
		</button>
	{:else if phase === 'active'}
		<div class="bj-actions">
			<button type="button" class="chip bj-action" disabled={busy} onclick={() => act('hit')}>
				ЕЩЁ
			</button>
			<button type="button" class="chip bj-action" disabled={busy} onclick={() => act('stand')}>
				ХВАТИТ
			</button>
			<button
				type="button"
				class="chip bj-action"
				disabled={busy || player.length !== 2}
				onclick={() => act('double')}
			>
				×2
			</button>
		</div>
	{:else}
		<button type="button" class="bj-cta" onclick={playAgain}>
			<span class="bj-cta-label">ЕЩЁ РАЗДАЧУ</span>
		</button>
	{/if}
</div>

<style>
	.bj-screen {
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

	.bj-table {
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-md);
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
	}
	.bj-hand {
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.bj-hand-label {
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--text-muted);
		font-family: var(--font-body);
		display: flex;
		align-items: baseline;
		gap: var(--space-sm);
	}
	.bj-hand-total {
		font-family: var(--font-numeric);
		font-weight: 900;
		color: var(--text-primary);
		text-transform: none;
		letter-spacing: 0;
	}
	.bj-cards {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}
	.bj-card {
		position: relative;
		width: 60px;
		height: 84px;
		flex-shrink: 0;
		border-radius: 9px;
		overflow: hidden;
		background: var(--bg-primary, #111);
		border: 2px solid var(--border-secondary);
		box-shadow: 0 2px 5px rgba(0, 0, 0, 0.35);
	}
	.bj-card-img {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.bj-card-badge {
		position: absolute;
		display: flex;
		flex-direction: column;
		align-items: center;
		line-height: 1;
		padding: 2px 4px;
		border-radius: 5px;
		box-shadow: 0 1px 3px rgba(0, 0, 0, 0.5);
		font-family: 'Fredoka', var(--font-chrome);
	}
	.bj-card-badge-tl {
		top: 4px;
		left: 4px;
	}
	.bj-card-badge-br {
		bottom: 4px;
		right: 4px;
		transform: rotate(180deg);
	}
	.bj-card-rank {
		font-weight: 700;
		font-size: 12px;
		color: #fff;
	}
	.bj-card-suit {
		font-size: 8px;
		color: #fff;
	}
	.bj-card-back {
		border-color: #ede3ce;
	}

	.bj-result {
		text-align: center;
		border-radius: 14px;
		padding: var(--space-lg);
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
	}
	.bj-result-label {
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-primary);
	}
	.bj-result-amount {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		margin-top: var(--space-sm);
	}
	.bj-result-natural .bj-result-amount,
	.bj-result-win .bj-result-amount {
		color: var(--accent-pink);
	}
	.bj-result-lose .bj-result-amount,
	.bj-result-bust .bj-result-amount {
		color: var(--destructive-text);
	}
	.bj-result-push .bj-result-amount {
		color: var(--text-muted);
	}

	.bj-error {
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

	.bj-actions {
		display: grid;
		grid-template-columns: 1fr 1fr 1fr;
		gap: var(--space-sm);
	}
	.bj-action {
		font-family: var(--font-chrome);
		font-size: var(--font-body-size);
		text-transform: uppercase;
		padding: var(--space-md) var(--space-xs);
		text-align: center;
	}

	.bj-cta {
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
	.bj-cta:active:not(:disabled) {
		transform: translate(2px, 2px);
		box-shadow: 2px 2px 0 #111;
	}
	.bj-cta:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.bj-cta-label {
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		color: #1a0f12;
		letter-spacing: 0.04em;
	}
	.bj-cta-sub {
		font-size: 12px;
		color: #3a1420;
		font-family: var(--font-body);
	}
</style>
