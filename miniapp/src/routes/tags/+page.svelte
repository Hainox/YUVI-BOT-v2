<script lang="ts">
	// Теги — рынок аренды Telegram custom_title (TAG-02, D-07/D-08).
	// GET /api/v1/tags: pricing + текущая аренда (если есть) вызывающего.
	// Пока аренда активна/подвешена — показываем её карточку с отменой
	// вместо формы новой аренды (tag_rental_service действует только на
	// вызывающего, V4 — та же дисциплина, что bot/handlers/tags.py).
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	type Pricing = { per_day: number; allowed_days: number[]; title_max: number };
	type Rental = { title: string; status: string; price_paid: number | null; expires_at: string | null };
	type TagsState = { pricing: Pricing; active: Rental | null };
	type RentResult = Rental & { user_balance_after: number };

	const STATUS_LABELS: Record<string, string> = {
		active: 'активна — тег стоит',
		suspended: 'в очереди — сейчас приоритет у номинанта дня'
	};

	let loading = $state(true);
	let loadError = $state<string | null>(null);
	let pricing = $state<Pricing | null>(null);
	let active = $state<Rental | null>(null);

	let title = $state('');
	let days = $state(1);
	let renting = $state(false);
	let rentError = $state<string | null>(null);

	let cancelling = $state(false);
	let cancelError = $state<string | null>(null);

	function describeError(err: unknown): string {
		return err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
	}

	async function load() {
		loading = true;
		loadError = null;
		try {
			const res = await apiFetch<TagsState>('/api/v1/tags');
			pricing = res.pricing;
			active = res.active;
			if (res.pricing.allowed_days.length > 0) days = res.pricing.allowed_days[0];
		} catch (err) {
			loadError = describeError(err);
		} finally {
			loading = false;
		}
	}

	function formatExpires(iso: string | null): string {
		if (!iso) return '';
		return new Date(iso).toLocaleString('ru-RU', {
			day: '2-digit',
			month: '2-digit',
			hour: '2-digit',
			minute: '2-digit'
		});
	}

	let pricePreview = $derived(pricing ? pricing.per_day * days : 0);
	let titleTrimmed = $derived(title.trim());
	let titleTooLong = $derived(pricing !== null && titleTrimmed.length > pricing.title_max);
	let canRent = $derived(
		!renting && titleTrimmed.length > 0 && !titleTooLong && pricing !== null
	);

	async function rent() {
		if (!canRent) return;
		renting = true;
		rentError = null;
		try {
			const res = await apiFetch<RentResult>('/api/v1/tags/rent', {
				method: 'POST',
				body: JSON.stringify({
					title: titleTrimmed,
					days,
					idem_key: `tag_rent:${crypto.randomUUID()}`
				})
			});
			active = { title: res.title, status: res.status, price_paid: res.price_paid, expires_at: res.expires_at };
			title = '';
			haptic('win');
		} catch (err) {
			rentError = describeError(err);
			haptic('error');
		} finally {
			renting = false;
		}
	}

	async function cancel() {
		if (cancelling) return;
		cancelling = true;
		cancelError = null;
		try {
			await apiFetch<{ cancelled: boolean }>('/api/v1/tags/cancel', { method: 'POST' });
			active = null;
			haptic('tap');
		} catch (err) {
			cancelError = describeError(err);
			haptic('error');
		} finally {
			cancelling = false;
		}
	}

	onMount(load);
</script>

