<script lang="ts">
	// Что новое (WHATSNEW-01, запрошено 2026-07-24) — лента обновлений/планов
	// разработки. Read-only: публикация только через /post_update
	// (bot/handlers/owner.py, settings.owner_id) — этот экран сам ничего не
	// пишет, только GET /api/v1/changelog (тот же паттерн, что history/rules).
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';

	type Entry = { id: number; title: string; body: string | null; created_at: string };

	let loading = $state(true);
	let error = $state<string | null>(null);
	let entries = $state<Entry[]>([]);

	function formatDate(iso: string): string {
		const d = new Date(iso);
		return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });
	}

	onMount(async () => {
		try {
			const res = await apiFetch<{ entries: Entry[] }>('/api/v1/changelog');
			entries = res.entries;
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
		} finally {
			loading = false;
		}
	});
</script>

<div class="wn-screen">
	<div class="menu-head">
		<h1 class="menu-title">Что новое</h1>
		<div class="menu-sub">обновления и планы разработки</div>
	</div>

	{#if loading}
		<div class="screen-loading"><span>загрузка…</span></div>
	{:else if error}
		<div class="cf-error">{error}</div>
	{:else if entries.length === 0}
		<div class="wn-empty">Пока пусто — загляни позже.</div>
	{:else}
		<div class="wn-list">
			{#each entries as entry (entry.id)}
				<div class="wn-entry">
					<div class="wn-entry-head">
						<span class="wn-entry-title">{entry.title}</span>
						<span class="wn-entry-date">{formatDate(entry.created_at)}</span>
					</div>
					{#if entry.body}
						<div class="wn-entry-body">{entry.body}</div>
					{/if}
				</div>
			{/each}
		</div>
	{/if}
</div>

<style>
	.wn-screen {
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

	.cf-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.wn-empty {
		color: var(--text-muted);
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		text-align: center;
		padding: var(--space-lg) 0;
	}

	.wn-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.wn-entry {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-md);
	}
	.wn-entry-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--space-sm);
	}
	.wn-entry-title {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		color: var(--text-primary);
	}
	.wn-entry-date {
		font-size: 12px;
		color: var(--text-muted);
		font-family: var(--font-body);
		white-space: nowrap;
	}
	.wn-entry-body {
		margin-top: var(--space-xs);
		font-size: var(--font-body-size);
		color: var(--text-secondary);
		font-family: var(--font-body);
		line-height: 1.5;
		white-space: pre-wrap;
	}
</style>
