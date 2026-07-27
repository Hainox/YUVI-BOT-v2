<script lang="ts">
	// /card — miniapp screen over GET /api/v1/ai/card (hub-parity for
	// bot/handlers/ai_card.py). No target picked = self (same default as the
	// chat command when called with no reply/@arg).
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';
	import UserPicker from '$lib/components/UserPicker.svelte';

	type TopWord = { word: string; count: number };
	type Card = {
		display_name: string;
		portrait: string;
		stats: { total_messages: number; active_days: number; streak: number; top_words: TopWord[] };
		nlp: { classified_count: number; avg_sentiment: number | null; avg_toxicity: number | null };
	};

	let targetUserId = $state<number | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let card = $state<Card | null>(null);

	async function load() {
		loading = true;
		error = null;
		try {
			const path =
				targetUserId === null
					? '/api/v1/ai/card'
					: `/api/v1/ai/card?target_user_id=${targetUserId}`;
			card = await apiFetch<Card>(path);
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
		} finally {
			loading = false;
		}
	}

	onMount(load);
</script>

<div class="card-screen">
	<div class="menu-head">
		<h1 class="menu-title">Карточка участника</h1>
		<div class="menu-sub">AI-портрет · статистика · настроение</div>
	</div>

	<UserPicker bind:value={targetUserId} label="Кого показать (пусто — себя)" />
	<button type="button" class="chip chip-all card-reload" disabled={loading} onclick={load}>
		{loading ? 'загрузка…' : 'Показать'}
	</button>

	{#if error}
		<div class="cf-error">{error}</div>
	{:else if card}
		<div class="card-block">
			<div class="card-block-title">🎭 Портрет — {card.display_name}</div>
			<div class="card-block-text">{card.portrait}</div>
		</div>
		<div class="card-block">
			<div class="card-block-title">📊 Статистика</div>
			<div class="card-row"><span>Сообщений</span><b>{card.stats.total_messages}</b></div>
			<div class="card-row"><span>Активных дней</span><b>{card.stats.active_days}</b></div>
			<div class="card-row"><span>Текущая серия</span><b>{card.stats.streak} дн.</b></div>
			{#if card.stats.top_words.length > 0}
				<div class="card-words">
					Топ слов чата: {card.stats.top_words.map((w) => w.word).join(', ')}
				</div>
			{/if}
		</div>
		<div class="card-block">
			<div class="card-block-title">💬 Настроение/токсичность</div>
			{#if card.nlp.classified_count === 0}
				<div class="card-empty">Пока недостаточно данных.</div>
			{:else}
				{#if card.nlp.avg_sentiment !== null}
					<div class="card-row"><span>Среднее настроение</span><b>{card.nlp.avg_sentiment.toFixed(2)}</b></div>
				{/if}
				{#if card.nlp.avg_toxicity !== null}
					<div class="card-row"><span>Средняя токсичность</span><b>{card.nlp.avg_toxicity.toFixed(2)}</b></div>
				{/if}
				<div class="card-words">Проанализировано сообщений: {card.nlp.classified_count}</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	.card-screen {
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
	.card-reload {
		padding: var(--space-sm) var(--space-md);
	}
	.cf-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}
	.card-block {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-md);
	}
	.card-block-title {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		color: var(--text-primary);
		margin-bottom: var(--space-sm);
	}
	.card-block-text {
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		color: var(--text-secondary);
		line-height: 1.5;
		white-space: pre-wrap;
	}
	.card-row {
		display: flex;
		justify-content: space-between;
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		color: var(--text-secondary);
		padding: 2px 0;
	}
	.card-words {
		margin-top: var(--space-xs);
		font-size: 12px;
		color: var(--text-muted);
		font-family: var(--font-body);
	}
	.card-empty {
		color: var(--text-muted);
		font-family: var(--font-body);
		font-size: var(--font-body-size);
	}
</style>
