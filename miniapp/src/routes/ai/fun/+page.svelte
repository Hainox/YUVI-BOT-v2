<script lang="ts">
	// /topics /phrase /joke — miniapp screen over GET /api/v1/ai/{topics,
	// phrase,joke} (hub-parity for bot/handlers/ai_topics.py). All three are
	// parameterless one-shot generations, folded into a single screen with a
	// tab per command instead of three separate hub tiles.
	import { AI_REQUEST_TIMEOUT_MS, apiFetch, ApiError } from '$lib/api';

	type Tab = 'topics' | 'phrase' | 'joke';
	const TABS: { key: Tab; label: string; title: string }[] = [
		{ key: 'topics', label: 'Темы', title: 'Темы обсуждений' },
		{ key: 'phrase', label: 'Фраза дня', title: 'Фраза дня' },
		{ key: 'joke', label: 'Анекдот', title: 'Анекдот дня' }
	];

	let tab = $state<Tab>('topics');
	let loading = $state(false);
	let error = $state<string | null>(null);
	let results = $state<Record<Tab, string | null>>({ topics: null, phrase: null, joke: null });

	async function generate() {
		loading = true;
		error = null;
		try {
			const res = await apiFetch<{ text: string }>(
				`/api/v1/ai/${tab}`,
				undefined,
				AI_REQUEST_TIMEOUT_MS
			);
			results = { ...results, [tab]: res.text };
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
		} finally {
			loading = false;
		}
	}
</script>

<div class="fun-screen">
	<div class="menu-head">
		<h1 class="menu-title">AI-развлечения</h1>
		<div class="menu-sub">темы обсуждений · фраза дня · анекдот</div>
	</div>

	<div class="fun-tabs">
		{#each TABS as t (t.key)}
			<button type="button" class={`chip ${tab === t.key ? 'chip-on' : ''}`} onclick={() => (tab = t.key)}>
				{t.label}
			</button>
		{/each}
	</div>

	<button type="button" class="chip chip-all fun-cta" disabled={loading} onclick={generate}>
		{loading ? 'генерируем…' : 'Сгенерировать'}
	</button>

	{#if error}
		<div class="cf-error">{error}</div>
	{:else if results[tab]}
		<div class="fun-result">
			<div class="fun-result-title">{TABS.find((t) => t.key === tab)?.title}</div>
			<div class="fun-result-text">{results[tab]}</div>
		</div>
	{/if}
</div>

<style>
	.fun-screen {
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
	.fun-tabs {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: var(--space-xs);
	}
	.fun-cta {
		width: 100%;
		padding: var(--space-md);
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
	}
	.cf-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}
	.fun-result {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-md);
	}
	.fun-result-title {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		color: var(--text-primary);
		margin-bottom: var(--space-sm);
	}
	.fun-result-text {
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		color: var(--text-secondary);
		line-height: 1.5;
		white-space: pre-wrap;
	}
</style>
