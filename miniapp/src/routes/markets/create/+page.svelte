<script lang="ts">
	// Create Market — miniapp entry point for BET-01 market creation (D-05),
	// previously chat-command-only (`/market_create` in bot/handlers/markets.py).
	// Thin screen over POST /api/v1/markets (api/routes/markets.py), which is
	// itself a thin wrapper over markets_service.create_market — this file
	// duplicates NO business rules, it only mirrors the service's documented
	// limits as client-side hints so the form can show inline errors before a
	// round-trip. The server stays the final authority — every limit below is
	// re-checked there and its error message is what actually reaches the user
	// on a 400/409.
	//
	// Limits mirrored from bot/services/markets_service.py module constants
	// (QUESTION_MIN_LEN/MAX_LEN, MIN_OPTIONS/MAX_OPTIONS) and from
	// bot/config.py's `market_creation_fee` (MARKET_CREATION_FEE env var,
	// default 100) — same "hardcode the documented default, server enforces
	// the real value" convention already used by BET_CHIPS/STAR_CHIPS on the
	// sibling bet/donate screens (no dedicated GET /config route exists, and
	// this task's backend scope is exactly the one POST route).
	//
	// Duration is chip-only (no free-text entry) — every chip value is a
	// pre-validated token accepted by markets_service.parse_duration
	// ("<n>h"/"<n>d"), so there is no client-side duration error state to
	// show; an out-of-range custom duration simply cannot be constructed here.
	import { goto } from '$app/navigation';
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	const QUESTION_MIN_LEN = 5;
	const QUESTION_MAX_LEN = 400;
	const MIN_OPTIONS = 2;
	const MAX_OPTIONS = 6;
	const CREATION_FEE = 100;
	// common/models/market.py: MarketOption.label is String(200) — markets_service
	// only checks labels are non-empty, not their length, so an over-length label
	// would otherwise reach the DB layer unvalidated. Client-side cap only;
	// still not authoritative (server truncation/error, if any, wins).
	const OPTION_LABEL_MAX_LEN = 200;

	const DURATION_CHIPS = [
		{ value: '1h', label: '1ч' },
		{ value: '6h', label: '6ч' },
		{ value: '24h', label: '24ч' },
		{ value: '3d', label: '3д' },
		{ value: '7d', label: '7д' }
	];

	type MarketOption = { id: number; position: number; label: string; pool: number; share_pct: number };
	type CreateMarketResult = {
		id: number;
		question: string;
		status: string;
		closes_at: string;
		total_pool: number;
		winning_option_id: number | null;
		options: MarketOption[];
		user_balance_after: number;
	};

	let question = $state('');
	let options = $state<string[]>(['', '']);
	let duration = $state('24h');
	let submitting = $state(false);
	let submitAttempted = $state(false);
	let submitError = $state<string | null>(null);
	let created = $state<CreateMarketResult | null>(null);

	let trimmedQuestion = $derived(question.trim());
	let filledOptionsCount = $derived(options.filter((o) => o.trim().length > 0).length);
	let questionValid = $derived(trimmedQuestion.length >= QUESTION_MIN_LEN);
	let optionsValid = $derived(filledOptionsCount >= MIN_OPTIONS);
	let canSubmit = $derived(questionValid && optionsValid && !submitting);
	let durationLabel = $derived(DURATION_CHIPS.find((d) => d.value === duration)?.label ?? duration);

	function addOption() {
		if (options.length >= MAX_OPTIONS) return;
		options = [...options, ''];
		haptic('tap');
	}

	function removeOption(index: number) {
		if (options.length <= MIN_OPTIONS) return;
		options = options.filter((_, i) => i !== index);
	}

	function describeError(err: unknown): string {
		return err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
	}

	async function submit() {
		submitAttempted = true;
		if (!canSubmit) {
			haptic('error');
			return;
		}
		submitting = true;
		submitError = null;
		try {
			const result = await apiFetch<CreateMarketResult>('/api/v1/markets', {
				method: 'POST',
				body: JSON.stringify({
					question: trimmedQuestion,
					options: options.map((o) => o.trim()).filter((o) => o.length > 0),
					duration,
					ref_id: `market_create:${crypto.randomUUID()}`
				})
			});
			created = result;
			haptic('win');
		} catch (err) {
			submitError = describeError(err);
			haptic('error');
		} finally {
			submitting = false;
		}
	}
</script>

