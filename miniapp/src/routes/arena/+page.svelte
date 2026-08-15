<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';
	import ArenaFighter from '$lib/components/ArenaFighter.svelte';

	type FighterType = 'tank' | 'assassin' | 'berserker' | 'tactician';
	type ArenaMatch = {
		id: number;
		status: 'waiting' | 'accepting' | 'active' | 'finished' | 'cancelled' | 'refunded' | string;
		creator_id: number;
		creator_bet: number;
		opponent_id: number | null;
		opponent_bet: number | null;
		expires_at: string;
		accept_deadline: string | null;
		creator_fighter?: FighterType | null;
		opponent_fighter?: FighterType | null;
		creator_confirmed?: boolean;
		opponent_confirmed?: boolean;
		started_at?: string | null;
		user_balance_after?: number;
	};

	type Fighter = {
		id: FighterType;
		name: string;
		role: string;
		stats: string;
		special: string;
		accent: 'pink' | 'cyan' | 'yellow';
	};

	const fighters: Fighter[] = [
		{ id: 'tank', name: 'Танк', role: 'выносливость и защита', stats: '130 HP · 20 тяжёлый удар', special: 'Крепость', accent: 'cyan' },
		{ id: 'assassin', name: 'Ассасин', role: 'скорость и уклонение', stats: '80 HP · 30 спецприём', special: 'Молниеносный рывок', accent: 'pink' },
		{ id: 'berserker', name: 'Берсерк', role: 'риск и урон на низком HP', stats: '105 HP · 24 тяжёлый удар', special: 'Ярость', accent: 'yellow' },
		{ id: 'tactician', name: 'Тактик', role: 'контроль и эффекты', stats: '95 HP · тактическая ловушка', special: 'Тактическая ловушка', accent: 'cyan' }
	];

	let matches = $state<ArenaMatch[]>([]);
	let myUserId = $state<number | null>(null);
	let activeMatch = $state<ArenaMatch | null>(null);
	let selectedFighter = $state<FighterType>('tank');
	let bet = $state(100);
	let acceptFighter = $state<FighterType>('assassin');
	let acceptBet = $state(100);
	let acceptingMatchId = $state<number | null>(null);
	let loading = $state(true);
	let busy = $state(false);
	let refreshSequence = 0;
	let error = $state<string | null>(null);
	let notice = $state<string | null>(null);

	function messageFrom(errorValue: unknown): string {
		if (errorValue instanceof ApiError) {
			if (errorValue.status === 400) return 'Недостаточно ювиков для этой ставки.';
			if (errorValue.status === 409) return 'Матч уже занят или изменился. Обновляю список.';
			if (errorValue.status === 422) return errorValue.message;
			return errorValue.message || 'Не удалось выполнить запрос.';
		}
		return 'Не удалось связаться с Arena.';
	}

	function idempotencyKey(prefix: string): string {
		return `${prefix}:${crypto.randomUUID()}`;
	}

	async function loadMatches() {
		const requestSequence = ++refreshSequence;
		try {
			const [loaded, own] = await Promise.all([
				apiFetch<ArenaMatch[]>('/api/v1/arena/matches'),
				apiFetch<ArenaMatch[]>('/api/v1/arena/my-matches')
			]);
			if (requestSequence !== refreshSequence) return;
			activeMatch = own[0] ?? null;
			matches = loaded.filter((match) => !own.some((mine) => mine.id === match.id));
			error = null;
		} catch (err) {
			if (requestSequence === refreshSequence) error = messageFrom(err);
		} finally {
			if (requestSequence === refreshSequence) loading = false;
		}
	}

	async function createMatch() {
		if (busy) return;
		if (!Number.isInteger(bet) || bet < 100) {
			error = 'Минимальная ставка — 100 ювиков.';
			return;
		}
		busy = true;
		error = null;
		notice = null;
		try {
			const created = await apiFetch<ArenaMatch>('/api/v1/arena/matches', {
				method: 'POST',
				body: JSON.stringify({
					fighter: selectedFighter,
					bet,
					idempotency_key: idempotencyKey('create')
				})
			});
			activeMatch = created;
			notice = `Матч #${created.id} открыт. Ждём соперника.`;
			haptic('win');
			await loadMatches();
		} catch (err) {
			error = messageFrom(err);
			haptic('error');
		} finally {
			busy = false;
		}
	}

	async function acceptMatch(match: ArenaMatch) {
		if (busy) return;
		if (!Number.isInteger(acceptBet) || acceptBet < 100) {
			error = 'Минимальная ставка — 100 ювиков.';
			return;
		}
		busy = true;
		acceptingMatchId = match.id;
		error = null;
		notice = null;
		try {
			const accepted = await apiFetch<ArenaMatch>(`/api/v1/arena/matches/${match.id}/accept`, {
				method: 'POST',
				body: JSON.stringify({
					fighter: acceptFighter,
					bet: acceptBet,
					idempotency_key: idempotencyKey(`accept-${match.id}`)
				})
			});
			activeMatch = accepted;
			notice = `Матч #${match.id} принят. Подтверди участие, чтобы начать бой.`;
			haptic('win');
			acceptingMatchId = null;
			await loadMatches();
		} catch (err) {
			error = messageFrom(err);
			haptic('error');
			if (err instanceof ApiError && err.status === 409) await loadMatches();
		} finally {
			busy = false;
			acceptingMatchId = null;
		}
	}

	function formatExpiry(value: string): string {
		const remaining = new Date(value).getTime() - Date.now();
		if (remaining <= 0) return 'истёк';
		const minutes = Math.floor(remaining / 60000);
		const seconds = Math.floor((remaining % 60000) / 1000);
		return minutes > 0 ? `ещё ${minutes} мин` : `ещё ${seconds} сек`;
	}

	function fighterName(id: FighterType | null | undefined): string {
		return fighters.find((fighter) => fighter.id === id)?.name ?? 'скрыт';
	}

	async function confirmActiveMatch() {
		if (!activeMatch || busy) return;
		busy = true;
		error = null;
		try {
			activeMatch = await apiFetch<ArenaMatch>(`/api/v1/arena/matches/${activeMatch.id}/confirm`, {
				method: 'POST'
			});
			notice = activeMatch.status === 'active'
				? `Матч #${activeMatch.id} начался. Переходим в боевой экран.`
				: 'Подтверждение принято. Ждём второго игрока.';
			haptic('win');
		} catch (err) {
			error = messageFrom(err);
			haptic('error');
		} finally {
			busy = false;
		}
	}

	async function cancelActiveMatch() {
		if (!activeMatch || busy) return;
		busy = true;
		error = null;
		try {
			await apiFetch<ArenaMatch>(`/api/v1/arena/matches/${activeMatch.id}/cancel`, { method: 'POST' });
			activeMatch = null;
			notice = 'Матч отменён, ставка возвращена.';
			haptic('tap');
			await loadMatches();
		} catch (err) {
			error = messageFrom(err);
			haptic('error');
		} finally {
			busy = false;
		}
	}

	onMount(() => {
		void (async () => {
			try {
				const me = await apiFetch<{ user_id: number }>('/api/v1/me');
				myUserId = me.user_id;
			} catch {
				// The layout already owns the fatal auth screen; lobby can still render.
			}
			await loadMatches();
		})();

		// The lobby endpoint only returns waiting matches. Refreshing both feeds
		// lets the creator discover waiting → accepting after the opponent joins,
		// and recovers the own match after Telegram suspends/reloads the WebView.
		const refreshTimer = window.setInterval(() => {
			if (!busy) void loadMatches();
		}, 10_000);
		return () => window.clearInterval(refreshTimer);
	});
