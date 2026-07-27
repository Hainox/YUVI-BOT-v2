<script lang="ts">
	// /ask — miniapp screen over POST /api/v1/ai/ask (hub-parity for
	// bot/handlers/ai_ask.py). RAG search over chat history, plain text answer.
	import { apiFetch, ApiError } from '$lib/api';

	let question = $state('');
	let loading = $state(false);
	let error = $state<string | null>(null);
	let answer = $state<string | null>(null);

	async function ask() {
		const q = question.trim();
		if (!q || loading) return;
		loading = true;
		error = null;
		answer = null;
		try {
			const res = await apiFetch<{ answer: string }>('/api/v1/ai/ask', {
				method: 'POST',
				body: JSON.stringify({ question: q })
			});
			answer = res.answer;
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
		} finally {
			loading = false;
		}
	}
</script>

<div class="ask-screen">
	<div class="menu-head">
		<h1 class="menu-title">Спросить чат</h1>
		<div class="menu-sub">поиск по истории переписки</div>
	</div>

	<textarea
		class="ask-input"
		placeholder="о чём спорили вчера?"
		rows="3"
		bind:value={question}
		disabled={loading}
	></textarea>

	<button type="button" class="chip chip-all ask-cta" disabled={loading || !question.trim()} onclick={ask}>
		{loading ? 'ищем…' : 'Спросить'}
	</button>

	{#if error}
		<div class="cf-error">{error}</div>
	{/if}

	{#if answer}
		<div class="ask-answer">{answer}</div>
	{/if}
</div>

<style>
	.ask-screen {
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
	.ask-input {
		width: 100%;
		box-sizing: border-box;
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 10px;
		padding: var(--space-sm) var(--space-md);
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		color: var(--text-primary);
		resize: vertical;
	}
	.ask-cta {
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
	.ask-answer {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-md);
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		color: var(--text-secondary);
		white-space: pre-wrap;
		line-height: 1.5;
	}
</style>
