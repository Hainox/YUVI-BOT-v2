<script lang="ts">
	// Market Detail — option list with pool-share bars (AMM inline-graphic
	// visual language, 04.2-UI-SPEC.md §Component Inventory "Market Detail"),
	// BET_CHIPS amount picker, "ПОСТАВИТЬ" CTA (Copywriting Contract). No
	// confirm dialog per the Copywriting Contract ("Placing a market bet is a
	// financial commitment but NOT gated behind a confirm dialog"). On
	// resolution, the winning option gets a Hero-tier reveal banner (pink
	// accent, same "jackpot theater" treatment as duel/coinflip/gacha).
	//
	// Server is the sole source of truth for pools/outcome — this screen only
	// renders whatever GET/POST /api/v1/markets/{id} return. Balance updates
	// arrive via lib/api.ts's balance-sniffing (instant, this tab) AND the
	// SSE stream seeded in +layout.svelte (other tabs/actions).
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	const BET_CHIPS = [10, 50, 100, 500, 1000];

	type MarketOption = { id: number; position: number; label: string; pool: number; share_pct: number };
	type MarketDetail = {
		id: number;
		question: string;
		status: string;
		closes_at: string;
		total_pool: number;
		winning_option_id: number | null;
		options: MarketOption[];
	};
	type BetResult = {
		replayed: boolean;
		bet_id?: number;
		option_position: number;
		amount: number;
		user_balance_after: number;
	};

	const marketId = Number(page.params.id);

	let loading = $state(true);
	let error = $state<string | null>(null);
	let market = $state<MarketDetail | null>(null);

	let selectedPosition = $state<number | null>(null);
	let bet = $state(BET_CHIPS[0]);
	let placing = $state(false);
	let placeError = $state<string | null>(null);
	let placeResult = $state<BetResult | null>(null);

	function describeError(err: unknown): string {
		return err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
	}

	async function load() {
		try {
			market = await apiFetch<MarketDetail>(`/api/v1/markets/${marketId}`);
			if (market.options.length > 0) selectedPosition = market.options[0].position;
		} catch (err) {
			error = describeError(err);
		} finally {
			loading = false;
		}
	}

	onMount(load);

	function selectedOption(): MarketOption | null {
		return market?.options.find((o) => o.position === selectedPosition) ?? null;
	}

	function winningOption(): MarketOption | null {
		if (!market || market.winning_option_id == null) return null;
		return market.options.find((o) => o.id === market!.winning_option_id) ?? null;
	}

	async function placeBet() {
		if (placing || !market || selectedPosition == null) return;
		placing = true;
		placeError = null;
		placeResult = null;
		try {
			const res = await apiFetch<BetResult>(`/api/v1/markets/${marketId}/bets`, {
				method: 'POST',
				body: JSON.stringify({
					option_position: selectedPosition,
					amount: bet,
					ref_id: `market_bet:${crypto.randomUUID()}`
				})
			});
			placeResult = res;
			haptic('win');
			await load(); // pools change after a bet — refresh share bars
		} catch (err) {
			placeError = describeError(err);
			haptic('error');
		} finally {
			placing = false;
		}
	}
</script>