</script>

<div class="arena-screen">
	<div class="arena-head">
		<div>
			<div class="arena-kicker">YUVI ARENA · PHASE 4</div>
			<h1 class="menu-title">Арена</h1>
			<div class="menu-sub">сервер решает · ты выбираешь удар</div>
		</div>
		<div class="arena-mark" aria-hidden="true">VS</div>
	</div>

	<div class="arena-intro">
		<div class="arena-intro-title">90 секунд до победы</div>
		<div class="arena-intro-copy">
			Выбери бойца и поставь минимум 100 ювиков. Во время боя решения принимает сервер — без
			подкруток клиента.
		</div>
	</div>

	<div class="arena-links">
		<button type="button" class="arena-link" onclick={() => goto('/arena/profile')}>ПРОФИЛЬ <span>рейтинг и XP</span></button>
		<button type="button" class="arena-link" onclick={() => goto('/arena/history')}>ИСТОРИЯ <span>завершённые бои</span></button>
		<button type="button" class="arena-link" onclick={() => goto('/arena/leaderboard')}>ТОП <span>кто здесь босс</span></button>
	</div>

	<button type="button" class="training-link" onclick={() => goto('/arena/training')}>
		<span class="training-link-title">Тренировка против AI</span>
		<span class="training-link-sub">без ставок, без рейтинга — потренируй все 4 стиля боя</span>
	</button>

	<section class="arena-panel" aria-labelledby="create-title">
		<div class="section-heading">
			<div>
				<div class="section-kicker">новый матч</div>
				<h2 id="create-title">Выбери бойца</h2>
			</div>
			<div class="minimum-bet">от 100¥</div>
		</div>

		<div class="fighter-grid">
			{#each fighters as fighter (fighter.id)}
				<button
					type="button"
					class={`fighter-card fc-${fighter.accent} ${selectedFighter === fighter.id ? 'fighter-selected' : ''}`}
					aria-pressed={selectedFighter === fighter.id}
					onclick={() => (selectedFighter = fighter.id)}
				>
					<ArenaFighter fighter={fighter.id} size="sm" state={selectedFighter === fighter.id ? 'selected' : 'idle'} />
					<span class="fighter-copy">
						<span class="fighter-name">{fighter.name}</span>
						<span class="fighter-role">{fighter.role}</span>
						<span class="fighter-stats">{fighter.stats}</span>
						<span class="fighter-special">✦ {fighter.special}</span>
					</span>
				</button>
			{/each}
		</div>

		<div class="bet-row">
			<label for="create-bet">Ставка</label>
			<div class="bet-input-wrap">
				<input id="create-bet" type="number" min="100" step="50" inputmode="numeric" bind:value={bet} disabled={busy} />
				<span>¥</span>
			</div>
		</div>
		<button type="button" class="arena-cta" disabled={busy} onclick={createMatch}>
			{busy && acceptingMatchId === null ? 'ОТКРЫВАЕМ…' : 'ОТКРЫТЬ МАТЧ'}
		</button>
	</section>

	{#if activeMatch}
		<section class="arena-panel active-match-panel" aria-labelledby="active-match-title">
			<div class="section-heading">
				<div>
					<div class="section-kicker">твой матч #{activeMatch.id}</div>
					<h2 id="active-match-title">
						{activeMatch.status === 'waiting' ? 'Ждём соперника' : activeMatch.status === 'accepting' ? 'Подтвердите бой' : 'Бой готов'}
					</h2>
				</div>
				<span class="active-status">{activeMatch.status}</span>
			</div>
			<div class="active-match-copy">
				{#if activeMatch.status === 'waiting'}
					Твой {fighterName(activeMatch.creator_fighter)} готов. Отправь номер матча сопернику.
				{:else if activeMatch.status === 'accepting'}
					Оба игрока внесли ставки. Каждый должен подтвердить участие в течение 15 секунд.
				{:else}
					Оба игрока подтвердили участие.
				{/if}
			</div>
			<div class="active-match-actions">
				{#if activeMatch.status === 'waiting'}
					<button type="button" class="chip" disabled={busy} onclick={cancelActiveMatch}>отменить матч</button>
				{:else if activeMatch.status === 'accepting'}
					<button type="button" class="arena-cta" disabled={busy} onclick={confirmActiveMatch}>
						{busy ? 'ПОДТВЕРЖДАЕМ…' : 'ПОДТВЕРДИТЬ УЧАСТИЕ'}
					</button>
				{:else if activeMatch.status === 'active'}
					<button type="button" class="arena-cta" onclick={() => goto(`/arena/match/${activeMatch!.id}`)}>
						ПЕРЕЙТИ В БОЙ
					</button>
				{/if}
			</div>
		</section>
	{/if}

	<section class="matches-section" aria-labelledby="matches-title">
		<div class="section-heading">
			<div>
				<div class="section-kicker">лобби</div>
				<h2 id="matches-title">Открытые матчи</h2>
			</div>
			<button type="button" class="refresh-button" onclick={loadMatches} disabled={loading || busy} aria-label="Обновить список">↻</button>
		</div>

		{#if loading}
			<div class="empty-state">ищем соперников…</div>
		{:else if matches.length === 0}
			<div class="empty-state">
				<div class="empty-icon">⚔</div>
				Пока никто не ждёт.<br />Открой первый матч.
			</div>
		{:else}
			<div class="match-list">					{#each matches as match (match.id)}
						<article class="match-card">							<div class="match-topline">

							<span class="match-id">МАТЧ #{match.id}</span>
							<span class="match-expiry">{formatExpiry(match.expires_at)}</span>
						</div>
						<div class="match-versus">
							<div class="match-side">
								<span class="match-player">игрок #{match.creator_id}</span>
								<strong>{match.creator_bet}¥</strong>
								<span class="match-fighter"><ArenaFighter fighter={match.creator_fighter ?? null} size="sm" />{fighterName(match.creator_fighter)}</span>
							</div>
							<span class="versus">VS</span>
							<div class="match-side match-side-right">
								<span class="match-player">твоё место</span>
								<strong>?</strong>
								<span class="match-fighter">выбери бойца</span>
							</div>
						</div>							{#if myUserId !== match.creator_id}
							<div class="accept-controls">

							<div class="accept-fighters">
								{#each fighters as fighter (fighter.id)}
									<button
										type="button"
										class={`mini-fighter ${acceptFighter === fighter.id ? 'mini-fighter-selected' : ''}`}
										onclick={() => (acceptFighter = fighter.id)}
									>
										{fighter.name}
									</button>
								{/each}
							</div>
							<div class="bet-row compact">
								<label for={`accept-bet-${match.id}`}>Твоя ставка</label>
								<div class="bet-input-wrap">
									<input id={`accept-bet-${match.id}`} type="number" min="100" step="50" inputmode="numeric" bind:value={acceptBet} disabled={busy} />
									<span>¥</span>
								</div>
							</div>
							<button type="button" class="accept-button" disabled={busy} onclick={() => acceptMatch(match)}>
								{acceptingMatchId === match.id ? 'ПРИНИМАЕМ…' : 'ВСТУПИТЬ'}
							</button>							</div>
							{:else}
								<div class="own-match-label">Это твой открытый матч — управление выше.</div>
							{/if}
						</article>

				{/each}
			</div>
		{/if}
	</section>

	{#if notice}
		<div class="arena-notice" role="status">{notice}</div>
	{/if}
	{#if error}
		<div class="arena-error" role="alert">{error}</div>
	{/if}

	<div class="arena-footnote">Боец соперника скрыт до начала матча · минимальная ставка 100 ювиков</div>
</div>

<style>
	.arena-screen {
		padding: 24px 18px 36px;
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
	}
	.arena-head {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--space-md);
	}
	.arena-kicker,
	.section-kicker {
		font-family: var(--font-body);
		font-size: 10px;
		letter-spacing: 0.14em;
		text-transform: uppercase;
		color: var(--accent-yellow);
	}
	.menu-title {
		font-family: var(--font-chrome);
		font-size: var(--font-display-size);
		font-weight: 700;
		margin: 2px 0 0;
		color: var(--text-primary);
	}
	.menu-sub {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		margin-top: var(--space-xs);
		letter-spacing: 0.04em;
		font-family: var(--font-body);
	}
	.arena-mark {
		font-family: var(--font-numeric);
		font-size: 26px;
		color: var(--accent-pink);
		border: 2px solid var(--accent-pink);
		border-radius: 50%;
		width: 58px;
		height: 58px;
		display: grid;
		place-items: center;
		transform: rotate(-8deg);
		box-shadow: 0 0 20px rgba(255, 91, 141, 0.2);
	}
	.arena-intro,
	.arena-panel,
	.match-card {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
	}
	.arena-intro {
		padding: var(--space-md);
		border-color: rgba(255, 216, 74, 0.45);
		background: linear-gradient(135deg, rgba(255, 216, 74, 0.1), var(--bg-secondary-2));
	}
	.arena-intro-title {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		color: var(--accent-yellow);
	}
	.arena-intro-copy {
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		line-height: 1.5;
		color: var(--text-muted);
		margin-top: var(--space-xs);
	}
	.arena-links { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--space-xs); }
	.arena-link { min-height: 64px; padding: 8px 6px; border: 1px solid var(--border-secondary); border-radius: 10px; background: var(--bg-secondary-2); color: var(--accent-cyan); font: 12px var(--font-chrome); cursor: pointer; }
	.arena-link span { display: block; margin-top: 4px; color: var(--text-muted); font: 10px var(--font-body); }
	.arena-link:hover { border-color: var(--accent-cyan); background: #221d30; }
	.training-link {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: var(--space-md);
		background: var(--bg-secondary-2);
		border: 1px dashed var(--accent-cyan);
		border-radius: 14px;
		text-align: left;
		cursor: pointer;
	}
	.training-link:active {
		transform: scale(0.99);
	}
	.training-link-title {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		color: var(--accent-cyan);
	}
	.training-link-sub {
		font-family: var(--font-body);
		font-size: 12px;
		color: var(--text-muted);
	}
	.arena-panel {
		padding: var(--space-md);
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
	}
	.section-heading {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-sm);
	}
	h2 {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		color: var(--text-primary);
		margin: 2px 0 0;
	}
	.minimum-bet {
		font-family: var(--font-numeric);
		font-size: 16px;
		color: var(--accent-yellow);
	}
	.fighter-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--space-sm);
	}
	.fighter-card {
		min-height: 142px;
		padding: 10px;
		border: 1px solid var(--border-secondary);
		border-radius: 10px;
		background: var(--bg-secondary-1);
		display: flex;
		align-items: center;
		gap: 8px;
		text-align: left;
		font-family: inherit;
		cursor: pointer;
		transition: border-color 0.15s, transform 0.1s, background 0.15s;
	}
	.fighter-card:hover { background: #221d30; }
	.fighter-card:active { transform: scale(0.98); }
	.fighter-selected { border-color: var(--accent-yellow); box-shadow: inset 0 0 0 1px var(--accent-yellow); }
	.fighter-copy { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
	.fighter-name {
		display: block;
		font-family: var(--font-chrome);
		font-size: 15px;
		color: var(--text-primary);
	}
	.fc-pink .fighter-name { color: var(--accent-pink); }
	.fc-cyan .fighter-name { color: var(--accent-cyan); }
	.fc-yellow .fighter-name { color: var(--accent-yellow); }
	.fighter-role,
	.fighter-stats {
		display: block;
		font-family: var(--font-body);
		font-size: 11px;
		line-height: 1.35;
	}
	.fighter-role { color: var(--text-secondary); margin-top: 5px; }
	.fighter-stats { color: var(--text-muted); margin-top: 4px; }
	.fighter-special { color: var(--accent-yellow); font: 10px var(--font-body); margin-top: 3px; }
	.bet-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-sm);
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		color: var(--text-muted);
	}
	.bet-input-wrap {
		display: flex;
		align-items: center;
		gap: 4px;
		color: var(--accent-yellow);
		font-family: var(--font-numeric);
	}
	.bet-input-wrap input {
		width: 104px;
		background: var(--bg-dominant);
		border: 1px solid var(--border-secondary);
		border-radius: 8px;
		padding: 9px 10px;
		color: var(--text-primary);
		font-family: var(--font-numeric);
		font-size: 17px;
		text-align: right;
	}
	.bet-input-wrap input:focus { outline: none; border-color: var(--accent-yellow); }
	.arena-cta,
	.accept-button {
		min-height: 48px;
		border: none;
		border-radius: 10px;
		font-family: var(--font-shout);
		font-size: 17px;
		letter-spacing: 0.04em;
		cursor: pointer;
		transition: transform 0.08s, opacity 0.15s;
	}
	.arena-cta { background: var(--accent-yellow); color: #17120a; box-shadow: 3px 3px 0 #111; }
	.arena-cta:active:not(:disabled), .accept-button:active:not(:disabled) { transform: translate(2px, 2px); }
	.arena-cta:disabled, .accept-button:disabled { opacity: 0.55; cursor: not-allowed; }
	.active-match-copy, .active-match-ready {
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		line-height: 1.5;
		color: var(--text-muted);
	}
	.active-status {
		font-family: var(--font-body);
		font-size: 11px;
		color: var(--accent-cyan);
		text-transform: uppercase;
	}
	.active-match-actions { display: flex; flex-direction: column; gap: var(--space-sm); }
	.active-match-actions .arena-cta { width: 100%; }
	.own-match-label {
		padding-top: var(--space-sm);
		border-top: 1px solid var(--divider);
		font-family: var(--font-body);
		font-size: 12px;
		color: var(--text-muted);
	}
	.matches-section { display: flex; flex-direction: column; gap: var(--space-md); }
	.refresh-button {
		width: 44px;
		height: 44px;
		border: 1px solid var(--border-secondary);
		border-radius: 50%;
		background: var(--bg-secondary-2);
		color: var(--accent-cyan);
		font-size: 24px;
		cursor: pointer;
	}
	.refresh-button:disabled { opacity: 0.5; }
	.match-list { display: flex; flex-direction: column; gap: var(--space-sm); }
	.match-card { padding: var(--space-md); }
	.match-topline { display: flex; justify-content: space-between; gap: var(--space-sm); }
	.match-id, .match-expiry { font-family: var(--font-body); font-size: 10px; letter-spacing: 0.08em; }
	.match-id { color: var(--accent-pink); }
	.match-expiry { color: var(--text-muted); }
	.match-versus { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: var(--space-sm); margin: var(--space-md) 0; }
	.match-side { display: flex; flex-direction: column; gap: 2px; }
	.match-side-right { text-align: right; align-items: flex-end; }
	.match-player, .match-fighter { font-family: var(--font-body); font-size: 11px; color: var(--text-muted); }
	.match-side strong { font-family: var(--font-numeric); font-size: 24px; color: var(--text-primary); }
	.match-fighter { display: inline-flex; align-items: center; gap: 4px; color: var(--accent-cyan); }
	.match-fighter :global(.fighter-visual) { width: 24px; height: 26px; }
	.match-fighter :global(.fighter-label), .match-fighter :global(.fighter-ring), .match-fighter :global(.fighter-aura), .match-fighter :global(.fighter-shadow), .match-fighter :global(.fighter-scanline) { display: none; }
	.versus { font-family: var(--font-numeric); font-size: 18px; color: var(--accent-yellow); }
	.accept-controls { border-top: 1px solid var(--divider); padding-top: var(--space-sm); display: flex; flex-direction: column; gap: var(--space-sm); }
	.accept-fighters { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; }
	.mini-fighter { min-height: 38px; padding: 4px; border: 1px solid var(--border-secondary); border-radius: 6px; background: var(--bg-dominant); color: var(--text-muted); font-family: var(--font-body); font-size: 10px; cursor: pointer; }
	.mini-fighter-selected { border-color: var(--accent-pink); color: var(--accent-pink); background: rgba(255, 91, 141, 0.08); }
	.accept-button { background: var(--accent-pink); color: #1d0c13; box-shadow: 3px 3px 0 #111; }
	.empty-state { padding: var(--space-xl) var(--space-md); border: 1px dashed var(--border-secondary); border-radius: 12px; text-align: center; color: var(--text-muted); font-family: var(--font-body); font-size: var(--font-body-size); line-height: 1.6; }
	.empty-icon { color: var(--accent-yellow); font-size: 30px; margin-bottom: var(--space-xs); }
	.arena-notice, .arena-error { padding: var(--space-sm) var(--space-md); border-radius: 8px; font-family: var(--font-body); font-size: var(--font-body-size); }
	.arena-notice { color: var(--accent-cyan); background: rgba(123, 230, 255, 0.08); border: 1px solid rgba(123, 230, 255, 0.3); }
	.arena-error { color: var(--destructive-text); background: var(--destructive-bg); border: 1px solid rgba(255, 117, 112, 0.3); }
	.arena-footnote { text-align: center; color: var(--text-muted); font-family: var(--font-body); font-size: 11px; line-height: 1.4; }
	@media (max-width: 360px) {
		.arena-links { grid-template-columns: 1fr; }
		.fighter-grid { grid-template-columns: 1fr; }
		.fighter-card { min-height: 118px; }
		.accept-fighters { grid-template-columns: repeat(2, 1fr); }
	}
</style>
