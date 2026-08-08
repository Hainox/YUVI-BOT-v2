<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { apiFetch, ApiError } from '$lib/api';

	type Match = { id: number; result: 'win' | 'draw' | 'technical_loss' | null; reason: string | null; settlement_status: string; opponent_name: string | null; viewer_fighter: string; opponent_fighter: string | null; payout_amount: number; refund_amount: number; rating_delta: number; finished_at: string | null };
	let items = $state<Match[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	function isRefund(item: Match): boolean { return item.settlement_status === 'refunded' && item.result === null; }
	function label(result: Match['result'], refunded: boolean): string { return refunded ? 'Возврат' : result === 'win' ? 'Победа' : result === 'draw' ? 'Ничья' : 'Поражение'; }
	function formatTime(value: string | null): string { return value ? new Date(value).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : 'дата неизвестна'; }

	onMount(async () => {
		try { items = await apiFetch<Match[]>('/api/v1/arena/history?limit=50'); }
		catch (value) { error = value instanceof ApiError ? value.message : 'Не удалось загрузить историю Arena.'; }
		finally { loading = false; }
	});
</script>

<div class="arena-history">
	<div class="history-head"><div><div class="kicker">YUVI ARENA · АРХИВ</div><h1>История боёв</h1><p>Твои завершённые матчи, рейтинг и выплаты</p></div><div class="history-mark">⚔</div></div>
	{#if loading}<div class="screen-loading"><span>загрузка истории…</span></div>
	{:else if error}<div class="history-error" role="alert">{error}</div>
	{:else if items.length === 0}<div class="history-empty"><div>⚔</div><strong>Пока без боёв</strong><span>Открой матч в Arena — результат появится здесь.</span><button type="button" class="history-button" onclick={() => goto('/arena')}>ОТКРЫТЬ ARENA</button></div>
	{:else}<div class="history-list">{#each items as item (item.id)}{@const refunded = isRefund(item)}<button type="button" class="history-row" onclick={() => goto(`/arena/result/${item.id}`)}><span class={`history-result ${refunded ? 'refund' : item.result === 'win' ? 'win' : item.result === 'draw' ? 'draw' : 'loss'}`}>{refunded ? '↩' : item.result === 'win' ? '✦' : item.result === 'draw' ? '＝' : '×'}</span><span class="history-main"><strong>{label(item.result, refunded)} · #{item.id}</strong><small>против {item.opponent_name ?? 'соперника'} · {formatTime(item.finished_at)}</small></span><span class="history-value">{item.payout_amount > 0 ? `+${item.payout_amount}¥` : item.refund_amount > 0 ? `возврат ${item.refund_amount}¥` : `${item.rating_delta > 0 ? '+' : ''}${item.rating_delta} рейтинга`}</span><span class="history-chevron">›</span></button>{/each}</div>{/if}
</div>

<style>
	.arena-history { padding: 24px 18px 40px; display: flex; flex-direction: column; gap: var(--space-md); }
	.history-head { display: flex; justify-content: space-between; gap: var(--space-md); }
	.kicker { color: var(--accent-yellow); font: 10px var(--font-body); letter-spacing: .14em; text-transform: uppercase; }
	h1 { margin: 3px 0 0; color: var(--text-primary); font: 700 var(--font-display-size) var(--font-chrome); }
	.history-head p { margin: 5px 0 0; color: var(--text-muted); font: 13px var(--font-body); }
	.history-mark { width: 54px; height: 54px; display: grid; place-items: center; color: var(--accent-pink); border: 2px solid var(--accent-pink); border-radius: 50%; font-size: 25px; transform: rotate(-12deg); }
	.history-list { display: flex; flex-direction: column; gap: var(--space-sm); }
	.history-row { position: relative; display: grid; grid-template-columns: 36px 1fr auto 14px; align-items: center; gap: var(--space-sm); min-height: 72px; padding: 11px 12px; color: inherit; background: var(--bg-secondary-2); border: 1px solid var(--border-secondary); border-radius: 12px; text-align: left; cursor: pointer; }
	.history-row:hover { border-color: var(--accent-cyan); background: #221d30; }
	.history-result { width: 32px; height: 32px; display: grid; place-items: center; border-radius: 50%; font: 20px var(--font-numeric); }
	.history-result.win { color: var(--accent-yellow); background: rgba(255,216,74,.12); } .history-result.draw, .history-result.refund { color: var(--accent-cyan); background: rgba(123,230,255,.12); } .history-result.loss { color: var(--accent-pink); background: rgba(255,91,141,.12); }
	.history-main { min-width: 0; display: flex; flex-direction: column; gap: 4px; } .history-main strong { color: var(--text-primary); font: 13px var(--font-chrome); } .history-main small { overflow: hidden; color: var(--text-muted); font: 11px var(--font-body); text-overflow: ellipsis; white-space: nowrap; }
	.history-value { color: var(--accent-yellow); font: 13px var(--font-numeric); text-align: right; white-space: nowrap; } .history-chevron { color: var(--text-muted); font-size: 22px; }
	.history-empty { padding: 55px 20px; display: flex; flex-direction: column; align-items: center; gap: 8px; color: var(--text-muted); background: var(--bg-secondary-2); border: 1px dashed var(--border-secondary); border-radius: 14px; text-align: center; font: 13px var(--font-body); } .history-empty > div { color: var(--accent-yellow); font-size: 36px; } .history-empty strong { color: var(--text-primary); font: 17px var(--font-chrome); } .history-button { margin-top: 10px; min-height: 44px; padding: 8px 16px; color: #17120a; background: var(--accent-yellow); border: 0; border-radius: 8px; font: 700 12px var(--font-chrome); cursor: pointer; }
	.history-error { padding: 12px 16px; color: var(--destructive-text); background: var(--destructive-bg); border-radius: 8px; font: 13px var(--font-body); }
</style>