{#if loading}
	<div class="screen-loading"><span>загрузка тегов…</span></div>
{:else if loadError}
	<div class="screen-error">
		<h2>Ошибка</h2>
		<div class="err-msg">{loadError}</div>
		<button type="button" onclick={load}>Повторить</button>
	</div>
{:else if pricing}
	<div class="tags-screen">
		<div class="menu-head">
			<h1 class="menu-title">Теги</h1>
			<div class="menu-sub">аренда персонального тега над именем</div>
		</div>

		{#if active}
			<div class="tags-active">
				<div class="tags-active-label">твой тег</div>
				<div class="tags-active-title">«{active.title}»</div>
				<div class="tags-active-status">{STATUS_LABELS[active.status] ?? active.status}</div>
				{#if active.expires_at}
					<div class="tags-active-expires">до {formatExpires(active.expires_at)}</div>
				{/if}
				{#if active.price_paid !== null}
					<div class="tags-active-price">оплачено {active.price_paid}¥ · возврата нет</div>
				{/if}

				{#if cancelError}
					<div class="tags-error">{cancelError}</div>
				{/if}

				<button type="button" class="chip tags-cancel-btn" disabled={cancelling} onclick={cancel}>
					{cancelling ? 'отменяем…' : 'отменить аренду'}
				</button>
			</div>
		{:else}
			<div class="tags-form feature-card">
				<span class="fc-title">Новая аренда</span>
				<span class="fc-desc">
					{pricing.per_day}¥/день · до {pricing.title_max} символов · тег виден всем в чате
				</span>

				<label class="tags-field">
					<span class="tags-field-label">Титул</span>
					<input
						class="tags-input"
						type="text"
						maxlength={pricing.title_max}
						placeholder="например: Босс"
						disabled={renting}
						bind:value={title}
					/>
					<span class="tags-counter" class:tags-counter-over={titleTooLong}>
						{titleTrimmed.length}/{pricing.title_max}
					</span>
				</label>

				<div class="tags-days-row">
					<span class="tags-field-label">Срок</span>
					<div class="tags-days-chips">
						{#each pricing.allowed_days as d (d)}
							<button
								type="button"
								class={`chip ${days === d ? 'chip-on' : ''}`}
								disabled={renting}
								onclick={() => (days = d)}
							>
								{d} дн.
							</button>
						{/each}
					</div>
				</div>

				<div class="tags-preview">
					цена: <strong>{pricePreview}¥</strong>
				</div>

				{#if rentError}
					<div class="tags-error">{rentError}</div>
				{/if}

				<button type="button" class="chip chip-all tags-rent-btn" disabled={!canRent} onclick={rent}>
					{renting ? 'оформляем…' : `арендовать за ${pricePreview}¥`}
				</button>
			</div>
		{/if}
	</div>
{/if}

<style>
	.tags-screen {
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

	.tags-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.tags-active {
		background: var(--bg-secondary-2);
		border: 2px solid var(--accent-yellow);
		border-radius: 14px;
		padding: var(--space-lg);
		text-align: center;
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.tags-active-label {
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
	}
	.tags-active-title {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		color: var(--accent-yellow);
		line-height: 1.1;
	}
	.tags-active-status {
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		color: var(--text-secondary);
	}
	.tags-active-expires,
	.tags-active-price {
		font-size: 12px;
		color: var(--text-muted);
		font-family: var(--font-body);
	}
	.tags-cancel-btn {
		margin-top: var(--space-sm);
		align-self: center;
	}

	.tags-form {
		gap: var(--space-md);
	}

	.tags-field {
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
		position: relative;
	}
	.tags-field-label {
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
	}
	.tags-input {
		background: var(--bg-dominant);
		border: 2px solid var(--border-secondary);
		border-radius: 10px;
		padding: var(--space-sm) var(--space-md);
		font-family: var(--font-body);
		font-size: var(--font-heading-size);
		color: var(--text-primary);
	}
	.tags-input:focus {
		outline: none;
		border-color: var(--accent-yellow);
	}
	.tags-counter {
		align-self: flex-end;
		font-size: 11px;
		color: var(--text-muted);
		font-family: var(--font-numeric);
	}
	.tags-counter-over {
		color: var(--destructive-text);
	}

	.tags-days-row {
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.tags-days-chips {
		display: flex;
		gap: var(--space-xs);
	}
	.tags-days-chips .chip {
		flex: 1;
	}

	.tags-preview {
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		color: var(--text-secondary);
	}
	.tags-preview strong {
		font-family: var(--font-numeric);
		font-size: var(--font-heading-size);
		color: var(--accent-yellow);
	}

	.tags-rent-btn {
		width: 100%;
	}
</style>
