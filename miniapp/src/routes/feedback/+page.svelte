<script lang="ts">
	// Feedback submission — destination of the "Фидбек" hub tile (CASINO-03, D-04).
	// Structurally mirrors transfer/+page.svelte (04.3-UI-SPEC.md §Component
	// Inventory "4. Feedback submission screen"): single form — category chips
	// (single-select) + textarea + primary CTA + inline success/error, no
	// separate confirm step.
	//
	// POST /api/v1/feedback's FeedbackBody carries no author field at all
	// (api/routes/feedback.py) — author is exclusively auth.user_id/auth.chat_id
	// server-side (T-04.3-01, same IDOR discipline as transfer/duel/markets).
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	type Category = 'bug' | 'idea' | 'complaint' | 'other';

	const CATEGORY_CHIPS: { value: Category; label: string }[] = [
		{ value: 'bug', label: 'баг' },
		{ value: 'idea', label: 'идея' },
		{ value: 'complaint', label: 'жалоба' },
		{ value: 'other', label: 'другое' }
	];

	const MAX_TEXT_LENGTH = 2000;

	let category = $state<Category | null>(null);
	let text = $state('');
	let sending = $state(false);
	let error = $state<string | null>(null);
	let submitted = $state(false);

	let canSubmit = $derived(category !== null && text.trim().length > 0 && !sending);

	async function send() {
		if (!canSubmit || category === null) return;
		sending = true;
		error = null;
		try {
			await apiFetch<{ status: string }>('/api/v1/feedback', {
				method: 'POST',
				body: JSON.stringify({ category, text: text.trim() })
			});
			submitted = true;
			text = '';
			category = null;
			haptic('tap');
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
			haptic('error');
		} finally {
			sending = false;
		}
	}
</script>

<div class="tr-screen">
	<div class="menu-head">
		<h1 class="menu-title">Фидбек</h1>
		<div class="menu-sub">баг, идея или жалоба — админы увидят</div>
	</div>

	<div class="fb-field">
		<span class="tr-field-label">Категория</span>
		<div class="fb-chips">
			{#each CATEGORY_CHIPS as chipOption (chipOption.value)}
				<button
					type="button"
					class={`chip ${category === chipOption.value ? 'chip-on' : ''}`}
					disabled={sending}
					onclick={() => (category = chipOption.value)}
				>
					{chipOption.label}
				</button>
			{/each}
		</div>
	</div>

	<label class="tr-field">
		<span class="tr-field-label">Текст</span>
		<textarea
			class="tr-input fb-textarea"
			placeholder="Опиши баг, идею или жалобу…"
			maxlength={MAX_TEXT_LENGTH}
			bind:value={text}
			disabled={sending}
		></textarea>
	</label>

	{#if error}
		<div class="tr-error">{error}</div>
	{/if}

	{#if submitted}
		<div class="tr-success">
			<div class="tr-success-title">Спасибо!</div>
			<div class="tr-success-sub">Заявка отправлена — админы её увидят.</div>
		</div>
	{/if}

	<button type="button" class="tr-cta" disabled={!canSubmit} onclick={send}>
		<span class="tr-cta-label">{sending ? 'отправляем…' : 'ОТПРАВИТЬ'}</span>
	</button>
</div>

<style>
	.tr-screen {
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

	.fb-field {
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.tr-field {
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.tr-field-label {
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
	}

	.fb-chips {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: var(--space-xs);
	}

	.tr-input {
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 10px;
		padding: var(--space-sm) var(--space-md);
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		color: var(--text-primary);
	}
	.tr-input:focus {
		outline: none;
		border-color: var(--accent-pink);
	}
	.fb-textarea {
		min-height: 140px;
		resize: vertical;
		line-height: 1.5;
	}

	.tr-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.tr-success {
		background: var(--bg-secondary-2);
		border: 2px solid var(--accent-cyan);
		border-radius: 14px;
		padding: var(--space-md);
		text-align: center;
	}
	.tr-success-title {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		color: var(--accent-cyan);
	}
	.tr-success-sub {
		font-size: 12px;
		color: var(--text-muted);
		margin-top: var(--space-xs);
		font-family: var(--font-body);
		line-height: 1.4;
	}

	.tr-cta {
		background: var(--accent-pink);
		border: none;
		border-radius: 14px;
		padding: var(--space-md);
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--space-xs);
		cursor: pointer;
		box-shadow: 4px 4px 0 #111;
		transition: transform 0.08s;
	}
	.tr-cta:active:not(:disabled) {
		transform: translate(2px, 2px);
		box-shadow: 2px 2px 0 #111;
	}
	.tr-cta:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.tr-cta-label {
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		color: #1a0f12;
		letter-spacing: 0.04em;
	}
</style>
