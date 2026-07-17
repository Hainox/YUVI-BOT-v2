<script lang="ts">
	// Admin analytics — destination of the admin sub-hub's "Аналитика" tile
	// (CASINO-03, D-03). Composes GET /api/v1/admin/summary (existing
	// economy_service.get_chat_summary) + GET /api/v1/admin/analytics
	// (admin_analytics_service: game popularity / turnover / DAU) — no
	// client-side computation beyond merging the fixed game-type list with
	// whatever admin_analytics_service.get_game_popularity actually returned
	// (games with 0 rounds in the period are omitted by the backend's
	// GROUP BY, but 04.3-UI-SPEC.md requires them to render as `—` rows
	// rather than being hidden).
	//
	// Both routes are gated by require_admin server-side (T-04.3-05) — a
	// non-admin reaching this screen directly gets 403 on both calls, shown
	// here as the benign "Только для админов" state (04.3-UI-SPEC.md
	// Copywriting Contract), never a generic connection-error screen.
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';

	type Summary = {
		bank_balance: number;
		total_in_circulation: number;
		open_markets_count: number;
	};

	type GamePopularity = { game: string; rounds: number };
	type Turnover = { bets_placed: number; bank_commission: number };
	type ActivePlayers = { day: string; active_players: number };
	type Analytics = {
		game_popularity: GamePopularity[];
		turnover: Turnover;
		active_players: ActivePlayers[];
	};

	type Period = '24h' | '7d' | '30d';

	const PERIOD_CHIPS: { value: Period; label: string }[] = [
		{ value: '24h', label: '24ч' },
		{ value: '7d', label: '7д' },
		{ value: '30d', label: '30д' }
	];

	// Same game-type taxonomy as stats/+page.svelte's GAME_LABELS.
	const ALL_GAMES = ['slots', 'roulette', 'blackjack', 'dice', 'coinflip'];
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

	function mergeGamePopularity(rows: GamePopularity[]): GamePopularity[] {
		const counts = new Map(rows.map((row) => [row.game, row.rounds]));
		return ALL_GAMES.map((game) => ({ game, rounds: counts.get(game) ?? 0 })).sort(
			(a, b) => b.rounds - a.rounds
		);
	}

	function formatDay(iso: string): string {
		const d = new Date(iso);
		if (Number.isNaN(d.getTime())) return iso;
		return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' });
	}

	let loading = $state(true);
	let error = $state<string | null>(null);
	let forbidden = $state(false);
	let summary = $state<Summary | null>(null);
	let analytics = $state<Analytics | null>(null);
	let period = $state<Period>('7d');
	let analyticsLoading = $state(false);

	let rankedGames = $derived(analytics ? mergeGamePopularity(analytics.game_popularity) : []);

	function reportError(err: unknown): void {
		if (err instanceof ApiError && err.status === 403) {
			forbidden = true;
			return;
		}
		error = err instanceof ApiError ? `${err.status}: ${err.message}` : String(err ?? 'unknown_error');
	}

	async function loadAnalytics() {
		analyticsLoading = true;
		try {
			analytics = await apiFetch<Analytics>(`/api/v1/admin/analytics?period=${period}`);
		} catch (err) {
			reportError(err);
		} finally {
			analyticsLoading = false;
		}
	}

	function selectPeriod(value: Period) {
		if (value === period || analyticsLoading) return;
		period = value;
		loadAnalytics();
	}

	onMount(async () => {
		try {
			summary = await apiFetch<Summary>('/api/v1/admin/summary');
			await loadAnalytics();
		} catch (err) {
			reportError(err);
		} finally {
			loading = false;
		}
	});
</script>

