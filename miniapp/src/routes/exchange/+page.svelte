<script lang="ts">
	// Exchange — P2P биржа ювиков (EXCHANGE-01). Seller lists yuviks for sale
	// describing what they want in return as free text (NOT a bot-tracked
	// structured price) — the actual payment happens off-platform between two
	// humans, the bot only escrows the yuvik side. Three-tab shape (browse /
	// create / mine) mirrors duel/+page.svelte's tab structure; row shape for
	// the open-listings list reuses markets/+page.svelte's mk-row rhythm.
	//
	// Single accent for this screen: cyan (matches the "Биржа" hub tile) —
	// shared chip-on/chip-all primitives keep their own baked-in colors
	// (yellow/pink respectively, same as every other screen that reuses them).
	//
	// Honest-disclaimer requirement: the escrow/claim/confirm dance only
	// protects the yuvik side against a seller rug-pull — it cannot verify the
	// buyer actually paid off-platform. The banner below and the create-form
	// copy both say this plainly, not in fine print.
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic, user } from '$lib/tg';

	type OpenListing = {
		id: number;
		seller_user_id: number;
		seller_name: string;
		yuvik_amount: number;
		want_description: string;
		item_type: string;
		created_at: string;
	};

	type MyListing = {
		id: number;
		role: 'seller' | 'buyer';
		status: string;
		yuvik_amount: number;
		want_description: string;
		item_type: string;
		seller_user_id: number;
		seller_name: string;
		claimed_by_user_id: number | null;
		claimed_by_name: string | null;
		created_at: string;
		claimed_at: string | null;
		resolved_at: string | null;
	};

	type Tab = 'browse' | 'create' | 'mine';

	const myId = user?.id ?? null;
	const WANT_MAX_LEN = 300;

	let tab = $state<Tab>('browse');

	function describeError(err: unknown): string {
		return err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
	}

	function statusLabel(status: string): string {
		switch (status) {
			case 'open':
				return 'открыт';
			case 'claimed':
				return 'заклеймлен';
			case 'fulfilled':
				return 'завершён';
			case 'cancelled':
				return 'отменён';
			default:
				return status;
		}
	}

	// --- Browse (GET /exchange + POST /exchange/{id}/claim) ------------------

	let browseLoading = $state(true);
	let browseError = $state<string | null>(null);
	let listings = $state<OpenListing[]>([]);
	let claimingId = $state<number | null>(null);
	let claimError = $state<string | null>(null);

	async function loadOpenListings() {
		browseLoading = true;
		browseError = null;
		try {
			listings = await apiFetch<OpenListing[]>('/api/v1/exchange');
		} catch (err) {
			browseError = describeError(err);
		} finally {
			browseLoading = false;
		}
	}

	async function claim(listingId: number) {
		if (claimingId !== null) return;
		claimingId = listingId;
		claimError = null;
		try {
			const res = await apiFetch<{ status: string; claimed: boolean }>(
				`/api/v1/exchange/${listingId}/claim`,
				{ method: 'POST' }
			);
			if (res.claimed) {
				haptic('tap');
				await loadOpenListings();
				myListingsLoaded = false;
			} else {
				claimError = 'Этот листинг уже кто-то заклеймил раньше.';
				await loadOpenListings();
			}
		} catch (err) {
			claimError = describeError(err);
			haptic('error');
		} finally {
			claimingId = null;
		}
	}

	// --- Create (POST /exchange) ---------------------------------------------

	let createAmount = $state(100);
	let createDescription = $state('');
	let creating = $state(false);
	let createError = $state<string | null>(null);
	let createdListing = $state<{ id: number; yuvik_amount: number } | null>(null);

	async function createListing() {
		if (creating) return;
		const description = createDescription.trim();
		if (!description) {
			createError = 'Опиши, что хочешь получить взамен.';
			return;
		}
		if (createAmount < 1) {
			createError = 'Сумма должна быть положительной.';
			return;
		}
		creating = true;
		createError = null;
		createdListing = null;
		try {
			const res = await apiFetch<{ id: number; yuvik_amount: number }>('/api/v1/exchange', {
				method: 'POST',
				body: JSON.stringify({
					yuvik_amount: createAmount,
					want_description: description,
					ref_id: `exchange_create:${crypto.randomUUID()}`
				})
			});
			createdListing = res;
			createDescription = '';
			haptic('tap');
			await loadOpenListings();
			myListingsLoaded = false;
		} catch (err) {
			createError = describeError(err);
			haptic('error');
		} finally {
			creating = false;
		}
	}

	// --- Mine (GET /exchange/mine + cancel/confirm) ---------------------------

	let myListingsLoaded = false;
	let myListingsLoading = $state(true);
	let myListingsError = $state<string | null>(null);
	let myListings = $state<MyListing[]>([]);
	let actingOnId = $state<number | null>(null);
	let mineActionError = $state<string | null>(null);

	async function loadMyListings() {
		myListingsLoading = true;
		myListingsError = null;
		try {
			myListings = await apiFetch<MyListing[]>('/api/v1/exchange/mine');
			myListingsLoaded = true;
		} catch (err) {
			myListingsError = describeError(err);
		} finally {
			myListingsLoading = false;
		}
	}

	async function cancelListing(listingId: number) {
		if (actingOnId !== null) return;
		actingOnId = listingId;
		mineActionError = null;
		try {
			await apiFetch(`/api/v1/exchange/${listingId}/cancel`, { method: 'POST' });
			haptic('tap');
			await loadMyListings();
			await loadOpenListings();
		} catch (err) {
			mineActionError = describeError(err);
			haptic('error');
		} finally {
			actingOnId = null;
		}
	}

	async function confirmListing(listingId: number) {
		if (actingOnId !== null) return;
		actingOnId = listingId;
		mineActionError = null;
		try {
			await apiFetch(`/api/v1/exchange/${listingId}/confirm`, {
				method: 'POST',
				body: JSON.stringify({ ref_id: `exchange_confirm:${crypto.randomUUID()}` })
			});
			haptic('big-win');
			await loadMyListings();
		} catch (err) {
			mineActionError = describeError(err);
			haptic('error');
		} finally {
			actingOnId = null;
		}
	}

	function selectTab(next: Tab) {
		tab = next;
		if (next === 'mine' && !myListingsLoaded) loadMyListings();
	}

	onMount(loadOpenListings);
