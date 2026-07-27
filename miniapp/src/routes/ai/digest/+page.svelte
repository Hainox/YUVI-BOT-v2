<script lang="ts">
	// /digest — miniapp screen over GET /api/v1/ai/digest (hub-parity for
	// bot/handlers/ai_digest.py). Manual digest, no digest_min_messages
	// suppression (same as the chat command — explicit ask, no spam concern).
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';

	const DAY_PRESETS = [1, 3, 7];

	let days = $state(1);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let text = $state<string | null>(null);

	async function load() {
		loading = true;
		error = null;
		try {
			const res = await apiFetch<{ text: string }>(`/api/v1/ai/digest?days=${days}`);
			text = res.text;
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
		} finally {
			loading = false;
		}
	}

	onMount(load);
</script>

<div class="digest-screen">
	<div class="menu-head">
		<h1 class="menu-title">Дайджест</h1>
		<div class="menu-sub">AI-пересказ жизни чата за период</div>
	</div>

	<div class="digest-chips">
		{#each DAY_PRESETS as d (d)}
			<button
				type="button"
				class={`chip ${days === d ? 'chip-on' : ''}`}
				disabled={loading}
				onclick={() => {
					days = d;
					load();
				}}
			>
				{d} дн.
			</button>
		{/each}
	</div>

	{#if loading}
		<div class="screen-loading"><span>загрузка…</span></div>
	{:else if error}
		<div class="cf-error">{error}</div>
	{:else if text}
		<div class="digest-text">{text}</div>
	{/if}
</div>

<style>
	.digest-screen {
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
	.digest-chips {
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
	.digest-text {
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
