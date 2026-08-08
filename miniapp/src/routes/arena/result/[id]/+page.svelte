<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';
	import ArenaFighter from '$lib/components/ArenaFighter.svelte';

	type Result = {
		id: number;
		status: string;
		result: 'win' | 'draw' | 'technical_loss' | null;
		reason: string | null;
		settlement_status: string;
		viewer_name: string | null;
		opponent_name: string | null;
		viewer_fighter: string;
		opponent_fighter: string | null;
		viewer_bet: number;
		opponent_bet: number;
		payout_amount: number;
		refund_amount: number;
		rating_delta: number;
		rating: number | null;
		xp_gained: number;
		finished_at: string | null;
	};

	const matchId = Number(page.params.id);
	let result = $state<Result | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);

	const isRefund = $derived(result?.settlement_status === 'refunded' && result?.result === null);
	const headline = $derived(
		isRefund ? 'ВОЗВРАТ' : result?.result === 'win' ? 'ПОБЕДА' : result?.result === 'draw' ? 'НИЧЬЯ' : 'ПОРАЖЕНИЕ'
	);
	const resultTone = $derived(
		isRefund ? 'refund' : result?.result === 'win' ? 'win' : result?.result === 'draw' ? 'draw' : 'loss'
	);

	function formatReason(reason: string | null): string {
		return ({ knockout: 'нокаут', time_limit: 'по итогам времени', disconnect_timeout: 'техническое поражение', forfeit: 'сдача', both_disconnected: 'оба игрока отключились', admin_refund: 'возврат администратором' } as Record<string, string>)[reason ?? ''] ?? 'матч завершён';
	}

	onMount(async () => {
		if (!Number.isInteger(matchId) || matchId <= 0) {
			error = 'Некорректный номер матча.';
			loading = false;
			return;
		}
		try {
			result = await apiFetch<Result>(`/api/v1/arena/matches/${matchId}/result`);
			if (result.result === 'win') haptic('big-win');
			else if (result.result === 'draw' || isRefund) haptic('tap');
			else haptic('lose');
		} catch (value) {
			error = value instanceof ApiError ? value.message : 'Не удалось загрузить результат.';
		} finally {
			loading = false;
		}
	});
</script>