{#if loading}
	<div class="screen-loading"><span>загрузка рынка…</span></div>
{:else if error}
	<div class="mkd-error">{error}</div>
{:else if market}
	<div class="mkd-screen">
		<div class="menu-head">
			<h1 class="menu-title">{market.question}</h1>
			<div class="menu-sub">суммарный пул {market.total_pool}¥ · {market.options.length} вариантов</div>
		</div>

		{#if market.status === 'resolved' && winningOption()}
			<div class="mkd-reveal">
				<div class="mkd-reveal-flash">ПОБЕДИЛ</div>
				<div class="mkd-reveal-option">«{winningOption()!.label}»</div>
			</div>
		{:else if market.status !== 'open'}
			<div class="mkd-status-note">
				Рынок {market.status === 'cancelled' ? 'отменён' : market.status} — ставки закрыты.
			</div>
		{/if}

		<div class="mkd-options">
			{#each market.options as opt (opt.id)}
				{@const isWinner = market.winning_option_id === opt.id}
				<button
					type="button"
					class={`mkd-opt ${selectedPosition === opt.position ? 'mkd-opt-selected' : ''} ${isWinner ? 'mkd-opt-winner' : ''}`}
					disabled={market.status !== 'open' || placing}
					onclick={() => (selectedPosition = opt.position)}
				>
					<div class="mkd-opt-top">
						<span class="mkd-opt-label">{opt.label}</span>
						<span class="mkd-opt-pct">{opt.share_pct}%</span>
					</div>
					<div class="mkd-opt-bar-track">
						<div class="mkd-opt-bar-fill" style={`width: ${opt.share_pct}%`}></div>
					</div>
					<div class="mkd-opt-pool">пул {opt.pool}¥</div>
				</button>
			{/each}
		</div>

		{#if placeError}
			<div class="mkd-error">{placeError}</div>
		{/if}

		{#if placeResult}
			<div class="mkd-placed">
				{placeResult.replayed
					? 'Ставка уже была принята ранее.'
					: `Ставка принята: ${placeResult.amount}¥ на «${selectedOption()?.label ?? ''}».`}
			</div>
		{/if}

		{#if market.status === 'open'}
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
							disabled={placing}
							onclick={() => (bet = v)}
						>
							{v}
						</button>
					{/each}
				</div>
			</div>

			<button type="button" class="mkd-cta" disabled={placing || selectedPosition == null} onclick={placeBet}>
				<span class="mkd-cta-label">{placing ? 'ставим…' : 'ПОСТАВИТЬ'}</span>
				<span class="mkd-cta-sub">
					{placing ? '' : `${bet}¥ на «${selectedOption()?.label ?? ''}»`}
				</span>
			</button>
		{/if}
	</div>
{/if}

<style>
	.mkd-screen {
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
		font-size: var(--font-heading-size);
		font-weight: 700;
		margin: 0;
		color: var(--text-primary);
		line-height: 1.3;
	}
	.menu-sub {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		margin-top: var(--space-xs);
		letter-spacing: 0.04em;
		font-family: var(--font-body);
	}

	.mkd-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.mkd-status-note {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 10px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		color: var(--text-muted);
		font-family: var(--font-body);
	}

	/* Resolution reveal — Hero-tier "jackpot theater" moment (04.2-UI-SPEC.md
	   Component Inventory: "the winning option gets a Hero-tier (64px) reveal
	   banner in the tile's assigned pink accent"). */
	.mkd-reveal {
		border-radius: 14px;
		padding: var(--space-lg);
		text-align: center;
		background: var(--bg-secondary-2);
		border: 2px solid var(--accent-pink);
	}
	.mkd-reveal-flash {
		font-family: var(--font-numeric);
		font-size: var(--font-hero-size);
		font-weight: 900;
		text-transform: uppercase;
		line-height: 1;
		color: var(--accent-pink);
	}
	.mkd-reveal-option {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		margin-top: var(--space-sm);
		color: var(--text-primary);
	}

	.mkd-options {
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.mkd-opt {
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 12px;
		padding: var(--space-sm) var(--space-md);
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
		text-align: left;
		cursor: pointer;
		font-family: inherit;
		color: inherit;
	}
	.mkd-opt:disabled {
		cursor: not-allowed;
	}
	.mkd-opt-selected {
		border-color: var(--accent-cyan);
	}
	.mkd-opt-winner {
		border-color: var(--accent-pink);
	}
	.mkd-opt-top {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
	}
	.mkd-opt-label {
		font-family: var(--font-chrome);
		font-size: var(--font-body-size);
		font-weight: 700;
		color: var(--text-primary);
	}
	.mkd-opt-pct {
		font-family: var(--font-numeric);
		font-size: var(--font-body-size);
		font-weight: 900;
		color: var(--accent-cyan);
	}
	/* Pool-share bar — same visual language as the AMM price-impact inline
	   graphic on the Farm screen (04.2-UI-SPEC.md: "thin #1c1827-surface bar
	   with an accent-cyan fill"). */
	.mkd-opt-bar-track {
		height: 8px;
		border-radius: 4px;
		background: var(--bg-dominant);
		overflow: hidden;
	}
	.mkd-opt-bar-fill {
		height: 100%;
		background: var(--accent-cyan);
		border-radius: 4px;
		transition: width 0.25s ease;
	}
	.mkd-opt-pool {
		font-size: 11px;
		color: var(--text-muted);
		font-family: var(--font-body);
	}

	.mkd-placed {
		background: var(--bg-secondary-2);
		border: 2px solid var(--accent-cyan);
		border-radius: 12px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		color: var(--text-primary);
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

	.mkd-cta {
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
	.mkd-cta:active:not(:disabled) {
		transform: translate(2px, 2px);
		box-shadow: 2px 2px 0 #111;
	}
	.mkd-cta:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.mkd-cta-label {
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		color: #1a0f12;
		letter-spacing: 0.04em;
	}
	.mkd-cta-sub {
		font-size: 12px;
		color: #3a1420;
		font-family: var(--font-body);
	}
</style>
