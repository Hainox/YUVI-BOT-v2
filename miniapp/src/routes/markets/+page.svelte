<script lang="ts">
	// Markets list — destination of the "Рынки" hub tile (D-04, BET-01/02/03
	// UI-only addition). 04.2-UI-SPEC.md §Component Inventory "Markets List +
	// Market Detail screens": row shape reuses the hist-row grid rhythm (card
	// surface, padding) extended with title/closing-countdown/total-pool/
	// option-count. Shows ONLY open markets — resolved/cancelled markets
	// belong to the future History feed, not this list (GET /api/v1/markets
	// already scopes to status="open" server-side, see markets_service.
	// get_open_markets).
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { apiFetch, ApiError } from '$lib/api';

	type MarketListItem = {
		id: number;
		question: string;
		closes_at: string;
		total_pool: number;
		options_count: number;
	};

	let loading = $state(true);
	let error = $state<string | null>(null);
	let markets = $state<MarketListItem[]>([]);

	function msRemaining(closesAt: string): number {
		return new Date(closesAt).getTime() - Date.now();
	}

	function isClosingSoon(closesAt: string): boolean {
		const ms = msRemaining(closesAt);
		return ms > 0 && ms < 24 * 3_600_000;
	}

	function countdownLabel(closesAt: string): string {
		const ms = msRemaining(closesAt);
		if (ms <= 0) return 'закрывается…';
		const hours = ms / 3_600_000;
		if (hours < 1) return `${Math.max(1, Math.round(ms / 60_000))} мин`;
		if (hours < 24) return `${Math.round(hours)} ч`;
		return `${Math.floor(hours / 24)} дн`;
	}

	onMount(async () => {
		try {
			markets = await apiFetch<MarketListItem[]>('/api/v1/markets');
		} catch (err) {
			error = err instanceof ApiError ? `${err.status}: ${err.message}` : String(err ?? 'unknown_error');
		} finally {
			loading = false;
		}
	});
</script>

<div class="mk-screen">
	<div class="menu-head">
		<h1 class="menu-title">Рынки</h1>
		<div class="menu-sub">ставь на исход, следи за котировкой</div>
	</div>

	{#if loading}
		<div class="screen-loading"><span>загрузка рынков…</span></div>
	{:else if error}
		<div class="mk-error">{error}</div>
	{:else if markets.length === 0}
		<div class="mk-empty">
			<div class="mk-empty-jp">しずか</div>
			<div class="mk-empty-ru">тихо</div>
			<div class="mk-empty-hint">рынков пока нет. Создай через /market_create в чате — появится здесь.</div>
		</div>
	{:else}
		<div class="mk-list">
			{#each markets as m (m.id)}
				<button type="button" class="mk-row" onclick={() => goto(`/markets/${m.id}`)}>
					<div class="mk-row-top">
						<span class="mk-row-title">{m.question}</span>
						{#if isClosingSoon(m.closes_at)}
							<span class="mk-chip-soon">закрывается через {countdownLabel(m.closes_at)}</span>
						{/if}
					</div>
					<div class="mk-row-bottom">
						<span class="mk-row-pool">{m.total_pool}<small>¥</small></span>
						<span class="mk-row-meta">
							{#if !isClosingSoon(m.closes_at)}
								{countdownLabel(m.closes_at)} ·
							{/if}
							{m.options_count} вариантов
						</span>
					</div>
				</button>
			{/each}
		</div>
	{/if}
</div>

<style>
	.mk-screen {
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

	.mk-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.mk-empty {
		text-align: center;
		padding: var(--space-2xl) var(--space-lg);
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.mk-empty-jp {
		font-family: var(--font-jp);
		font-size: var(--font-heading-size);
		color: var(--text-locked);
	}
	.mk-empty-ru {
		font-family: var(--font-chrome);
		font-size: var(--font-display-size);
		font-weight: 700;
		color: var(--text-secondary);
		text-transform: lowercase;
	}
	.mk-empty-hint {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		font-family: var(--font-body);
		line-height: 1.5;
		max-width: 320px;
		margin: var(--space-xs) auto 0;
	}

	.mk-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.mk-row {
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
	.mk-row:active {
		transform: scale(0.985);
	}
	.mk-row-top {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--space-sm);
	}
	.mk-row-title {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		color: var(--text-primary);
	}
	.mk-chip-soon {
		flex-shrink: 0;
		background: var(--accent-yellow);
		color: #1a1508;
		font-size: 10px;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		padding: 3px 8px;
		border-radius: 20px;
		white-space: nowrap;
	}
	.mk-row-bottom {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
	}
	.mk-row-pool {
		font-family: var(--font-numeric);
		font-size: var(--font-heading-size);
		font-weight: 900;
		color: var(--accent-cyan);
	}
	.mk-row-pool small {
		font-size: 11px;
		margin-left: 1px;
	}
	.mk-row-meta {
		font-size: 12px;
		color: var(--text-muted);
		font-family: var(--font-body);
	}
</style>
