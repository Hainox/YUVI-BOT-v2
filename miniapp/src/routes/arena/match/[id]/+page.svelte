<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { apiFetch, apiStream, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';
	import ArenaFighter from '$lib/components/ArenaFighter.svelte';

	type Action = 'fast_attack' | 'heavy_attack' | 'block' | 'dodge' | 'special';
	type EngineCombatant = {
		fighter: string;
		hp: number;
		energy: number;
		max_hp?: number;
		max_energy?: number;
		dodge_cooldown_remaining: number;
	};
	type Outcome = {
		event_type?: string;
		phase_index: number;
		player_action: string;
		opponent_action: string;
		player_damage: number;
		opponent_damage: number;
		player_counter: boolean;
		opponent_counter: boolean;
	};
	type ArenaState = {
		match_id: number;
		phase_index: number;
		phase_deadline: string;
		phase_started_at: string;
		allowed_actions: Action[];
		actions: Record<string, { action: Action; client_action_id: string }>;
		engine: { player: EngineCombatant; opponent: EngineCombatant; elapsed_seconds: number };
		fighters: { player: string; opponent: string };
		last_outcome: Outcome | null;
		outcome_history: Outcome[];
		terminal: boolean;
		result: string | null;
		winner_id: number | null;
		loser_id: number | null;
		viewer_id: number;
		viewer_side: 'player' | 'opponent';
		reason: string | null;
		refund_required: boolean;
		finished_at: string | null;
		phase_duration_seconds?: number;
	};
	type ArenaEvent = { event_type: string; payload: Partial<ArenaState> & { phase_index?: number } };

	const matchId = Number(page.params.id);
	const actionLabels: Record<Action, { name: string; hint: string }> = {
		fast_attack: { name: 'Быстрая атака', hint: 'быстро, но больно' },
		heavy_attack: { name: 'Тяжёлая атака', hint: 'ломает блок' },
		block: { name: 'Блок', hint: 'защити себя' },
		dodge: { name: 'Уклонение', hint: 'полный dodge атаки' },
		special: { name: 'Спецприём', hint: 'потрать энергию' }
	};

	let matchState = $state<ArenaState | null>(null);
	let loading = $state(true);
	let submitting = $state(false);
	let error = $state<string | null>(null);
	let streamStatus = $state<'connecting' | 'live' | 'offline'>('connecting');
	let secondsLeft = $state(0);
	let selectedAction = $state<Action | null>(null);
	let lastOutcome = $state<Outcome | null>(null);
	let abortController: AbortController | null = null;
	let streamTask: Promise<void> | null = null;
	let timer: ReturnType<typeof setInterval> | null = null;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	let refreshSequence = 0;
	let destroyed = false;

	function describeError(value: unknown): string {
		if (value instanceof ApiError) {
			if (value.status === 404) return 'Боевая сессия ещё не готова или уже закрыта.';
			if (value.status === 409) return value.message;
			if (value.status === 503) return 'Arena runtime временно недоступен.';
			return value.message || 'Не удалось выполнить действие.';
		}
		return 'Соединение с Arena потеряно.';
	}

	function applyState(next: ArenaState): void {
		if (next.match_id !== matchId) return;
		if (matchState && next.phase_index < matchState.phase_index) return;
		if (matchState && next.phase_index > matchState.phase_index) selectedAction = null;
		matchState = next;
		secondsLeft = Math.max(0, Math.ceil((Date.parse(next.phase_deadline) - Date.now()) / 1000));
		if (next.last_outcome && (!lastOutcome || next.last_outcome.phase_index > lastOutcome.phase_index)) {
			lastOutcome = next.last_outcome;
			haptic('tap');
		}
		if (next.terminal) {
			selectedAction = null;
			submitting = false;
		}
	}

	function applyEvent(event: ArenaEvent): void {
		if (event.event_type === 'phase_resolved') {
			const outcome = event.payload as Outcome;
			if (!lastOutcome || outcome.phase_index > lastOutcome.phase_index) {
				lastOutcome = outcome;
				haptic('tap');
			}
			// Pub/Sub carries the compact outcome only. Re-read the authoritative
			// snapshot so HP/energy advance even when no second REST action arrives.
			void loadState();
			return;
		}
		if (event.payload && typeof event.payload.phase_index === 'number') {
			const incoming = event.payload as ArenaState;
			if (!matchState || incoming.phase_index >= matchState.phase_index) {
				matchState = { ...matchState, ...incoming } as ArenaState;
				secondsLeft = matchState.terminal ? 0 : Math.max(0, Math.ceil((Date.parse(matchState.phase_deadline) - Date.now()) / 1000));
			}
		}
		if (event.event_type === 'match_terminal' || event.event_type === 'player_reconnected') {
			void loadState();
		}
		if (event.event_type === 'match_terminal') {
			selectedAction = null;
			submitting = false;
		}
	}

	async function loadState(): Promise<void> {
		const request = ++refreshSequence;
		try {
			const next = await apiFetch<ArenaState>(`/api/v1/arena/matches/${matchId}`);
			if (!destroyed && request === refreshSequence) applyState(next);
			error = null;
		} catch (value) {
			if (!destroyed && request === refreshSequence) error = describeError(value);
		} finally {
			if (!destroyed && request === refreshSequence) loading = false;
		}
	}

	async function reconnect(): Promise<void> {
		if (destroyed || !matchState || matchState.terminal) return;
		try {
			const next = await apiFetch<ArenaState>(`/api/v1/arena/matches/${matchId}/reconnect`, { method: 'POST' });
			applyState(next);
			await loadState();
		} catch (value) {
			if (!destroyed) error = describeError(value);
		}
	}

	async function disconnect(): Promise<void> {
		if (!matchState || matchState.terminal) return;
		try {
			await apiFetch(`/api/v1/arena/matches/${matchId}/disconnect`, { method: 'POST' });
		} catch {
			// The server-side timeout remains authoritative if the WebView vanishes.
		}
	}

	async function sendAction(action: Action): Promise<void> {
		if (submitting || !matchState || matchState.terminal || phaseLocked) return;
		submitting = true;
		selectedAction = action;
		error = null;
		const clientActionId = `arena:${matchId}:${matchState.phase_index}:${crypto.randomUUID()}`;
		try {
			const next = await apiFetch<ArenaState>(`/api/v1/arena/matches/${matchId}/actions`, {
				method: 'POST',
				body: JSON.stringify({ action, client_action_id: clientActionId })
			});
			applyState(next);
		} catch (value) {
			const message = describeError(value);
			if (value instanceof ApiError && value.status === 409) await loadState();
			else error = message;
			if (!(matchState?.terminal)) selectedAction = null;
		} finally {
			submitting = false;
		}
	}

	async function forfeit(): Promise<void> {
		if (!matchState || matchState.terminal || submitting) return;
		if (!window.confirm('Сдаться и получить техническое поражение?')) return;
		submitting = true;
		try {
			const next = await apiFetch<ArenaState>(`/api/v1/arena/matches/${matchId}/forfeit`, { method: 'POST' });
			applyState(next);
			haptic('lose');
		} catch (value) {
			error = describeError(value);
		} finally {
			submitting = false;
		}
	}

	async function consumeEvents(signal: AbortSignal): Promise<void> {
		const response = await apiStream(`/api/v1/arena/matches/${matchId}/events`, { signal });
		if (!response.body) throw new Error('SSE body unavailable');
		streamStatus = 'live';
		const reader = response.body.getReader();
		const decoder = new TextDecoder();
		let buffer = '';
		while (!signal.aborted && !destroyed) {
			const { value, done } = await reader.read();
			if (done) {
				if (!signal.aborted && !destroyed) throw new Error('Arena event stream closed');
				await reader.cancel();
				break;
			}
			buffer += decoder.decode(value ?? new Uint8Array(), { stream: true });
			const chunks = buffer.split('\n\n');
			buffer = chunks.pop() ?? '';
			for (const chunk of chunks) {
				const dataLine = chunk.split('\n').find((line) => line.startsWith('data: '));
				if (!dataLine) continue;
				try {
					applyEvent(JSON.parse(dataLine.slice(6)) as ArenaEvent);
				} catch {
					// Ignore malformed event; the next matchState refresh repairs the UI.
				}
			}
		}
	}

	function scheduleStreamReconnect(): void {
		if (destroyed || reconnectTimer !== null || matchState?.terminal) return;
		reconnectTimer = setTimeout(() => {
			reconnectTimer = null;
			void startEventStream();
		}, 1500);
	}

	async function startEventStream(): Promise<void> {
		if (destroyed || streamTask || matchState?.terminal) return;
		abortController?.abort();
		abortController = new AbortController();
		streamStatus = 'connecting';
		streamTask = consumeEvents(abortController.signal)
			.catch(() => {
				if (!destroyed && !abortController?.signal.aborted) {
					streamStatus = 'offline';
					void reconnect();
					scheduleStreamReconnect();
				}
			})
			.finally(() => {
				streamTask = null;
			});
		await streamTask;
	}

	function updateTimer(): void {
		if (!matchState || matchState.terminal) {
			secondsLeft = 0;
			return;
		}
		secondsLeft = Math.max(0, Math.ceil((Date.parse(matchState.phase_deadline) - Date.now()) / 1000));
	}

	function onVisibilityChange(): void {
		if (document.visibilityState === 'visible') {
			void reconnect().finally(() => startEventStream());
		} else {
			void disconnect();
			abortController?.abort();
		}
	}

	onMount(() => {
		if (!Number.isInteger(matchId) || matchId <= 0) {
			error = 'Некорректный номер матча.';
			loading = false;
			return;
		}
		void (async () => {
			await loadState();
			if (!destroyed && !matchState?.terminal) void startEventStream();
		})();
		timer = setInterval(updateTimer, 250);
		document.addEventListener('visibilitychange', onVisibilityChange);
		return () => {
			destroyed = true;
			abortController?.abort();
			if (reconnectTimer !== null) clearTimeout(reconnectTimer);
			if (timer !== null) clearInterval(timer);
			document.removeEventListener('visibilitychange', onVisibilityChange);
			void disconnect();
		};
	});

	const viewerSide = $derived(matchState?.viewer_side ?? 'player');
	const enemySide = $derived(viewerSide === 'player' ? 'opponent' : 'player');
	const viewerCombatant = $derived(matchState?.engine[viewerSide]);
	const enemyCombatant = $derived(matchState?.engine[enemySide]);
	const viewerFighter = $derived(matchState?.fighters[viewerSide] ?? 'Боец');
	const enemyFighter = $derived(matchState?.fighters[enemySide] ?? 'Соперник');
	const playerHp = $derived(viewerCombatant?.hp ?? 0);
	const opponentHp = $derived(enemyCombatant?.hp ?? 0);
	const playerMaxHp = $derived(viewerCombatant?.max_hp ?? 130);
	const opponentMaxHp = $derived(enemyCombatant?.max_hp ?? 130);
	const playerEnergy = $derived(viewerCombatant?.energy ?? 0);
	const phaseDuration = $derived(matchState?.phase_duration_seconds ?? 3);
	const dealtDamage = $derived(lastOutcome ? (viewerSide === 'player' ? lastOutcome.player_damage : lastOutcome.opponent_damage) : 0);
	const incomingDamage = $derived(lastOutcome ? (viewerSide === 'player' ? lastOutcome.opponent_damage : lastOutcome.player_damage) : 0);
	const phaseLocked = $derived(Boolean(matchState?.actions && Object.keys(matchState.actions).length > 0));
	const viewerVisualState = $derived(matchState?.terminal ? (matchState.winner_id === matchState.viewer_id ? 'victory' : matchState.refund_required ? 'idle' : 'defeat') : selectedAction ? 'attack' : 'idle');
	const enemyVisualState = $derived(matchState?.terminal ? (matchState.winner_id === matchState.viewer_id ? 'defeat' : matchState.refund_required ? 'idle' : 'victory') : lastOutcome ? 'hit' : 'idle');
	const viewerAction = $derived(selectedAction ?? (viewerSide === 'player' ? lastOutcome?.player_action : lastOutcome?.opponent_action) ?? '');
	const enemyAction = $derived(viewerSide === 'player' ? lastOutcome?.opponent_action ?? '' : lastOutcome?.player_action ?? '');
	const resultLabel = $derived(
		matchState?.refund_required
			? 'ВОЗВРАТ СТАВОК'
			: matchState?.winner_id === matchState?.viewer_id
				? 'ПОБЕДА'
				: 'БОЙ ОКОНЧЕН'
	);
</script>

<div class="fight-screen" aria-busy={loading}>
	{#if loading}
		<div class="screen-loading"><span>подключение к бою…</span></div>
	{:else if error && !matchState}
		<div class="fight-error" role="alert"><strong>Бой недоступен</strong><span>{error}</span><button type="button" class="chip" onclick={() => { error = null; void loadState(); }}>повторить</button></div>
	{:else if matchState}
		<div class="fight-head">
			<div>
				<div class="fight-kicker">YUVI ARENA · MATCH #{matchId}</div>
				<h1 class="menu-title">{matchState.terminal ? resultLabel : 'Бой'}</h1>
			</div>
			<div class={`connection-dot ${streamStatus}`} title={streamStatus === 'live' ? 'соединение активно' : 'переподключение'}></div>
		</div>

		<div class="fight-hud">
			<div class="fighter-hud fighter-hud-with-visual">
				<ArenaFighter fighter={viewerFighter as 'tank' | 'assassin' | 'berserker' | 'tactician'} size="sm" state={viewerVisualState} action={viewerAction} />
				<div class="fighter-hud-name">{viewerFighter}</div>
				<div class="bar"><span style={`width: ${Math.max(0, Math.min(100, (playerHp / playerMaxHp) * 100))}%`}></span></div>
				<div class="bar-label">{playerHp} HP · {playerEnergy} EN</div>
			</div>
			<div class="fight-timer">{matchState.terminal ? '—' : secondsLeft}</div>
			<div class="fighter-hud opponent fighter-hud-with-visual">
				<ArenaFighter fighter={enemyFighter as 'tank' | 'assassin' | 'berserker' | 'tactician'} size="sm" side="opponent" state={enemyVisualState} action={enemyAction} />
				<div class="fighter-hud-name">{enemyFighter}</div>
				<div class="bar"><span style={`width: ${Math.max(0, Math.min(100, (opponentHp / opponentMaxHp) * 100))}%`}></span></div>
				<div class="bar-label">{opponentHp} HP</div>
			</div>
		</div>

		<div class="arena-stage" aria-hidden="true">
			<div class="stage-grid"></div>
			<div class="stage-name stage-name-player">{viewerFighter}</div>
			<ArenaFighter fighter={viewerFighter as 'tank' | 'assassin' | 'berserker' | 'tactician'} size="lg" state={viewerVisualState} action={viewerAction} />
			<div class="stage-vs">VS</div>
			<ArenaFighter fighter={enemyFighter as 'tank' | 'assassin' | 'berserker' | 'tactician'} size="lg" side="opponent" state={enemyVisualState} action={enemyAction} />
			<div class="stage-name stage-name-opponent">{enemyFighter}</div>
			{#if lastOutcome}
				<div class="damage-pop damage-pop-player">-{dealtDamage}</div>
				<div class="damage-pop damage-pop-opponent">-{incomingDamage}</div>
			{/if}
		</div>

		<div class="phase-card">
			<div class="phase-label">ФАЗА {matchState.phase_index + 1} · {matchState.terminal ? 'ЗАВЕРШЕНО' : `${secondsLeft} сек`}</div>
			<div class="phase-track"><span style={`width: ${matchState.terminal ? 100 : Math.max(0, Math.min(100, (secondsLeft / phaseDuration) * 100))}%`}></span></div>
			{#if phaseLocked && !matchState.terminal}<div class="phase-note">Действие принято — ждём соперника</div>{/if}
		</div>

		{#if lastOutcome}
			<div class="outcome-card" class:outcome-counter={lastOutcome.player_counter}>
				<strong>{lastOutcome.player_action} ↔ {lastOutcome.opponent_action}</strong>
				<span>твой урон {dealtDamage} · входящий {incomingDamage}</span>
			</div>
		{/if}

		{#if matchState.terminal}
			<div class="terminal-card">
				<div class="terminal-title">{resultLabel}</div>
				<div>{matchState.reason === 'both_disconnected' ? 'Оба игрока отключились.' : matchState.reason ?? 'Матч завершён.'}</div>
				{#if matchState.refund_required}<div class="refund-note">Ставки будут возвращены финансовым контуром.</div>{/if}
				<button type="button" class="result-link" onclick={() => goto(`/arena/result/${matchId}`)}>ПОКАЗАТЬ ФИНАЛЬНЫЙ ИТОГ</button>
			</div>
		{:else}
			<div class="actions-grid">
				{#each matchState.allowed_actions as action (action)}
					<button type="button" class={`action-button action-${action} ${selectedAction === action ? 'selected' : ''}`} disabled={submitting || phaseLocked} onclick={() => sendAction(action)}>
						<span>{actionLabels[action as Action]?.name ?? action}</span>
						<small>{actionLabels[action as Action]?.hint ?? ''}</small>
					</button>
				{/each}
			</div>
			<button type="button" class="forfeit-button" disabled={submitting} onclick={forfeit}>Сдаться</button>
		{/if}

		{#if error}<div class="fight-inline-error" role="alert">{error}</div>{/if}
	{:else}
		<div class="fight-error" role="alert">Состояние боя не загрузилось.</div>
	{/if}
</div>

<style>
	.fight-screen { padding: 24px 18px 36px; display: flex; flex-direction: column; gap: var(--space-md); }
	.fight-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-md); }
	.fight-kicker { color: var(--accent-yellow); font: 10px var(--font-body); letter-spacing: .14em; text-transform: uppercase; }
	.menu-title { margin: 2px 0 0; color: var(--text-primary); font: 700 var(--font-display-size) var(--font-chrome); }
	.connection-dot { width: 12px; height: 12px; margin-top: 8px; border-radius: 50%; background: var(--accent-yellow); box-shadow: 0 0 12px currentColor; }
	.connection-dot.live { color: var(--positive); background: var(--positive); }
	.connection-dot.offline { color: var(--destructive); background: var(--destructive); }
	.fight-hud { display: grid; grid-template-columns: minmax(0, 1fr) 48px minmax(0, 1fr); align-items: center; gap: var(--space-sm); padding: var(--space-md); background: linear-gradient(180deg, rgba(28,24,39,.98), rgba(13,10,24,.98)); border: 1px solid var(--border-secondary); border-radius: 14px; }
	.fighter-hud-with-visual { position: relative; min-width: 0; }
	.fighter-hud-with-visual :global(.fighter-visual) { width: 56px; height: 60px; margin: -4px 0 -7px; }
	.fighter-hud-with-visual.opponent :global(.fighter-visual) { margin-left: auto; }
	.fighter-hud.opponent { text-align: right; }
	.fighter-hud-name { overflow: hidden; color: var(--text-primary); font: 700 13px var(--font-chrome); text-overflow: ellipsis; white-space: nowrap; }
	.bar { height: 9px; margin-top: 8px; overflow: hidden; border-radius: 5px; background: var(--bg-dominant); }
	.bar span { display: block; height: 100%; border-radius: inherit; background: var(--accent-cyan); transition: width .2s ease; }
	.opponent .bar span { margin-left: auto; background: var(--accent-pink); }
	.bar-label { margin-top: 4px; color: var(--text-muted); font: 11px var(--font-body); }
	.fight-timer { color: var(--accent-yellow); font: 900 28px var(--font-numeric); text-align: center; }
	.arena-stage { position: relative; min-height: 224px; display: flex; align-items: flex-end; justify-content: space-around; gap: 4px; overflow: hidden; padding: 12px 4px 8px; border: 1px solid var(--border-secondary); border-radius: 16px; background: radial-gradient(ellipse at 50% 80%, rgba(123,230,255,.12), transparent 48%), linear-gradient(180deg, #151225 0%, #0b0914 100%); }
	.stage-grid { position: absolute; inset: 42% 0 0; opacity: .32; background: linear-gradient(rgba(123,230,255,.22) 1px, transparent 1px), linear-gradient(90deg, rgba(123,230,255,.22) 1px, transparent 1px); background-size: 26px 18px; transform: perspective(120px) rotateX(58deg) scale(1.35); transform-origin: bottom; mask-image: linear-gradient(to top, black, transparent); }
	.stage-name { position: absolute; top: 10px; color: var(--text-muted); font: 10px var(--font-body); letter-spacing: .08em; text-transform: uppercase; }
	.stage-name-player { left: 12px; color: var(--accent-cyan); }
	.stage-name-opponent { right: 12px; color: var(--accent-pink); }
	.stage-vs { position: absolute; left: 50%; top: 44%; z-index: 1; color: var(--accent-yellow); font: 24px var(--font-numeric); opacity: .75; transform: translate(-50%, -50%) rotate(-8deg); text-shadow: 0 0 16px rgba(255,216,74,.7); }
	.damage-pop { position: absolute; z-index: 3; top: 45%; color: var(--accent-pink); font: 28px var(--font-numeric); text-shadow: 2px 2px 0 #080812, 0 0 12px var(--accent-pink); animation: damage-rise .65s ease-out both; }
	.damage-pop-player { left: 24%; }
	.damage-pop-opponent { right: 24%; color: var(--accent-yellow); }
	.phase-card, .outcome-card, .terminal-card { padding: var(--space-md); background: var(--bg-secondary-2); border: 1px solid var(--border-secondary); border-radius: 12px; }
	.phase-label { color: var(--text-primary); font: 13px var(--font-chrome); text-align: center; }
	.phase-track { height: 6px; margin-top: 10px; overflow: hidden; border-radius: 4px; background: var(--bg-dominant); }
	.phase-track span { display: block; height: 100%; background: var(--accent-yellow); transition: width .2s linear; }
	.phase-note, .outcome-card span, .terminal-card { color: var(--text-muted); font: 12px/1.5 var(--font-body); }
	.phase-note { margin-top: 8px; text-align: center; }
	.outcome-card { display: flex; flex-direction: column; gap: 4px; text-align: center; }
	.outcome-card strong { color: var(--accent-cyan); font-family: var(--font-chrome); }
	.outcome-counter { border-color: var(--accent-yellow); }
	.terminal-card { border-color: var(--accent-pink); text-align: center; }
	.terminal-title { color: var(--accent-pink); font: 700 28px var(--font-numeric); }
	.refund-note { margin-top: 8px; color: var(--accent-yellow); }
	.result-link { margin-top: 10px; min-height: 44px; border: 0; border-radius: 8px; background: var(--accent-yellow); color: #17120a; cursor: pointer; font: 700 12px var(--font-chrome); }
	.actions-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-sm); }
	.action-button { position: relative; min-height: 82px; padding: 10px; border: 1px solid var(--border-secondary); border-radius: 12px; background: linear-gradient(145deg, rgba(28,24,39,.98), rgba(13,10,24,.98)); color: var(--text-primary); cursor: pointer; font-family: var(--font-chrome); transition: transform .12s ease, border-color .15s ease, box-shadow .15s ease, background .15s ease; }
	.action-button::before { content: ''; position: absolute; left: 10px; right: 10px; top: 8px; height: 2px; border-radius: 2px; background: currentColor; opacity: .35; }
	.action-button:hover:not(:disabled) { transform: translateY(-2px); border-color: currentColor; box-shadow: 0 8px 22px rgba(0,0,0,.25); }
	.action-button span, .action-button small { display: block; }
	.action-button small { margin-top: 5px; color: var(--text-muted); font: 11px var(--font-body); }
	.action-button.selected { border-color: var(--accent-yellow); box-shadow: inset 0 0 0 1px var(--accent-yellow), 0 0 18px rgba(255,216,74,.16); background: rgba(255,216,74,.09); }
	.action-fast_attack { border-color: rgba(123, 230, 255, .45); }
	.action-heavy_attack { border-color: rgba(255, 91, 141, .45); }
	.action-block { border-color: rgba(255, 216, 74, .45); }
	.action-dodge { border-color: rgba(123, 230, 255, .45); }
	.action-special { grid-column: 1 / -1; border-color: var(--accent-pink); }
	.forfeit-button { min-height: 44px; border: 0; background: transparent; color: var(--text-muted); cursor: pointer; font: 12px var(--font-body); }
	.fight-inline-error, .fight-error { padding: var(--space-sm) var(--space-md); border-radius: 8px; background: var(--destructive-bg); color: var(--destructive-text); font: 13px/1.5 var(--font-body); }
	.fight-error { min-height: 40vh; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: var(--space-sm); text-align: center; }
	@keyframes damage-rise { from { opacity: 0; transform: translateY(12px) scale(.75); } 20% { opacity: 1; } to { opacity: 0; transform: translateY(-34px) scale(1.08); } }
	@media (prefers-reduced-motion: reduce) { .action-button, .damage-pop { transition: none; animation: none; } }
	@media (max-width: 360px) { .fight-hud { grid-template-columns: 1fr 38px 1fr; padding: 10px; } .fighter-hud-name { font-size: 11px; } .fight-timer { font-size: 23px; } .arena-stage { min-height: 190px; } .arena-stage :global(.fighter-lg) { width: 132px; height: 140px; } }
</style>