<div class="result-screen">
	{#if loading}
		<div class="screen-loading"><span>считаем итог…</span></div>
	{:else if error}
		<div class="result-error" role="alert"><strong>Результат недоступен</strong><span>{error}</span><button type="button" class="result-button" onclick={() => goto('/arena')}>в арену</button></div>
	{:else if result}
		<div class="result-kicker">YUVI ARENA · MATCH #{result.id}</div>
		<div class={`result-hero ${resultTone}`}>
			<div class="result-burst">{resultTone === 'win' ? '✦' : resultTone === 'draw' ? '＝' : resultTone === 'refund' ? '↩' : '×'}</div>
			<h1>{headline}</h1>
			<p>{formatReason(result.reason)}</p>
		</div>

		<div class="result-stage" aria-hidden="true">
			<div class="result-stage-grid"></div>
			<div class="result-fighter result-fighter-player">
				<ArenaFighter fighter={result.viewer_fighter as 'tank' | 'assassin' | 'berserker' | 'tactician'} size="md" state={result.result === 'win' ? 'victory' : isRefund ? 'idle' : 'defeat'} />
				<span>{result.viewer_name ?? 'ты'}</span>
			</div>
			<div class="result-stage-vs">VS</div>
			<div class="result-fighter result-fighter-opponent">
				<ArenaFighter fighter={result.opponent_fighter as 'tank' | 'assassin' | 'berserker' | 'tactician' | null} size="md" side="opponent" state={result.result === 'win' ? 'defeat' : isRefund ? 'idle' : 'victory'} />
				<span>{result.opponent_name ?? 'соперник'}</span>
			</div>
		</div>
		<div class="versus-card">
			<div><span>{result.viewer_name ?? 'ты'}</span><strong>{result.viewer_fighter}</strong></div>
			<b>VS</b>
			<div class="right"><span>{result.opponent_name ?? 'соперник'}</span><strong>{result.opponent_fighter ?? '—'}</strong></div>
		</div>

		<div class="result-grid">
			<div class="result-stat primary"><span>{result.payout_amount > 0 ? 'Выплата' : result.refund_amount > 0 ? 'Возврат' : 'Итог'}</span><strong>{result.payout_amount > 0 ? `+${result.payout_amount}` : result.refund_amount > 0 ? `+${result.refund_amount}` : '0'}<small>¥</small></strong></div>
			<div class="result-stat"><span>Рейтинг</span><strong class:positive={result.rating_delta > 0} class:negative={result.rating_delta < 0}>{result.rating_delta > 0 ? '+' : ''}{result.rating_delta}</strong><small>{result.rating ?? '—'} после боя</small></div>
			<div class="result-stat"><span>Прогресс</span><strong>+{result.xp_gained}</strong><small>XP бойцу</small></div>
			<div class="result-stat"><span>Банк матча</span><strong>{(result.viewer_bet + result.opponent_bet).toLocaleString('ru-RU')}</strong><small>ювиков</small></div>
		</div>

		<div class="result-actions">
			<button type="button" class="result-button primary-button" onclick={() => goto('/arena')}>РЕВАНШ В АРЕНУ</button>
			<div class="secondary-actions"><button type="button" class="result-button ghost" onclick={() => goto('/arena/history')}>история боёв</button><button type="button" class="result-button ghost" onclick={() => goto('/arena/leaderboard')}>рейтинг</button></div>
		</div>
	{/if}
</div>

<style>
	.result-screen { padding: 24px 18px 40px; display: flex; flex-direction: column; gap: var(--space-md); }
	.result-kicker { color: var(--accent-yellow); font: 10px var(--font-body); letter-spacing: .14em; text-transform: uppercase; text-align: center; }
	.result-hero { position: relative; overflow: hidden; padding: 34px 20px 28px; border-radius: 18px; text-align: center; background: var(--bg-secondary-2); border: 1px solid var(--border-secondary); }
	.result-hero::after { content: ''; position: absolute; inset: auto -15% -60%; height: 160px; border-radius: 50%; background: currentColor; opacity: .08; filter: blur(20px); }
	.result-hero.win { color: var(--accent-yellow); border-color: rgba(255,216,74,.5); }
	.result-hero.draw { color: var(--accent-cyan); border-color: rgba(123,230,255,.5); }
	.result-hero.loss { color: var(--accent-pink); border-color: rgba(255,91,141,.5); }
	.result-hero.refund { color: var(--accent-cyan); border-color: rgba(123,230,255,.5); }
	.result-burst { font: 42px var(--font-numeric); line-height: 1; opacity: .85; }
	.result-hero h1 { margin: 8px 0 4px; color: var(--text-primary); font: 700 42px var(--font-numeric); letter-spacing: .04em; }
	.result-hero p { margin: 0; color: var(--text-muted); font: 13px var(--font-body); }
	.result-stage { position: relative; min-height: 172px; display: flex; align-items: flex-end; justify-content: space-around; overflow: hidden; padding: 8px 8px 4px; border: 1px solid var(--border-secondary); border-radius: 16px; background: radial-gradient(ellipse at 50% 90%, rgba(255,216,74,.12), transparent 48%), linear-gradient(180deg, #171326, #0b0914); }
	.result-stage-grid { position: absolute; inset: 42% 0 0; opacity: .28; background: linear-gradient(rgba(255,216,74,.22) 1px, transparent 1px), linear-gradient(90deg, rgba(255,216,74,.22) 1px, transparent 1px); background-size: 26px 18px; transform: perspective(120px) rotateX(58deg) scale(1.35); transform-origin: bottom; mask-image: linear-gradient(to top, black, transparent); }
	.result-fighter { position: relative; z-index: 1; display: flex; flex-direction: column; align-items: center; gap: 0; color: var(--text-muted); font: 10px var(--font-body); text-transform: uppercase; }
	.result-fighter-player { color: var(--accent-cyan); }
	.result-fighter-opponent { color: var(--accent-pink); }
	.result-stage-vs { position: absolute; left: 50%; top: 48%; z-index: 2; color: var(--accent-yellow); font: 22px var(--font-numeric); transform: translate(-50%, -50%) rotate(-8deg); text-shadow: 0 0 14px rgba(255,216,74,.75); }
	.versus-card { display: grid; grid-template-columns: 1fr auto 1fr; gap: var(--space-sm); align-items: center; padding: var(--space-md); background: var(--bg-secondary-2); border: 1px solid var(--border-secondary); border-radius: 14px; }
	.versus-card div { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
	.versus-card .right { text-align: right; align-items: flex-end; }
	.versus-card span { color: var(--text-muted); font: 11px var(--font-body); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.versus-card strong { color: var(--text-primary); font: 16px var(--font-chrome); }
	.versus-card b { color: var(--accent-yellow); font: 17px var(--font-numeric); }
	.result-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-sm); }
	.result-stat { min-height: 100px; padding: 13px; display: flex; flex-direction: column; justify-content: space-between; gap: 7px; background: var(--bg-secondary-2); border: 1px solid var(--border-secondary); border-radius: 12px; }
	.result-stat span, .result-stat small { color: var(--text-muted); font: 11px var(--font-body); }
	.result-stat strong { color: var(--text-primary); font: 27px var(--font-numeric); }
	.result-stat strong small { color: var(--accent-pink); font: 13px var(--font-body); margin-left: 2px; }
	.result-stat.primary { grid-column: 1 / -1; border-color: rgba(255,216,74,.4); background: linear-gradient(135deg, rgba(255,216,74,.1), var(--bg-secondary-2)); }
	.result-stat.primary strong { color: var(--accent-yellow); font-size: 42px; }
	.positive { color: var(--positive-text) !important; } .negative { color: var(--destructive-text) !important; }
	.result-actions { display: flex; flex-direction: column; gap: var(--space-sm); margin-top: var(--space-sm); }
	.result-button { min-height: 48px; padding: 10px 16px; border: 0; border-radius: 10px; cursor: pointer; font: 700 14px var(--font-chrome); }
	.primary-button { color: #17120a; background: var(--accent-yellow); box-shadow: 3px 3px 0 #111; }
	.ghost { flex: 1; color: var(--text-muted); background: var(--bg-secondary-2); border: 1px solid var(--border-secondary); font: 12px var(--font-body); }
	.secondary-actions { display: flex; gap: var(--space-sm); }
	.result-error { min-height: 50vh; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: var(--space-sm); color: var(--destructive-text); font: 13px var(--font-body); text-align: center; }
	@media (max-width: 360px) { .result-hero h1 { font-size: 34px; } .result-stage { min-height: 150px; } }
	@media (prefers-reduced-motion: reduce) { .result-stage :global(.fighter-aura), .result-stage :global(.fighter-ring) { animation: none; } }
</style>
