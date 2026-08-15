<script lang="ts">
	// Итоги дня — miniapp screen over GET /api/v1/awards/today (hub-parity
	// for bot/handlers/awards.py's /awards). run_awards is idempotent per
	// MSK-day + per-nomination ref_id (AWARDS-02) — repeat calls the same day
	// just redisplay the already-paid result, no double payout.
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';

	type Nomination = {
		key: string;
		label: string;
		prize: number;
		winner_name: string | null;
		paid: number;
	};
	type AwardsResult = {
		day_msk: string;
		nominations: Nomination[];
		steam_game: string | null;
	};

	let loading = $state(true);
	let error = $state<string | null>(null);
	let result = $state<AwardsResult | null>(null);

	onMount(async () => {
		try {
			result = await apiFetch<AwardsResult>('/api/v1/awards/today');
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
		} finally {
			loading = false;
		}
	});
</script>

<div class="awards-screen">
	<div class="menu-head">
		<h1 class="menu-title">Итоги дня</h1>
		<div class="menu-sub">7 номинаций · выплаты из банка чата</div>
	</div>

	{#if loading}
		<div class="screen-loading"><span>загрузка…</span></div>
	{:else if error}
		<div class="cf-error">{error}</div>
	{:else if result}
		<div class="awards-list">
			{#each result.nominations as nom (nom.key)}
				<div class="awards-entry">
					<span class="awards-entry-label">{nom.label}</span>
					{#if nom.winner_name === null}
						<span class="awards-entry-winner awards-entry-none">никто</span>
					{:else}
						<span class="awards-entry-winner">{nom.winner_name}</span>
						<span class="awards-entry-prize">+{nom.prize}¥</span>
					{/if}
				</div>
			{/each}
			<div class="awards-steam">
				{#if result.steam_game}
					🎮 Игра дня из Steam Wishlist: <b>{result.steam_game}</b>
				{:else}
					🎮 Steam недоступен 😢
				{/if}
			</div>
		</div>
	{/if}
</div>

<style>
	.awards-screen {
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
	.awards-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.awards-entry {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-md);
		display: flex;
		align-items: center;
		gap: var(--space-sm);
		flex-wrap: wrap;
	}
	.awards-entry-label {
		font-family: var(--font-chrome);
		font-size: var(--font-body-size);
		font-weight: 700;
		color: var(--text-primary);
		flex: 1;
		min-width: 0;
	}
	.awards-entry-winner {
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		color: var(--text-secondary);
	}
	.awards-entry-none {
		color: var(--text-muted);
	}
	.awards-entry-prize {
		font-family: var(--font-numeric);
		font-weight: 900;
		color: var(--positive-text);
	}
	.awards-steam {
		text-align: center;
		font-size: var(--font-body-size);
		font-family: var(--font-body);
		color: var(--text-secondary);
		padding: var(--space-sm) 0;
	}
</style>
