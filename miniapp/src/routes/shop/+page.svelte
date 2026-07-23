<script lang="ts">
	// Магазин — soc-shop: poke/hug/joke_order/roast (SHOP-01, D-01..D-04).
	// GET /api/v1/shop for live costs, POST /api/v1/shop/{action} to fire one.
	// One target picker feeds all four action cards (no reason to ask "who"
	// four times) — GET /api/v1/members already exists for the autocomplete
	// (same UserPicker duel/transfer use). joke_order additionally needs a
	// topic string (social_service.do_joke_order's `topic` param).
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';
	import UserPicker from '$lib/components/UserPicker.svelte';

	type Costs = { poke: number; hug: number; joke_order: number; roast: number };
	type ShopAction = 'poke' | 'hug' | 'joke_order' | 'roast';
	type ActionResult = { text: string | null; replayed: boolean; user_balance_after: number };

	const ACTIONS: { key: ShopAction; emoji: string; label: string; hint: string }[] = [
		{ key: 'poke', emoji: '👉', label: 'Тыкнуть', hint: 'шаблонный тычок в чат' },
		{ key: 'hug', emoji: '🤗', label: 'Обнять', hint: 'тёплые обнимашки в чат' },
		{ key: 'joke_order', emoji: '🎭', label: 'Анекдот на заказ', hint: 'AI сочинит анекдот на твою тему' },
		{ key: 'roast', emoji: '🔥', label: 'Роаст', hint: 'AI жёстко подколет (без травли)' }
	];

	let loading = $state(true);
	let loadError = $state<string | null>(null);
	let costs = $state<Costs | null>(null);

	let targetUserId = $state<number | null>(null);
	let topic = $state('');

	let busyAction = $state<ShopAction | null>(null);
	let actionError = $state<string | null>(null);
	let lastResult = $state<{ action: ShopAction; result: ActionResult } | null>(null);

	function describeError(err: unknown): string {
		return err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
	}

	async function loadCosts() {
		try {
			const res = await apiFetch<{ costs: Costs }>('/api/v1/shop');
			costs = res.costs;
		} catch (err) {
			loadError = describeError(err);
		} finally {
			loading = false;
		}
	}

	async function runAction(action: ShopAction) {
		if (busyAction) return;
		if (targetUserId === null) {
			actionError = 'Сначала выбери, на кого — по @нику, имени или ID.';
			return;
		}
		if (action === 'joke_order' && !topic.trim()) {
			actionError = 'Укажи тему анекдота.';
			return;
		}

		busyAction = action;
		actionError = null;
		try {
			const body: Record<string, unknown> = {
				target_user_id: targetUserId,
				idem_key: `shop_${action}:${crypto.randomUUID()}`
			};
			if (action === 'joke_order') body.topic = topic.trim();

			const res = await apiFetch<ActionResult>(`/api/v1/shop/${action}`, {
				method: 'POST',
				body: JSON.stringify(body)
			});
			lastResult = { action, result: res };
			haptic(res.replayed ? 'tap' : 'win');
		} catch (err) {
			actionError = describeError(err);
			haptic('error');
		} finally {
			busyAction = null;
		}
	}

	function actionLabel(action: ShopAction): string {
		return ACTIONS.find((a) => a.key === action)?.label ?? action;
	}

	onMount(loadCosts);
</script>

{#if loading}
	<div class="screen-loading"><span>загрузка магазина…</span></div>
{:else if loadError}
	<div class="screen-error">
		<h2>Ошибка</h2>
		<div class="err-msg">{loadError}</div>
		<button type="button" onclick={loadCosts}>Повторить</button>
	</div>
{:else if costs}
	<div class="shop-screen">
		<div class="menu-head">
			<h1 class="menu-title">Магазин</h1>
			<div class="menu-sub">платные подколки для чата</div>
		</div>

		<UserPicker bind:value={targetUserId} label="На кого" placeholder="@ник, имя или ID" />

		{#if actionError}
			<div class="shop-error">{actionError}</div>
		{/if}

		{#if lastResult}
			<div class="shop-result">
				<div class="shop-result-title">{actionLabel(lastResult.action)}</div>
				{#if lastResult.result.replayed}
					<div class="shop-result-text shop-result-muted">
						уже выполнено раньше — повторный запрос не списал деньги повторно
					</div>
				{:else}
					<div class="shop-result-text">{lastResult.result.text ?? ''}</div>
				{/if}
			</div>
		{/if}

		<div class="shop-cards">
			{#each ACTIONS as a (a.key)}
				<div class="feature-card shop-card">
					<div class="shop-card-head">
						<span class="shop-card-emoji" aria-hidden="true">{a.emoji}</span>
						<div>
							<span class="fc-title">{a.label}</span>
							<span class="fc-desc">{a.hint}</span>
						</div>
					</div>

					{#if a.key === 'joke_order'}
						<input
							class="shop-topic-input"
							type="text"
							maxlength="200"
							placeholder="тема анекдота, например: коты"
							disabled={busyAction !== null}
							bind:value={topic}
						/>
					{/if}

					<button
						type="button"
						class="chip chip-all shop-card-btn"
						disabled={busyAction !== null}
						onclick={() => runAction(a.key)}
					>
						{busyAction === a.key ? 'выполняем…' : `${a.label} · ${costs[a.key]}¥`}
					</button>
				</div>
			{/each}
		</div>
	</div>
{/if}

<style>
	.shop-screen {
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

	.shop-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.shop-result {
		background: var(--bg-secondary-2);
		border: 2px solid var(--accent-pink);
		border-radius: 14px;
		padding: var(--space-md);
	}
	.shop-result-title {
		font-family: var(--font-chrome);
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--accent-pink);
	}
	.shop-result-text {
		margin-top: var(--space-xs);
		font-family: var(--font-body);
		font-size: var(--font-heading-size);
		color: var(--text-primary);
		line-height: 1.5;
	}
	.shop-result-muted {
		color: var(--text-muted);
		font-size: var(--font-body-size);
	}

	.shop-cards {
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.shop-card {
		gap: var(--space-sm);
		cursor: default;
	}
	.shop-card-head {
		display: flex;
		align-items: center;
		gap: var(--space-sm);
	}
	.shop-card-head > div {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.shop-card-emoji {
		font-size: 28px;
		line-height: 1;
	}
	.shop-topic-input {
		background: var(--bg-dominant);
		border: 2px solid var(--border-secondary);
		border-radius: 8px;
		padding: var(--space-sm);
		color: var(--text-primary);
		font-family: var(--font-body);
		font-size: var(--font-body-size);
	}
	.shop-topic-input:focus {
		outline: none;
		border-color: var(--accent-pink);
	}
	.shop-card-btn {
		width: 100%;
	}
</style>
