<script lang="ts">
	// Statistics dashboard — destination of the "Статистика" hub tile (D-05).
	// A personal read-only summary composing GET /api/v1/stats (which itself
	// composes existing economy_service/stats_service/clicker_service reads
	// — see api/routes/stats.py docstring). NOT a duplicate of Лидерборд
	// (chat-wide ranking) or История (raw transaction feed): this screen
	// is a single-user rollup across balance, casino, chat activity, farm.
	//
	// 04.2-UI-SPEC.md §"Statistics dashboard (D-05)": blue-gradient
	// balance-card header (reused, same `.balance-card`/`.bc-*` primitives as
	// the hub/portfolio headers), then 4 stacked card sections using the
	// same `bg-secondary-2` card surface as every other screen. Display-tier
	// (32px) Anton numerics for headline figures — NOT Hero (64px), this is
	// a routine reference screen, no jackpot theater. Missing fields render
	// `—` (em dash) instead of a full empty-state screen (dashboard layout
	// stays stable for brand-new users).
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';

	type BiggestWin = { amount: number; game: string } | null;

	type CasinoStats = {
		rounds_played: number;
		net_result: number;
		biggest_win: BiggestWin;
	};

	type PeakDay = { date: string; message_count: number } | null;

	type ActivityStats = {
		streak: number;
		peak_day: PeakDay;
		message_rank: number | null;
	};

	type FarmStats = {
		cp_per_sec: number;
		total_converted: number;
	};

	type StatsDashboard = {
		balance: number;
		bank_share_pct: number | null;
		casino: CasinoStats;
		activity: ActivityStats;
		farm: FarmStats;
	};

	const GAME_LABELS: Record<string, string> = {
		coinflip: 'Монетка',
		dice: 'Кости',
		roulette: 'Рулетка',
		blackjack: 'Блэкджек',
		slots: 'Слот'
	};

	function gameLabel(game: string): string {
		return GAME_LABELS[game] ?? game;
	}

	function formatSigned(amount: number): string {
		return amount >= 0 ? `+${amount.toLocaleString('ru-RU')}` : amount.toLocaleString('ru-RU');
	}

	function formatDate(iso: string): string {
		const d = new Date(iso);
		return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });
	}

	function formatPct(pct: number): string {
		return `${pct.toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%`;
	}

	let loading = $state(true);
	let error = $state<string | null>(null);
	let stats = $state<StatsDashboard | null>(null);

	onMount(async () => {
		try {
			stats = await apiFetch<StatsDashboard>('/api/v1/stats');
		} catch (err) {
			error = err instanceof ApiError ? `${err.status}: ${err.message}` : String(err ?? 'unknown_error');
		} finally {
			loading = false;
		}
	});
</script>

<div class="st-screen">
	<div class="menu-head">
		<h1 class="menu-title">Статистика</h1>
		<div class="menu-sub">баланс, стрик, большие выигрыши</div>
	</div>

	{#if loading}
		<div class="screen-loading"><span>загрузка статистики…</span></div>
	{:else if error}
		<div class="st-error">{error}</div>
	{:else if stats}
		<div class="balance-card st-bank">
			<div class="bc-handle">баланс и банк</div>
			<div class="bc-amount">
				<span class="bc-val">{stats.balance.toLocaleString('ru-RU')}</span>
				<span class="bc-unit">¥ юви</span>
			</div>
			<div class="bc-bank">
				доля в банке чата:
				<strong>{stats.bank_share_pct != null ? formatPct(stats.bank_share_pct) : '—'}</strong>
			</div>
		</div>

		<div class="st-card">
			<div class="st-card-title">Игровая статистика</div>
			<div class="st-row">
				<span class="st-label">раундов сыграно</span>
				<span class="st-value st-value-sm">{stats.casino.rounds_played || '—'}</span>
			</div>
			<div class="st-row">
				<span class="st-label">чистый выигрыш/проигрыш</span>
				<span
					class={`st-value ${stats.casino.net_result > 0 ? 'st-pos' : stats.casino.net_result < 0 ? 'st-neg' : ''}`}
				>
					{stats.casino.rounds_played > 0 ? `${formatSigned(stats.casino.net_result)}¥` : '—'}
				</span>
			</div>
			<div class="st-row">
				<span class="st-label">самый большой выигрыш</span>
				{#if stats.casino.biggest_win}
					<span class="st-value st-badge-row">
						+{stats.casino.biggest_win.amount.toLocaleString('ru-RU')}¥
						<span class="st-sub">{gameLabel(stats.casino.biggest_win.game)}</span>
						<span class="st-badge">личный рекорд</span>
					</span>
				{:else}
					<span class="st-value st-value-sm">—</span>
				{/if}
			</div>
		</div>

		<div class="st-card">
			<div class="st-card-title">Активность чата</div>
			<div class="st-row">
				<span class="st-label">стрик</span>
				<span class="st-value">
					{#if stats.activity.streak > 0}
						{stats.activity.streak}
						<span class="st-sub">дней подряд</span>
						{#if stats.activity.streak >= 7}<span class="st-badge">личный рекорд</span>{/if}
					{:else}
						—
					{/if}
				</span>
			</div>
			<div class="st-row">
				<span class="st-label">пиковый день чата</span>
				<span class="st-value st-value-sm">
					{#if stats.activity.peak_day}
						{formatDate(stats.activity.peak_day.date)} · {stats.activity.peak_day.message_count} сообщ.
					{:else}
						—
					{/if}
				</span>
			</div>
			<div class="st-row">
				<span class="st-label">место в топе по сообщениям</span>
				<span class="st-value st-value-sm">
					{stats.activity.message_rank != null ? `#${stats.activity.message_rank}` : '—'}
				</span>
			</div>
		</div>

		<div class="st-card">
			<div class="st-card-title">Ферма</div>
			<div class="st-row">
				<span class="st-label">доход в секунду</span>
				<span class="st-value">
					{stats.farm.cp_per_sec > 0 ? stats.farm.cp_per_sec.toLocaleString('ru-RU') : '—'}
					<span class="st-sub">CP/сек</span>
				</span>
			</div>
			<div class="st-row">
				<span class="st-label">всего конвертировано</span>
				<span class="st-value st-value-sm">
					{stats.farm.total_converted > 0 ? `${stats.farm.total_converted.toLocaleString('ru-RU')}¥` : '—'}
				</span>
			</div>
		</div>
	{/if}
</div>

<style>
	.st-screen {
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

	.st-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.st-bank {
		margin: 0;
	}

	.st-card {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-md);
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.st-card-title {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		color: var(--text-primary);
	}
	.st-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-sm);
	}
	.st-label {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		font-family: var(--font-body);
	}
	.st-value {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		color: var(--text-primary);
		display: flex;
		align-items: baseline;
		gap: 6px;
		flex-shrink: 0;
	}
	.st-value-sm {
		font-size: var(--font-heading-size);
	}
	.st-pos {
		color: var(--accent-pink);
	}
	.st-neg {
		color: var(--destructive-text);
	}
	.st-sub {
		font-family: var(--font-body);
		font-size: 11px;
		color: var(--text-muted);
		font-weight: 400;
	}
	.st-badge-row {
		flex-wrap: wrap;
		justify-content: flex-end;
	}
	.st-badge {
		font-family: var(--font-body);
		background: var(--accent-yellow);
		color: #1a0f12;
		font-size: 10px;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 2px 6px;
		border-radius: 20px;
	}
</style>
