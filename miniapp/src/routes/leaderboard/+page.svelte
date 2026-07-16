<script lang="ts">
	// Leaderboard — destination of the "Лидерборд" hub tile (CASINO-02).
	// 04-UI-SPEC.md §Component Inventory: "Stats/leaderboard list (stats-row,
	// rank badges, "me" tag) — webapp/stats.jsx — reuse directly for
	// /leaderboard screen." GET /api/v1/leaderboard already returns balance-
	// ranked rows (economy_service.get_leaderboard, ORDER BY balance DESC) —
	// this screen is a read-only ranked render, no client-side re-sort.
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';
	import { user } from '$lib/tg';

	type LeaderboardRow = {
		user_id: number;
		first_name: string | null;
		username: string | null;
		balance: number;
	};

	let loading = $state(true);
	let error = $state<string | null>(null);
	let rows = $state<LeaderboardRow[]>([]);

	const myId = user?.id ?? null;

	function displayName(row: LeaderboardRow): string {
		return row.username || row.first_name || `id${row.user_id}`;
	}

	onMount(async () => {
		try {
			rows = await apiFetch<LeaderboardRow[]>('/api/v1/leaderboard');
		} catch (err) {
			error = err instanceof ApiError ? `${err.status}: ${err.message}` : String(err ?? 'unknown_error');
		} finally {
			loading = false;
		}
	});
</script>

<div class="lb-screen">
	<div class="menu-head">
		<h1 class="menu-title">Лидерборд</h1>
		<div class="menu-sub">топ богачей чата</div>
	</div>

	{#if loading}
		<div class="screen-loading"><span>загрузка лидерборда…</span></div>
	{:else if error}
		<div class="lb-error">{error}</div>
	{:else if rows.length === 0}
		<div class="lb-empty">
			<div class="lb-empty-jp">いない</div>
			<div class="lb-empty-ru">пока никто не играл</div>
			<div class="lb-empty-hint">Крути слот или ферми — попадёшь в топ.</div>
		</div>
	{:else}
		<div class="lb-list">
			{#each rows as row, i (row.user_id)}
				{@const isMe = myId != null && row.user_id === myId}
				<div class={`lb-row ${isMe ? 'is-me' : ''}`}>
					<span class={`lb-rank rank-${Math.min(i + 1, 4)}`}>{i + 1}</span>
					<span class="lb-name">
						@{displayName(row)}
						{#if isMe}<span class="lb-me-tag">ты</span>{/if}
					</span>
					<span class="lb-balance">{row.balance.toLocaleString('ru-RU')}<small>¥</small></span>
				</div>
			{/each}
		</div>
	{/if}
</div>

<style>
	.lb-screen {
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

	.lb-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.lb-empty {
		text-align: center;
		padding: var(--space-2xl) var(--space-lg);
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.lb-empty-jp {
		font-family: var(--font-jp);
		font-size: var(--font-heading-size);
		color: var(--text-locked);
	}
	.lb-empty-ru {
		font-family: var(--font-chrome);
		font-size: var(--font-display-size);
		font-weight: 700;
		color: var(--text-secondary);
		text-transform: lowercase;
	}
	.lb-empty-hint {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		font-family: var(--font-body);
		line-height: 1.5;
		max-width: 320px;
		margin: var(--space-xs) auto 0;
	}

	.lb-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.lb-row {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 12px;
		padding: var(--space-sm) var(--space-md);
		display: flex;
		align-items: center;
		gap: var(--space-sm);
		min-height: 44px;
	}
	.lb-row.is-me {
		border-color: var(--accent-pink);
	}
	.lb-rank {
		font-family: var(--font-numeric);
		font-size: var(--font-heading-size);
		font-weight: 900;
		color: var(--text-muted);
		min-width: 22px;
		text-align: center;
	}
	.lb-rank.rank-1 {
		color: var(--accent-yellow);
	}
	.lb-name {
		flex: 1;
		font-size: var(--font-body-size);
		color: var(--text-secondary);
		font-family: var(--font-body);
		display: flex;
		align-items: center;
		gap: 6px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.lb-me-tag {
		flex-shrink: 0;
		background: var(--accent-pink);
		color: #1a0f12;
		font-size: 10px;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 2px 6px;
		border-radius: 20px;
	}
	.lb-balance {
		font-family: var(--font-numeric);
		font-size: var(--font-heading-size);
		font-weight: 900;
		color: var(--text-primary);
		flex-shrink: 0;
	}
	.lb-balance small {
		font-size: 11px;
		color: var(--accent-pink);
		margin-left: 1px;
	}
</style>