<div class="ad-screen">
	<div class="menu-head">
		<h1 class="menu-title">Аналитика</h1>
		<div class="menu-sub">банк чата, популярность игр, обороты, DAU</div>
	</div>

	{#if loading}
		<div class="screen-loading"><span>загрузка аналитики…</span></div>
	{:else if forbidden}
		<div class="ad-forbidden">
			<h2>Только для админов</h2>
			<div class="ad-forbidden-body">У тебя нет прав администратора в этом чате.</div>
		</div>
	{:else if error}
		<div class="ad-error">{error}</div>
	{:else if summary}
		<div class="balance-card ad-bank">
			<div class="bc-handle">банк чата</div>
			<div class="bc-amount">
				<span class="bc-val">{summary.bank_balance.toLocaleString('ru-RU')}</span>
				<span class="bc-unit">¥ юви</span>
			</div>
			<div class="bc-bank">
				в обороте: <strong>{summary.total_in_circulation.toLocaleString('ru-RU')}¥</strong>
			</div>
		</div>

		<div class="ad-period">
			{#each PERIOD_CHIPS as chipOption (chipOption.value)}
				<button
					type="button"
					class={`chip ${period === chipOption.value ? 'chip-on' : ''}`}
					disabled={analyticsLoading}
					onclick={() => selectPeriod(chipOption.value)}
				>
					{chipOption.label}
				</button>
			{/each}
		</div>

		<div class="st-card">
			<div class="st-card-title">Баланс и банк</div>
			<div class="st-row">
				<span class="st-label">банк чата</span>
				<span class="st-value st-value-sm">
					{summary.bank_balance ? `${summary.bank_balance.toLocaleString('ru-RU')}¥` : '—'}
				</span>
			</div>
			<div class="st-row">
				<span class="st-label">в обороте</span>
				<span class="st-value st-value-sm">
					{summary.total_in_circulation
						? `${summary.total_in_circulation.toLocaleString('ru-RU')}¥`
						: '—'}
				</span>
			</div>
		</div>

		{#if analyticsLoading && !analytics}
			<div class="screen-loading"><span>загрузка периода…</span></div>
		{:else if analytics}
			<div class="st-card">
				<div class="st-card-title">Популярность игр</div>
				<div class="ad-lb-list">
					{#each rankedGames as row, i (row.game)}
						<div class="lb-row">
							<span class={`lb-rank rank-${Math.min(i + 1, 4)}`}>{i + 1}</span>
							<span class="lb-name">{gameLabel(row.game)}</span>
							<span class="lb-balance">
								{row.rounds > 0 ? row.rounds : '—'}
								{#if row.rounds > 0}<small>раунд.</small>{/if}
							</span>
						</div>
					{/each}
				</div>
			</div>

			<div class="st-card">
				<div class="st-card-title">Оборот ювиков</div>
				<div class="st-row">
					<span class="st-label">поставлено за период</span>
					<span class="st-value st-value-sm">
						{analytics.turnover.bets_placed
							? `${analytics.turnover.bets_placed.toLocaleString('ru-RU')}¥`
							: '—'}
					</span>
				</div>
				<div class="st-row">
					<span class="st-label">комиссия банка</span>
					<span class="st-value st-value-sm">
						{analytics.turnover.bank_commission
							? `${analytics.turnover.bank_commission.toLocaleString('ru-RU')}¥`
							: '—'}
					</span>
				</div>
			</div>

			<div class="st-card">
				<div class="st-card-title">Активные игроки</div>
				{#if analytics.active_players.length === 0}
					<div class="st-row">
						<span class="st-label">за период</span>
						<span class="st-value st-value-sm">—</span>
					</div>
				{:else}
					{#each analytics.active_players as row (row.day)}
						<div class="st-row">
							<span class="st-label">{formatDay(row.day)}</span>
							<span class="st-value st-value-sm">{row.active_players}</span>
						</div>
					{/each}
				{/if}
			</div>
		{/if}
	{/if}
</div>

<style>
	.ad-screen {
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

	.ad-bank {
		margin: 0;
	}

	.ad-period {
		display: flex;
		gap: var(--space-sm);
	}
	.ad-period .chip {
		flex: 1;
		text-align: center;
	}

	.ad-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.ad-forbidden {
		text-align: center;
		padding: var(--space-2xl) var(--space-lg);
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.ad-forbidden h2 {
		font-family: var(--font-chrome);
		font-size: var(--font-display-size);
		font-weight: 700;
		color: var(--text-secondary);
		margin: 0;
	}
	.ad-forbidden-body {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		font-family: var(--font-body);
		line-height: 1.5;
		max-width: 320px;
		margin: 0 auto;
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

	.ad-lb-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.lb-row {
		background: var(--bg-secondary-1);
		border: 1px solid var(--border-secondary);
		border-radius: 12px;
		padding: var(--space-sm) var(--space-md);
		display: flex;
		align-items: center;
		gap: var(--space-sm);
		min-height: 44px;
	}
	.lb-rank {
		font-family: var(--font-numeric);
		font-size: var(--font-heading-size);
		font-weight: 900;
		color: var(--text-muted);
		min-width: 22px;
		text-align: center;
	}
	.lb-rank.rank-1 {
		color: var(--accent-yellow);
	}
	.lb-name {
		flex: 1;
		font-size: var(--font-body-size);
		color: var(--text-secondary);
		font-family: var(--font-body);
	}
	.lb-balance {
		font-family: var(--font-numeric);
		font-size: var(--font-heading-size);
		font-weight: 900;
		color: var(--text-primary);
		flex-shrink: 0;
		display: flex;
		align-items: baseline;
		gap: 4px;
	}
	.lb-balance small {
		font-size: 11px;
		font-family: var(--font-body);
		color: var(--text-muted);
	}
</style>
