<script lang="ts">
	// History — destination of the "История" hub tile (CASINO-02). GET
	// /api/v1/history already scopes rows to the auth user (T-04.2-12), so no
	// actor column is needed here (every row is "you") — the same hist-row
	// shape as webapp/history.jsx, just without the @actor column since the
	// combined economy+casino+farm+gacha+duel feed is inherently self-scoped.
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';

	type HistoryRow = {
		id: number;
		user_id: number | null;
		amount: number;
		kind: string;
		ref_id: string | null;
		note: string | null;
		created_at: string;
	};

	// Human labels for every `kind` string this project's services log
	// against a real user row (bank-only mirror kinds are already filtered
	// server-side by economy_service.HIDDEN_KINDS and never reach here for
	// user_id IS NULL rows anyway, since GET /history always scopes by
	// auth.user_id).
	const KIND_LABELS: Record<string, string> = {
		start_bonus: 'Стартовый бонус',
		transfer_out: 'Перевод: отправлено',
		transfer_in: 'Перевод: получено',
		casino_bet: 'Казино: ставка',
		casino_payout: 'Казино: выигрыш',
		farm_convert: 'Ферма: CP → ¥',
		farm_buy_cp: 'Ферма: покупка CP',
		duel_stake: 'Дуэль: ставка',
		duel_payout: 'Дуэль: выигрыш',
		duel_refund: 'Дуэль: возврат',
		gacha_roll: 'Гача: крутка',
		gacha_refund: 'Гача: возврат звёзд',
		bet: 'Рынок: ставка',
		market_payout: 'Рынок: выплата',
		market_refund: 'Рынок: возврат',
		market_create_fee: 'Рынок: комиссия создания',
		market_import_fee: 'Рынок: комиссия импорта',
		market_cancel_refund: 'Рынок: возврат при отмене'
	};

	function kindLabel(row: HistoryRow): string {
		return KIND_LABELS[row.kind] ?? row.kind;
	}

	function formatTime(iso: string): string {
		const d = new Date(iso);
		const now = new Date();
		const sameDay = d.toDateString() === now.toDateString();
		if (sameDay) return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
		return d.toLocaleString('ru-RU', {
			day: '2-digit',
			month: '2-digit',
			hour: '2-digit',
			minute: '2-digit'
		});
	}

	let loading = $state(true);
	let error = $state<string | null>(null);
	let items = $state<HistoryRow[]>([]);

	onMount(async () => {
		try {
			items = await apiFetch<HistoryRow[]>('/api/v1/history?limit=50');
		} catch (err) {
			error = err instanceof ApiError ? `${err.status}: ${err.message}` : String(err ?? 'unknown_error');
		} finally {
			loading = false;
		}
	});
</script>

<div class="hi-screen">
	<div class="menu-head">
		<h1 class="menu-title">История</h1>
		<div class="menu-sub">все твои ставки, тапы и переводы</div>
	</div>

	{#if loading}
		<div class="screen-loading"><span>загрузка истории…</span></div>
	{:else if error}
		<div class="hi-error">{error}</div>
	{:else if items.length === 0}
		<div class="hi-empty">
			<div class="hi-empty-jp">からっぽ</div>
			<div class="hi-empty-ru">пусто</div>
			<div class="hi-empty-hint">Играй, переводи — здесь появится история.</div>
		</div>
	{:else}
		<div class="hist-list">
			{#each items as it (it.id)}
				<div class="hist-row">
					<div class="hist-meta">
						<span class="hist-kind">{kindLabel(it)}</span>
						<span class="hist-time">{formatTime(it.created_at)}</span>
					</div>
					<div class={`hist-amount ${it.amount >= 0 ? 'pos' : 'neg'}`}>
						{it.amount >= 0 ? `+${it.amount}` : it.amount}¥
					</div>
				</div>
			{/each}
		</div>
	{/if}
</div>

<style>
	.hi-screen {
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

	.hi-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.hi-empty {
		text-align: center;
		padding: var(--space-2xl) var(--space-lg);
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.hi-empty-jp {
		font-family: var(--font-jp);
		font-size: var(--font-heading-size);
		color: var(--text-locked);
	}
	.hi-empty-ru {
		font-family: var(--font-chrome);
		font-size: var(--font-display-size);
		font-weight: 700;
		color: var(--text-secondary);
		text-transform: lowercase;
	}
	.hi-empty-hint {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		font-family: var(--font-body);
		line-height: 1.5;
		max-width: 320px;
		margin: var(--space-xs) auto 0;
	}

	.hist-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.hist-row {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 12px;
		padding: var(--space-sm) var(--space-md);
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-sm);
		min-height: 44px;
	}
	.hist-meta {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.hist-kind {
		font-size: var(--font-body-size);
		color: var(--text-secondary);
		font-family: var(--font-body);
	}
	.hist-time {
		font-size: 11px;
		color: var(--text-muted);
		font-family: var(--font-body);
	}
	.hist-amount {
		font-family: var(--font-numeric);
		font-size: var(--font-heading-size);
		font-weight: 900;
		flex-shrink: 0;
	}
	.hist-amount.pos {
		color: var(--accent-pink);
	}
	.hist-amount.neg {
		color: var(--destructive-text);
	}
</style>
