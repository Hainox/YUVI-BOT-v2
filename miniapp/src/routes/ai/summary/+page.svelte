<script lang="ts">
	// /summary + /summary_custom — miniapp screen over GET /api/v1/ai/summary
	// and POST /api/v1/ai/summary_custom (hub-parity for bot/handlers/
	// ai_summary.py). Same N default (100) as the chat command; an optional
	// focus field switches to the custom-prompt endpoint (empty focus = plain
	// /summary, matching the two separate chat commands folded into one screen).
	import { apiFetch, ApiError } from '$lib/api';

	const N_PRESETS = [50, 100, 200];

	let n = $state(100);
	let focus = $state('');
	let loading = $state(false);
	let error = $state<string | null>(null);
	let text = $state<string | null>(null);

	async function run() {
		loading = true;
		error = null;
		text = null;
		try {
			const trimmedFocus = focus.trim();
			const res = trimmedFocus
				? await apiFetch<{ text: string }>('/api/v1/ai/summary_custom', {
						method: 'POST',
						body: JSON.stringify({ n, focus: trimmedFocus })
					})
				: await apiFetch<{ text: string }>(`/api/v1/ai/summary?n=${n}`);
			text = res.text;
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
		} finally {
			loading = false;
		}
	}
</script>

<div class="summary-screen">
	<div class="menu-head">
		<h1 class="menu-title">Пересказ</h1>
		<div class="menu-sub">AI-пересказ последних сообщений чата</div>
	</div>

	<div class="summary-chips">
		{#each N_PRESETS as v (v)}
			<button type="button" class={`chip ${n === v ? 'chip-on' : ''}`} disabled={loading} onclick={() => (n = v)}>
				{v}
			</button>
		{/each}
	</div>

	<input
		class="summary-focus"
		type="text"
		placeholder="фокус темы, необязательно (например: про котов)"
		bind:value={focus}
		disabled={loading}
	/>

	<button type="button" class="chip chip-all summary-cta" disabled={loading} onclick={run}>
		{loading ? 'пересказываем…' : 'Пересказать'}
	</button>

	{#if error}
		<div class="cf-error">{error}</div>
	{/if}

	{#if text}
		<div class="summary-text">{text}</div>
	{/if}
</div>

<style>
	.summary-screen {
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
	.summary-chips {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: var(--space-xs);
	}
	.summary-focus {
		width: 100%;
		box-sizing: border-box;
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 10px;
		padding: var(--space-sm) var(--space-md);
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		color: var(--text-primary);
	}
	.summary-cta {
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
	.summary-text {
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
