<script lang="ts">
	// Donate — Mini App second UI-entry point for STARS-01 (D-10, 06-UI-SPEC.md
	// §"2. Donate screen"). Same backend flow as the bot's `/donate <N>`
	// command: POST /api/v1/donate (api/routes/donate.py) sends a raw Bot API
	// sendInvoice into the same group chat via api/telegram_client.py::
	// send_invoice (api container has no aiogram Bot instance) — chat_id/
	// user_id derive from AuthContext server-side (IDOR, T-06-17), this
	// screen's body carries ONLY {stars}. Payment + juvik credit happen
	// asynchronously in bot/handlers/donate.py::on_successful_payment
	// (idempotent by telegram_payment_charge_id) — this screen does NOT poll
	// for payment completion, same "fire invoice, no polling" contract for
	// both UI entries.
	//
	// Structurally mirrors transfer/+page.svelte's tr-screen/tr-field/tr-cta
	// shape (same primitives, no new tokens). "Last write wins" amount state:
	// tapping a chip sets amountInput; editing amountInput naturally
	// un-highlights any chip since none will match `amount === v`.
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	const STAR_CHIPS = [5, 10, 25, 50, 100];
	const STARS_TO_JUVIK_RATE = 10;

	let amountInput = $state(String(STAR_CHIPS[0]));
	let sending = $state(false);
	let error = $state<string | null>(null);
	let result = $state<{ stars: number } | null>(null);

	let amount = $derived.by(() => {
		const n = Number(amountInput);
		return Number.isFinite(n) ? n : 0;
	});
	let isValid = $derived(Number.isInteger(amount) && amount >= 1);
	let converted = $derived(amount * STARS_TO_JUVIK_RATE);

	function pickChip(v: number) {
		amountInput = String(v);
	}

	async function send() {
		if (sending) return;
		if (!isValid) {
			error = 'Введи целое число звёзд, минимум 1⭐.';
			haptic('error');
			return;
		}
		sending = true;
		error = null;
		result = null;
		try {
			await apiFetch('/api/v1/donate', {
				method: 'POST',
				body: JSON.stringify({ stars: amount })
			});
			result = { stars: amount };
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
		<h1 class="menu-title">Донат</h1>
		<div class="menu-sub">задонать звёздами Telegram — получи ювики</div>
	</div>

	<div class="bet-chips">
		{#each STAR_CHIPS as v (v)}
			<button
				type="button"
				class={`chip ${amount === v ? 'chip-on' : ''}`}
				disabled={sending}
				onclick={() => pickChip(v)}
			>
				{v}
			</button>
		{/each}
	</div>

	<label class="tr-field">
		<span class="tr-field-label">своё число звёзд</span>
		<input
			class="tr-input"
			type="text"
			inputmode="numeric"
			placeholder="например 250"
			bind:value={amountInput}
			disabled={sending}
		/>
	</label>

	<div class="donate-readout">
		<span class="donate-amount">{amount}⭐<span class="donate-eq">=</span>{converted}¥</span>
	</div>

	<div class="tr-fee-note">
		Счёт на оплату придёт в ваш групповой чат — там же нажми «Заплатить». Курс: 1⭐ = {STARS_TO_JUVIK_RATE}
		ювиков.
	</div>

	{#if error}
		<div class="tr-error">{error}</div>
	{/if}

	{#if result}
		<div class="tr-success">
			<div class="tr-success-title">Счёт отправлен в чат</div>
			<div class="tr-success-sub">
				Открой чат и нажми «Заплатить» на инвойсе — ювики придут сразу после оплаты.
			</div>
		</div>
	{/if}

	<button type="button" class="tr-cta" disabled={sending || !isValid} onclick={send}>
		<span class="tr-cta-label">{sending ? 'отправляем…' : 'ЗАДОНАТИТЬ'}</span>
		<span class="tr-cta-sub">{sending ? '' : `${amount}⭐ · счёт придёт в чат`}</span>
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

	.bet-chips {
		display: grid;
		grid-template-columns: repeat(5, 1fr);
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

	.donate-readout {
		display: flex;
		justify-content: center;
	}
	.donate-amount {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		color: var(--text-primary);
	}
	.donate-eq {
		margin: 0 var(--space-xs);
		color: var(--text-muted);
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
