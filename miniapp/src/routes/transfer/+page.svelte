<script lang="ts">
	// Transfer — destination of the "Перевод" hub tile (CASINO-02).
	// 04-UI-SPEC.md §Component Inventory "Transfer screen": single form —
	// recipient picker + amount chips (reuse BET_CHIPS shape) + primary CTA
	// (locked copy "Перевести"). No username-search API exists in this phase
	// (same gap already documented in duel/+page.svelte's Claude's Discretion
	// note) — recipient identity is the raw numeric Telegram user_id, same
	// as the duel challenge form's opponentId field.
	//
	// POST /api/v1/transfer's TransferBody carries no from-user field at all
	// (api/routes/economy.py) — from_user is exclusively auth.user_id
	// server-side (T-04.2-02). Fee (5%, min 1¥, economy_service.
	// transfer_with_fee/settings.transfer_fee_pct) is shown client-side as
	// informational copy before sending, computed with the same
	// max(1, ceil(amount*0.05)) formula the server applies.
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	const BET_CHIPS = [10, 50, 100, 500, 1000];
	const TRANSFER_FEE_PCT = 0.05;

	type TransferResult = {
		status: string;
		balance: number;
	};

	let toUserId = $state('');
	let amount = $state(BET_CHIPS[0]);
	let sending = $state(false);
	let error = $state<string | null>(null);
	let result = $state<{ toUserId: number; amount: number; fee: number } | null>(null);

	let fee = $derived(Math.max(1, Math.ceil(amount * TRANSFER_FEE_PCT)));
	let received = $derived(amount - fee);

	function parsedRecipient(): number | null {
		const id = Number(toUserId);
		return toUserId.trim() && Number.isInteger(id) && id > 0 ? id : null;
	}

	async function send() {
		const recipient = parsedRecipient();
		if (sending) return;
		if (recipient === null) {
			error = 'Введи корректный ID получателя (число).';
			return;
		}
		sending = true;
		error = null;
		result = null;
		try {
			await apiFetch<TransferResult>('/api/v1/transfer', {
				method: 'POST',
				body: JSON.stringify({
					to_user_id: recipient,
					amount,
					ref_id: `transfer:${crypto.randomUUID()}`
				})
			});
			result = { toUserId: recipient, amount, fee };
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
		<h1 class="menu-title">Перевод</h1>
		<div class="menu-sub">закинь другу ювиков</div>
	</div>

	<label class="tr-field">
		<span class="tr-field-label">ID получателя в Telegram</span>
		<input
			class="tr-input"
			type="text"
			inputmode="numeric"
			placeholder="например 123456789"
			bind:value={toUserId}
			disabled={sending}
		/>
	</label>

	<div class="bet-row">
		<div class="bet-display">
			<span class="bet-label">сумма</span>
			<div class="bet-amount">{amount}<small>¥</small></div>
		</div>
		<div class="bet-chips">
			{#each BET_CHIPS as v (v)}
				<button
					type="button"
					class={`chip ${amount === v ? 'chip-on' : ''}`}
					disabled={sending}
					onclick={() => (amount = v)}
				>
					{v}
				</button>
			{/each}
		</div>
	</div>

	<div class="tr-fee-note">
		Комиссия {Math.round(TRANSFER_FEE_PCT * 100)}% (мин. 1¥) уходит в банк чата — получатель получит
		<strong>{received}¥</strong> из {amount}¥.
	</div>

	{#if error}
		<div class="tr-error">{error}</div>
	{/if}

	{#if result}
		<div class="tr-success">
			<div class="tr-success-title">Переведено {result.amount}¥ → id{result.toUserId}</div>
			<div class="tr-success-sub">
				Комиссия {result.fee}¥ ушла в банк чата. Получатель получил {result.amount - result.fee}¥.
			</div>
		</div>
	{/if}

	<button type="button" class="tr-cta" disabled={sending} onclick={send}>
		<span class="tr-cta-label">{sending ? 'отправляем…' : 'ПЕРЕВЕСТИ'}</span>
		<span class="tr-cta-sub">{sending ? '' : `${amount}¥ · комиссия ${fee}¥`}</span>
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
		font-family: var(--font-numeric);
		font-size: var(--font-heading-size);
		color: var(--text-primary);
	}
	.tr-input:focus {
		outline: none;
		border-color: var(--accent-pink);
	}

	.bet-row {
		display: flex;
		align-items: center;
		gap: var(--space-md);
	}
	.bet-display {
		display: flex;
		flex-direction: column;
	}
	.bet-label {
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
	}
	.bet-amount {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		color: var(--text-primary);
	}
	.bet-amount small {
		font-size: 12px;
		color: var(--accent-pink);
		margin-left: 1px;
	}
	.bet-chips {
		display: grid;
		grid-template-columns: repeat(5, 1fr);
		gap: var(--space-xs);
		flex: 1;
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
	.tr-cta-sub {
		font-size: 12px;
		color: #3a1420;
		font-family: var(--font-body);
	}
</style>
