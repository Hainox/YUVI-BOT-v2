<script lang="ts">
	// Жертва дня — miniapp screen over GET /api/v1/victim/today (hub-parity
	// for bot/handlers/victim.py's /victim). Read-only: run_victim is
	// idempotent per MSK-day (same winner/prize on repeat calls), so hitting
	// it here is safe and needs no separate "already picked" branch client-side.
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';

	type VictimResult = {
		winner_name: string | null;
		prize: number;
		expires_at: string | null;
		is_new: boolean;
	};

	let loading = $state(true);
	let error = $state<string | null>(null);
	let result = $state<VictimResult | null>(null);

	function formatTime(iso: string): string {
		const d = new Date(iso);
		return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
	}

	onMount(async () => {
		try {
			result = await apiFetch<VictimResult>('/api/v1/victim/today');
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
		} finally {
			loading = false;
		}
	});
</script>

<div class="victim-screen">
	<div class="menu-head">
		<h1 class="menu-title">Жертва дня</h1>
		<div class="menu-sub">228 ювиков из банка · титул на 24ч · x2 комиссия перевода</div>
	</div>

	{#if loading}
		<div class="screen-loading"><span>загрузка…</span></div>
	{:else if error}
		<div class="cf-error">{error}</div>
	{:else if result?.winner_name === null}
		<div class="victim-empty">В чате пока нет активных участников для выбора жертвы дня.</div>
	{:else if result}
		<div class="victim-card">
			<div class="victim-target">🎯 {result.winner_name}</div>
			<div class="victim-prize">+{result.prize}¥ из банка чата</div>
			{#if result.expires_at}
				<div class="victim-expires">Дебафф действует до {formatTime(result.expires_at)}</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	.victim-screen {
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
	.victim-empty {
		color: var(--text-muted);
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		text-align: center;
		padding: var(--space-lg) 0;
	}
	.victim-card {
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-lg);
		text-align: center;
	}
	.victim-target {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		color: var(--text-primary);
	}
	.victim-prize {
		margin-top: var(--space-sm);
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		color: var(--positive-text);
	}
	.victim-expires {
		margin-top: var(--space-sm);
		font-size: 12px;
		color: var(--text-muted);
		font-family: var(--font-body);
	}
</style>
