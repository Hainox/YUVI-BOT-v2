<script lang="ts">
	// Feedback submission — destination of the "Фидбек" hub tile (CASINO-03, D-04).
	//
	// FEEDBACK-01 / D-15 (06-UI-SPEC.md §Component Inventory "3. AI-assistant
	// chat layer"): default view is now an AI chat — the participant writes
	// free text, the assistant asks clarifying questions and decides the
	// category/text itself. The original plain form (Phase 04.3-01: category
	// chips + textarea + CTA) is NOT rewritten — it stays exactly as it was,
	// reached as the fallback view either by explicit "Заполнить вручную" or
	// automatic degrade when the AI/parsing fails.
	//
	// Every chat turn POSTs '/api/v1/feedback/assist' with the running
	// `history` (role/content pairs only — no author fields, same IDOR
	// discipline as the plain form's FeedbackBody). If the response carries a
	// non-null `register`, the backend has ALREADY called feedback_service.
	// submit server-side — the frontend renders the submitted-card, it never
	// submits a second time.
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	type Category = 'bug' | 'idea' | 'complaint' | 'other';
	type ChatRole = 'user' | 'assistant';

	interface ChatMessage {
		role: ChatRole;
		content: string;
	}

	interface AssistResponse {
		reply: string;
		degraded: boolean;
		register: { category: Category; text: string } | null;
	}

	const CATEGORY_CHIPS: { value: Category; label: string }[] = [
		{ value: 'bug', label: 'баг' },
		{ value: 'idea', label: 'идея' },
		{ value: 'complaint', label: 'жалоба' },
		{ value: 'other', label: 'другое' }
	];

	const CATEGORY_LABELS: Record<Category, string> = {
		bug: 'баг',
		idea: 'идея',
		complaint: 'жалоба',
		other: 'другое'
	};

	const MAX_TEXT_LENGTH = 2000;

	const GREETING =
		'Привет! Расскажи, что случилось — баг, идея или жалоба? Я задам пару уточняющих вопросов и сама оформлю заявку.';

	// ─── screen mode ──────────────────────────────────────────────────────
	let mode = $state<'chat' | 'form'>('chat');
	// true only when the user tapped "Заполнить вручную" explicitly — controls
	// whether the "Попробовать с AI-помощником" back-toggle is shown (never
	// shown after an automatic degrade, per Copywriting Contract row 116).
	let manualSwitch = $state(false);
	let degraded = $state(false);

	// ─── chat state ───────────────────────────────────────────────────────
	let messages = $state<ChatMessage[]>([{ role: 'assistant', content: GREETING }]);
	let chatInput = $state('');
	let chatSending = $state(false);
	let submittedCategory = $state<Category | null>(null);
	let threadEl = $state<HTMLDivElement | null>(null);

	$effect(() => {
		// Re-run whenever the thread grows (new bubble / typing indicator) —
		// keep the latest message in view, same intent as any chat UI.
		void messages.length;
		void chatSending;
		threadEl?.scrollTo({ top: threadEl.scrollHeight, behavior: 'smooth' });
	});

	function switchToManual() {
		manualSwitch = true;
		mode = 'form';
	}

	function switchBackToChat() {
		mode = 'chat';
	}

	function degradeToForm() {
		degraded = true;
		manualSwitch = false; // auto-degrade — back-to-chat toggle stays hidden
		text = messages
			.filter((m) => m.role === 'user')
			.map((m) => m.content)
			.join('\n');
		mode = 'form';
		haptic('error');
	}

	async function sendChatMessage() {
		const content = chatInput.trim();
		if (!content || chatSending || submittedCategory !== null) return;

		messages = [...messages, { role: 'user', content }];
		chatInput = '';
		chatSending = true;
		try {
			const resp = await apiFetch<AssistResponse>('/api/v1/feedback/assist', {
				method: 'POST',
				body: JSON.stringify({
					history: messages.map(({ role, content: msgContent }) => ({
						role,
						content: msgContent
					}))
				})
			});
			if (resp.degraded) {
				degradeToForm();
				return;
			}
			messages = [...messages, { role: 'assistant', content: resp.reply }];
			if (resp.register) {
				submittedCategory = resp.register.category;
				haptic('tap');
			}
		} catch (err) {
			void (err instanceof ApiError ? err.message : String(err ?? 'unknown_error'));
			degradeToForm();
		} finally {
			chatSending = false;
		}
	}

	function handleChatKeydown(event: KeyboardEvent) {
		if (event.key === 'Enter') {
			event.preventDefault();
			sendChatMessage();
		}
	}

	// ─── plain form (fallback, Фаза 04.3-01 — unchanged logic) ─────────────
	let category = $state<Category | null>(null);
	let text = $state('');
	let formSending = $state(false);
	let formError = $state<string | null>(null);
	let formSubmitted = $state(false);

	let canSubmit = $derived(category !== null && text.trim().length > 0 && !formSending);

	async function sendForm() {
		if (!canSubmit || category === null) return;
		formSending = true;
		formError = null;
		try {
			await apiFetch<{ status: string }>('/api/v1/feedback', {
				method: 'POST',
				body: JSON.stringify({ category, text: text.trim() })
			});
			formSubmitted = true;
			text = '';
			category = null;
			haptic('tap');
		} catch (err) {
			formError = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
			haptic('error');
		} finally {
			formSending = false;
		}
	}
</script>