<div class="mc-screen">
	<div class="menu-head">
		<h1 class="menu-title">Создать рынок</h1>
		<div class="menu-sub">вопрос, варианты ответа, срок ставок</div>
	</div>

	{#if created}
		<div class="tr-success">
			<div class="tr-success-title">Рынок #{created.id} создан</div>
			<div class="tr-success-sub">Комиссия {CREATION_FEE}¥ списана — рынок открыт для ставок.</div>
			<button type="button" class="mode-toggle mc-goto-market" onclick={() => goto(`/markets/${created!.id}`)}>
				Перейти к рынку →
			</button>
		</div>
	{:else}
		<label class="tr-field">
			<span class="tr-field-label">Вопрос</span>
			<textarea
				class="tr-input mc-question"
				placeholder="Например: Кто выиграет турнир?"
				maxlength={QUESTION_MAX_LEN}
				bind:value={question}
				disabled={submitting}
			></textarea>
			<span class="mc-counter">{trimmedQuestion.length}/{QUESTION_MAX_LEN}</span>
		</label>
		{#if submitAttempted && !questionValid}
			<div class="mc-field-error">Вопрос должен быть не короче {QUESTION_MIN_LEN} символов.</div>
		{/if}

		<div class="tr-field">
			<span class="tr-field-label">Варианты ответа</span>
			<div class="mc-options-list">
				{#each options as _, i (i)}
					<div class="mc-opt-row">
						<input
							class="tr-input mc-opt-input"
							type="text"
							placeholder={`вариант ${i + 1}`}
							maxlength={OPTION_LABEL_MAX_LEN}
							bind:value={options[i]}
							disabled={submitting}
						/>
						{#if options.length > MIN_OPTIONS}
							<button
								type="button"
								class="mc-opt-remove"
								aria-label="Удалить вариант"
								disabled={submitting}
								onclick={() => removeOption(i)}
							>
								×
							</button>
						{/if}
					</div>
				{/each}
			</div>
			<button
				type="button"
				class="chip mc-opt-add"
				disabled={submitting || options.length >= MAX_OPTIONS}
				onclick={addOption}
			>
				+ вариант ({options.length}/{MAX_OPTIONS})
			</button>
		</div>
		{#if submitAttempted && !optionsValid}
			<div class="mc-field-error">Нужно минимум {MIN_OPTIONS} заполненных варианта.</div>
		{/if}

		<div class="tr-field">
			<span class="tr-field-label">Срок приёма ставок</span>
			<div class="bet-chips">
				{#each DURATION_CHIPS as d (d.value)}
					<button
						type="button"
						class={`chip ${duration === d.value ? 'chip-on' : ''}`}
						disabled={submitting}
						onclick={() => (duration = d.value)}
					>
						{d.label}
					</button>
				{/each}
			</div>
		</div>

		<div class="tr-fee-note">
			Комиссия создания <strong>{CREATION_FEE}¥</strong> спишется с твоего баланса в банк чата сразу при
			создании рынка.
		</div>

		{#if submitError}
			<div class="tr-error">{submitError}</div>
		{/if}

		<button type="button" class="tr-cta" disabled={!canSubmit} onclick={submit}>
			<span class="tr-cta-label">{submitting ? 'создаём…' : 'СОЗДАТЬ РЫНОК'}</span>
			<span class="tr-cta-sub">{submitting ? '' : `комиссия ${CREATION_FEE}¥ · закрытие через ${durationLabel}`}</span>
		</button>
	{/if}
</div>

<style>
	.mc-screen {
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
	.mc-question {
		min-height: 84px;
		resize: vertical;
		line-height: 1.5;
	}
	.mc-counter {
		align-self: flex-end;
		font-size: 11px;
		color: var(--text-muted);
		font-family: var(--font-body);
	}

	.mc-options-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.mc-opt-row {
		display: flex;
		align-items: center;
		gap: var(--space-xs);
	}
	.mc-opt-input {
		flex: 1;
	}
	.mc-opt-remove {
		flex-shrink: 0;
		width: 44px;
		height: 44px;
		border-radius: 10px;
		border: 2px solid var(--border-secondary);
		background: var(--bg-secondary-2);
		color: var(--destructive-text);
		font-size: 20px;
		font-weight: 900;
		line-height: 1;
		cursor: pointer;
		display: flex;
		align-items: center;
		justify-content: center;
	}
	.mc-opt-remove:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}
	.mc-opt-add {
		align-self: flex-start;
		background: var(--bg-secondary-2);
		color: var(--accent-cyan);
		box-shadow: none;
	}
	.mc-opt-add:hover:not(:disabled) {
		transform: none;
		box-shadow: none;
	}

	.bet-chips {
		display: grid;
		grid-template-columns: repeat(5, 1fr);
		gap: var(--space-xs);
	}

	.mc-field-error {
		color: var(--destructive-text);
		font-size: 12px;
		font-family: var(--font-body);
		margin-top: calc(-1 * var(--space-xs));
	}

	.tr-fee-note {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		line-height: 1.5;
		font-family: var(--font-body);
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 10px;
		padding: var(--space-sm) var(--space-md);
	}
	.tr-fee-note strong {
		color: var(--text-primary);
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
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
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
		font-family: var(--font-body);
		line-height: 1.4;
	}
	.mc-goto-market {
		align-self: center;
	}
	.mode-toggle {
		background: none;
		border: none;
		padding: 0;
		color: var(--accent-cyan);
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		text-decoration: underline;
		cursor: pointer;
		min-height: unset;
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
	.tr-cta-sub {
		font-size: 12px;
		color: #3a1420;
		font-family: var(--font-body);
	}
</style>
