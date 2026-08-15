<script lang="ts">
	// Настроение/токсичность чата — miniapp screen over GET /api/v1/mood
	// (hub-parity for bot/handlers/mood.py's /mood and /toxic, merged into one
	// screen since they're the same "чат-статистика" concept and the backend
	// already computes both from the same pre-classified columns, never LLM
	// synchronously — RESEARCH.md Anti-Patterns, mirrored in mood_service.py).
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';

	const DAY_PRESETS: { label: string; value: number | null }[] = [
		{ label: '7 дн.', value: 7 },
		{ label: '30 дн.', value: 30 },
		{ label: 'всё время', value: null }
	];

	type Mood = { classified_count: number; label_shares: { positive: number; neutral: number; negative: number }; avg_sentiment: number };
	type Toxicity = { classified_count: number; avg_toxicity: number; toxic_share: number };

	let days = $state<number | null>(7);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let mood = $state<Mood | null>(null);
	let toxicity = $state<Toxicity | null>(null);

	async function load() {
		loading = true;
		error = null;
		try {
			const path = days === null ? '/api/v1/mood' : `/api/v1/mood?days=${days}`;
			const res = await apiFetch<{ mood: Mood; toxicity: Toxicity }>(path);
			mood = res.mood;
			toxicity = res.toxicity;
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
		} finally {
			loading = false;
		}
	}

	onMount(load);
</script>

<div class="mood-screen">
	<div class="menu-head">
		<h1 class="menu-title">Настроение чата</h1>
		<div class="menu-sub">по уже классифицированным сообщениям</div>
	</div>

	<div class="mood-chips">
		{#each DAY_PRESETS as p (p.label)}
			<button
				type="button"
				class={`chip ${days === p.value ? 'chip-on' : ''}`}
				onclick={() => {
					days = p.value;
					load();
				}}
			>
				{p.label}
			</button>
		{/each}
	</div>

	{#if loading}
		<div class="screen-loading"><span>загрузка…</span></div>
	{:else if error}
		<div class="cf-error">{error}</div>
	{:else}
		<div class="mood-card">
			<div class="mood-card-title">Настроение</div>
			{#if mood && mood.classified_count > 0}
				<div class="mood-row"><span>Позитивных</span><b>{mood.label_shares.positive.toLocaleString('ru-RU', { style: 'percent' })}</b></div>
				<div class="mood-row"><span>Нейтральных</span><b>{mood.label_shares.neutral.toLocaleString('ru-RU', { style: 'percent' })}</b></div>
				<div class="mood-row"><span>Негативных</span><b>{mood.label_shares.negative.toLocaleString('ru-RU', { style: 'percent' })}</b></div>
				<div class="mood-sub">Проанализировано сообщений: {mood.classified_count}</div>
			{:else}
				<div class="mood-empty">Пока недостаточно данных.</div>
			{/if}
		</div>

		<div class="mood-card">
			<div class="mood-card-title">Токсичность</div>
			{#if toxicity && toxicity.classified_count > 0}
				<div class="mood-row"><span>Средний показатель</span><b>{toxicity.avg_toxicity.toFixed(2)}</b></div>
				<div class="mood-row"><span>Доля токсичных</span><b>{toxicity.toxic_share.toLocaleString('ru-RU', { style: 'percent' })}</b></div>
				<div class="mood-sub">Проанализировано сообщений: {toxicity.classified_count}</div>
			{:else}
				<div class="mood-empty">Пока недостаточно данных.</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	.mood-screen {
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
	.mood-chips {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: var(--space-xs);
	}
	.cf-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}
	.mood-card {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-md);
	}
	.mood-card-title {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		color: var(--text-primary);
		margin-bottom: var(--space-sm);
	}
	.mood-row {
		display: flex;
		justify-content: space-between;
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		color: var(--text-secondary);
		padding: 2px 0;
	}
	.mood-sub {
		margin-top: var(--space-xs);
		font-size: 12px;
		color: var(--text-muted);
		font-family: var(--font-body);
	}
	.mood-empty {
		color: var(--text-muted);
		font-family: var(--font-body);
		font-size: var(--font-body-size);
	}
</style>