{#if mode === 'chat'}
	<div class="chat-screen">
		<div class="menu-head">
			<h1 class="menu-title">Фидбек</h1>
			<div class="menu-sub">баг, идея или жалоба — админы увидят</div>
		</div>

		<button type="button" class="mode-toggle" onclick={switchToManual}>Заполнить вручную</button>

		<div class="chat-thread" bind:this={threadEl}>
			{#each messages as msg, i (i)}
				<div class={`bubble ${msg.role === 'user' ? 'bubble-user' : 'bubble-assistant'}`}>
					{msg.content}
				</div>
			{/each}

			{#if chatSending}
				<div class="bubble bubble-assistant bubble-typing">
					<span>AI печатает…</span>
				</div>
			{/if}

			{#if submittedCategory}
				<div class="tr-success chat-submitted">
					<div class="tr-success-title">Заявка отправлена</div>
					<div class="tr-success-sub">
						Категория: «{CATEGORY_LABELS[submittedCategory]}» — админы её увидят.
					</div>
				</div>
			{/if}
		</div>

		{#if submittedCategory === null}
			<div class="chat-input-row">
				<input
					class="tr-input chat-input"
					type="text"
					placeholder="Напиши сообщение…"
					bind:value={chatInput}
					disabled={chatSending}
					onkeydown={handleChatKeydown}
				/>
				<button
					type="button"
					class="chat-send"
					aria-label="Отправить"
					disabled={!chatInput.trim() || chatSending}
					onclick={sendChatMessage}
				>
					→
				</button>
			</div>
		{/if}
	</div>
{:else}
	<div class="tr-screen">
		<div class="menu-head">
			<h1 class="menu-title">Фидбек</h1>
			<div class="menu-sub">баг, идея или жалоба — админы увидят</div>
		</div>

		{#if manualSwitch}
			<button type="button" class="mode-toggle" onclick={switchBackToChat}>
				Попробовать с AI-помощником
			</button>
		{/if}

		{#if degraded}
			<div class="degraded-banner">
				🤖 AI-помощник сейчас недоступен — заполни форму вручную. Всё, что ты уже написал,
				сохранено ниже.
			</div>
		{/if}

		<div class="fb-field">
			<span class="tr-field-label">Категория</span>
			<div class="fb-chips">
				{#each CATEGORY_CHIPS as chipOption (chipOption.value)}
					<button
						type="button"
						class={`chip ${category === chipOption.value ? 'chip-on' : ''}`}
						disabled={formSending}
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
				disabled={formSending}
			></textarea>
		</label>

		{#if formError}
			<div class="tr-error">{formError}</div>
		{/if}

		{#if formSubmitted}
			<div class="tr-success">
				<div class="tr-success-title">Спасибо!</div>
				<div class="tr-success-sub">Заявка отправлена — админы её увидят.</div>
			</div>
		{/if}

		<button type="button" class="tr-cta" disabled={!canSubmit} onclick={sendForm}>
			<span class="tr-cta-label">{formSending ? 'отправляем…' : 'ОТПРАВИТЬ'}</span>
		</button>
	</div>
{/if}

<style>
	.tr-screen,
	.chat-screen {
		padding: 24px 18px 32px;
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
	}
	.chat-screen {
		min-height: var(--tg-viewport-stable-height, 100vh);
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

	/* ─── mode toggle (link-style, both directions) ──────────────────────── */
	.mode-toggle {
		background: none;
		border: none;
		padding: 0;
		align-self: flex-start;
		color: var(--accent-cyan);
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		text-decoration: underline;
		cursor: pointer;
		min-height: unset;
	}

	/* ─── AI chat thread ──────────────────────────────────────────────────── */
	.chat-thread {
		flex: 1;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
		min-height: 200px;
	}
	.bubble {
		max-width: 78%;
		padding: var(--space-sm) var(--space-md);
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		line-height: 1.5;
		white-space: pre-wrap;
		word-break: break-word;
	}
	.bubble-assistant {
		align-self: flex-start;
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px 14px 14px 4px;
		color: var(--text-secondary);
	}
	.bubble-user {
		align-self: flex-end;
		background: var(--bg-secondary-1);
		border: 2px solid var(--accent-cyan);
		border-radius: 14px 14px 4px 14px;
		color: var(--text-primary);
	}
	.bubble-typing span {
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
		animation: tokens-pulse 1.4s ease-in-out infinite;
	}
	.chat-submitted {
		align-self: stretch;
	}

	.chat-input-row {
		display: flex;
		align-items: center;
		gap: var(--space-sm);
		padding-bottom: env(safe-area-inset-bottom);
	}
	.chat-input {
		flex: 1;
		font-family: var(--font-body);
		font-size: var(--font-body-size);
	}
	.chat-send {
		flex-shrink: 0;
		width: 44px;
		height: 44px;
		min-height: 44px;
		border-radius: 12px;
		border: none;
		background: var(--accent-pink);
		color: #1a0f12;
		font-size: 20px;
		font-weight: 900;
		line-height: 1;
		cursor: pointer;
		display: flex;
		align-items: center;
		justify-content: center;
		box-shadow: 3px 3px 0 #111;
		transition: transform 0.08s;
	}
	.chat-send:active:not(:disabled) {
		transform: translate(2px, 2px);
		box-shadow: 1px 1px 0 #111;
	}
	.chat-send:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* ─── degraded banner (informational, NOT destructive) ───────────────── */
	.degraded-banner {
		background: var(--bg-secondary-2);
		border: 2px solid var(--accent-yellow);
		border-radius: 10px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
		color: var(--text-secondary);
		line-height: 1.5;
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