</script>

<div class="ex-screen">
	<div class="menu-head">
		<h1 class="menu-title">Биржа</h1>
		<div class="menu-sub">меняй ювики на что угодно — без посредников</div>
	</div>

	<div class="ex-disclaimer">
		Оплата того, что просит продавец, происходит <b>вне бота</b> — договаривайтесь напрямую.
		Бот только держит ювики продавца в эскроу и переводит их покупателю, когда продавец сам
		подтвердит, что реально получил оплату. Будь осторожен: бот не может проверить, что вторая
		сторона выполнит свою часть сделки.
	</div>

	<div class="ex-tabs">
		<button
			type="button"
			class={`chip ex-tab ${tab === 'browse' ? 'chip-on' : ''}`}
			onclick={() => selectTab('browse')}
		>
			биржа
		</button>
		<button
			type="button"
			class={`chip ex-tab ${tab === 'create' ? 'chip-on' : ''}`}
			onclick={() => selectTab('create')}
		>
			продать
		</button>
		<button
			type="button"
			class={`chip ex-tab ${tab === 'mine' ? 'chip-on' : ''}`}
			onclick={() => selectTab('mine')}
		>
			мои
		</button>
	</div>

	{#if tab === 'browse'}
		<div class="ex-panel">
			{#if browseLoading}
				<div class="screen-loading"><span>загрузка листингов…</span></div>
			{:else if browseError}
				<div class="ex-error">{browseError}</div>
			{:else if listings.length === 0}
				<div class="ex-empty">
					<div class="ex-empty-title">пусто</div>
					<div class="ex-empty-hint">Открытых листингов пока нет — стань первым во вкладке «продать».</div>
				</div>
			{:else}
				{#if claimError}
					<div class="ex-error">{claimError}</div>
				{/if}
				<div class="ex-list">
					{#each listings as l (l.id)}
						{@const mine = myId != null && l.seller_user_id === myId}
						<div class="ex-row">
							<div class="ex-row-top">
								<span class="ex-row-amount">{l.yuvik_amount}<small>¥</small></span>
								<span class="ex-row-seller">{mine ? 'твой листинг' : l.seller_name}</span>
							</div>
							<div class="ex-row-want">хочет: {l.want_description}</div>
							<button
								type="button"
								class="chip chip-all ex-claim-btn"
								disabled={mine || claimingId === l.id}
								onclick={() => claim(l.id)}
							>
								{mine ? 'не тебе' : claimingId === l.id ? 'клеймим…' : 'заклеймить'}
							</button>
						</div>
					{/each}
				</div>
			{/if}
		</div>
	{:else if tab === 'create'}
		<div class="ex-panel">
			<label class="ex-field">
				<span class="ex-field-label">Сумма (ювики, спишутся в эскроу сразу)</span>
				<input
					class="ex-input"
					type="number"
					inputmode="numeric"
					min="1"
					bind:value={createAmount}
					disabled={creating}
				/>
			</label>

			<label class="ex-field">
				<span class="ex-field-label">Что хочешь взамен ({createDescription.length}/{WANT_MAX_LEN})</span>
				<textarea
					class="ex-textarea"
					rows="3"
					maxlength={WANT_MAX_LEN}
					placeholder="например: 10 ювиков за подписку на канал"
					bind:value={createDescription}
					disabled={creating}
				></textarea>
			</label>

			{#if createError}
				<div class="ex-error">{createError}</div>
			{/if}

			{#if createdListing}
				<div class="ex-created">
					<div class="ex-created-title">Листинг #{createdListing.id} создан</div>
					<div class="ex-created-sub">
						{createdListing.yuvik_amount}¥ уже в эскроу. Он появится на бирже — жди, кто откликнется.
					</div>
				</div>
			{/if}

			<button type="button" class="ex-cta" disabled={creating} onclick={createListing}>
				<span class="ex-cta-label">{creating ? 'выставляем…' : 'ВЫСТАВИТЬ НА БИРЖУ'}</span>
				<span class="ex-cta-sub">{creating ? '' : `${createAmount}¥ уйдут в эскроу`}</span>
			</button>
		</div>
	{:else}
		<div class="ex-panel">
			{#if myListingsLoading}
				<div class="screen-loading"><span>загрузка твоих листингов…</span></div>
			{:else if myListingsError}
				<div class="ex-error">{myListingsError}</div>
			{:else if myListings.length === 0}
				<div class="ex-empty">
					<div class="ex-empty-title">пусто</div>
					<div class="ex-empty-hint">Ты ещё ничего не выставил и ничего не заклеймил.</div>
				</div>
			{:else}
				{#if mineActionError}
					<div class="ex-error">{mineActionError}</div>
				{/if}
				<div class="ex-list">
					{#each myListings as l (l.id + ':' + l.role)}
						<div class="ex-row">
							<div class="ex-row-top">
								<span class="ex-row-amount">{l.yuvik_amount}<small>¥</small></span>
								<span class={`ex-status-chip ex-status-${l.status}`}>{statusLabel(l.status)}</span>
							</div>
							<div class="ex-row-want">хочет: {l.want_description}</div>
							<div class="ex-row-meta">
								{#if l.role === 'seller'}
									роль: продавец
									{#if l.claimed_by_name}· покупатель: {l.claimed_by_name}{/if}
								{:else}
									роль: покупатель · продавец: {l.seller_name}
								{/if}
							</div>

							{#if l.role === 'seller' && l.status === 'open'}
								<button
									type="button"
									class="chip ex-mine-btn"
									disabled={actingOnId === l.id}
									onclick={() => cancelListing(l.id)}
								>
									{actingOnId === l.id ? '…' : 'отменить'}
								</button>
							{:else if l.role === 'seller' && l.status === 'claimed'}
								<button
									type="button"
									class="chip chip-all ex-mine-btn"
									disabled={actingOnId === l.id}
									onclick={() => confirmListing(l.id)}
								>
									{actingOnId === l.id ? '…' : 'я получил оплату — завершить'}
								</button>
							{/if}
						</div>
					{/each}
				</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	.ex-screen {
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

	.ex-disclaimer {
		font-size: var(--font-body-size);
		color: var(--text-secondary);
		line-height: 1.5;
		font-family: var(--font-body);
		background: var(--bg-secondary-2);
		border: 1px solid var(--accent-cyan);
		border-left: 3px solid var(--accent-cyan);
		border-radius: 10px;
		padding: var(--space-sm) var(--space-md);
	}

	.ex-tabs {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: var(--space-xs);
	}
	.ex-tab {
		text-align: center;
	}

	.ex-panel {
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
	}

	.ex-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.ex-empty {
		text-align: center;
		padding: var(--space-2xl) var(--space-lg);
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.ex-empty-title {
		font-family: var(--font-chrome);
		font-size: var(--font-display-size);
		font-weight: 700;
		color: var(--text-secondary);
		text-transform: lowercase;
	}
	.ex-empty-hint {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		font-family: var(--font-body);
		line-height: 1.5;
		max-width: 320px;
		margin: var(--space-xs) auto 0;
	}

	.ex-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.ex-row {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-md);
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.ex-row-top {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--space-sm);
	}
	.ex-row-amount {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		color: var(--accent-cyan);
	}
	.ex-row-amount small {
		font-size: 12px;
		margin-left: 1px;
	}
	.ex-row-seller {
		font-size: 12px;
		color: var(--text-muted);
		font-family: var(--font-body);
	}
	.ex-row-want {
		font-size: var(--font-body-size);
		color: var(--text-secondary);
		font-family: var(--font-body);
		line-height: 1.4;
	}
	.ex-row-meta {
		font-size: 12px;
		color: var(--text-muted);
		font-family: var(--font-body);
	}
	.ex-claim-btn,
	.ex-mine-btn {
		align-self: flex-start;
	}

	.ex-status-chip {
		flex-shrink: 0;
		font-size: 10px;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		padding: 3px 8px;
		border-radius: 20px;
		white-space: nowrap;
		background: var(--bg-secondary-1);
		color: var(--text-muted);
	}
	.ex-status-open {
		background: var(--accent-cyan);
		color: #06262c;
	}
	.ex-status-claimed {
		background: var(--accent-yellow);
		color: #2a2107;
	}
	.ex-status-fulfilled {
		background: var(--positive-bg);
		color: var(--positive-text);
	}
	.ex-status-cancelled {
		background: var(--destructive-bg);
		color: var(--destructive-text);
	}

	.ex-field {
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.ex-field-label {
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
	}
	.ex-input,
	.ex-textarea {
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 10px;
		padding: var(--space-sm) var(--space-md);
		font-family: var(--font-body);
		font-size: var(--font-heading-size);
		color: var(--text-primary);
		resize: vertical;
	}
	.ex-input {
		font-family: var(--font-numeric);
	}
	.ex-input:focus,
	.ex-textarea:focus {
		outline: none;
		border-color: var(--accent-cyan);
	}

	.ex-created {
		background: var(--bg-secondary-2);
		border: 2px solid var(--accent-cyan);
		border-radius: 14px;
		padding: var(--space-md);
		text-align: center;
	}
	.ex-created-title {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		color: var(--accent-cyan);
	}
	.ex-created-sub {
		font-size: 12px;
		color: var(--text-muted);
		margin-top: var(--space-xs);
		font-family: var(--font-body);
		line-height: 1.4;
	}

	/* Sticker-button CTA (locked house style — same box-shadow idiom as
	   duel/+page.svelte's .duel-cta and farm/+page.svelte's .farm-tap-btn). */
	.ex-cta {
		background: var(--accent-cyan);
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
	.ex-cta:active:not(:disabled) {
		transform: translate(2px, 2px);
		box-shadow: 2px 2px 0 #111;
	}
	.ex-cta:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.ex-cta-label {
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		color: #06262c;
		letter-spacing: 0.04em;
	}
	.ex-cta-sub {
		font-size: 12px;
		color: #123842;
		font-family: var(--font-body);
	}
</style>
