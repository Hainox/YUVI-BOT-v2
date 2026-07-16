<script lang="ts">
	// Portfolio — explicit CASINO-02 screen (open positions across all
	// markets, any status). 04-UI-SPEC.md §Component Inventory: "Reuses
	// stats-row/hist-row shape for open positions; balance-card gradient
	// reused for header summary." Empty state copy locked verbatim:
	// "ничего" / "нет открытых ставок. Загляни в /markets в чате."
	//
	// Read-only screen — no client-side balance mutation, no bet placement
	// here (that lives on the Market Detail screen). Tapping a row navigates
	// to that market's detail (Market Detail screen also serves as the
	// natural place to see the row's current live pool state).
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { apiFetch, ApiError } from '$lib/api';

	type Position = {
		bet_id: number;
		market_id: number;
		question: string;
		option_label: string;
		amount: number;
		payout: number | null;
		refunded: boolean;
		market_status: string;
	};

	let loading = $state(true);
	let error = $state<string | null>(null);
	let positions = $state<Position[]>([]);

	let openCount = $derived(positions.filter((p) => p.market_status === 'open').length);
	let openStaked = $derived(
		positions.filter((p) => p.market_status === 'open').reduce((sum, p) => sum + p.amount, 0)
	);

	function statusLabel(p: Position): string {
		if (p.refunded) return 'возврат';
		if (p.market_status === 'open') return 'открыта';
		if (p.market_status === 'resolved') return p.payout ? `выигрыш +${p.payout}¥` : 'проигрыш';
		if (p.market_status === 'cancelled') return 'отменена';
		return p.market_status;
	}

	onMount(async () => {
		try {
			positions = await apiFetch<Position[]>('/api/v1/markets/portfolio');
		} catch (err) {
			error = err instanceof ApiError ? `${err.status}: ${err.message}` : String(err ?? 'unknown_error');
		} finally {
			loading = false;
		}
	});
</script>

<div class="pf-screen">
	<div class="menu-head">
		<h1 class="menu-title">Портфолио</h1>
		<div class="menu-sub">твои открытые позиции</div>
	</div>

	{#if loading}
		<div class="screen-loading"><span>загрузка портфолио…</span></div>
	{:else if error}
		<div class="pf-error">{error}</div>
	{:else if positions.length === 0}
		<div class="pf-empty">
			<div class="pf-empty-jp">なにもない</div>
			<div class="pf-empty-ru">ничего</div>
			<div class="pf-empty-hint">нет открытых ставок. Загляни в /markets в чате.</div>
		</div>
	{:else}
		<div class="balance-card pf-summary">
			<div class="bc-handle">открытых позиций</div>
			<div class="bc-amount">
				<span class="bc-val">{openCount}</span>
				<span class="bc-unit">рынков</span>
			</div>
			<div class="bc-bank">в них поставлено: <strong>{openStaked}¥</strong></div>
		</div>

		<div class="pf-list">
			{#each positions as p (p.bet_id)}
				<button type="button" class="pf-row" onclick={() => goto(`/markets/${p.market_id}`)}>
					<div class="pf-row-top">
						<span class="pf-row-title">{p.question}</span>
						<span class={`pf-row-status pf-status-${p.market_status}`}>{statusLabel(p)}</span>
					</div>
					<div class="pf-row-bottom">
						<span class="pf-row-option">«{p.option_label}»</span>
						<span class="pf-row-amount">{p.amount}<small>¥</small></span>
					</div>
				</button>
			{/each}
		</div>
	{/if}
</div>

<style>
	.pf-screen {
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

	.pf-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.pf-empty {
		text-align: center;
		padding: var(--space-2xl) var(--space-lg);
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.pf-empty-jp {
		font-family: var(--font-jp);
		font-size: var(--font-heading-size);
		color: var(--text-locked);
	}
	.pf-empty-ru {
		font-family: var(--font-chrome);
		font-size: var(--font-display-size);
		font-weight: 700;
		color: var(--text-secondary);
		text-transform: lowercase;
	}
	.pf-empty-hint {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		font-family: var(--font-body);
		line-height: 1.5;
		max-width: 320px;
		margin: var(--space-xs) auto 0;
	}

	.pf-summary {
		margin: 0;
	}

	.pf-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.pf-row {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-md);
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
		text-align: left;
		cursor: pointer;
		font-family: inherit;
		color: inherit;
		min-height: 44px;
	}
	.pf-row:active {
		transform: scale(0.985);
	}
	.pf-row-top {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--space-sm);
	}
	.pf-row-title {
		font-family: var(--font-chrome);
		font-size: var(--font-body-size);
		font-weight: 700;
		color: var(--text-primary);
	}
	.pf-row-status {
		flex-shrink: 0;
		font-size: 10px;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		padding: 3px 8px;
		border-radius: 20px;
		white-space: nowrap;
		background: var(--bg-dominant);
		color: var(--text-muted);
	}
	.pf-status-open {
		color: var(--accent-cyan);
	}
	.pf-row-bottom {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
	}
	.pf-row-option {
		font-size: 12px;
		color: var(--text-muted);
		font-family: var(--font-body);
	}
	.pf-row-amount {
		font-family: var(--font-numeric);
		font-size: var(--font-heading-size);
		font-weight: 900;
		color: var(--text-primary);
	}
	.pf-row-amount small {
		font-size: 11px;
		color: var(--accent-pink);
		margin-left: 1px;
	}
</style>
