<script lang="ts">
	// Arena training mode (Phase 6, FIGHTING_SPEC.md §9) — practice against a
	// random AI, no stakes/rating/XP. Simpler than /arena/match/[id]: the AI
	// "submits" its action synchronously inside the same request as the
	// player's action (api/routes/arena_training.py), so there's no SSE
	// stream, no phase deadline countdown, no disconnect/reconnect dance —
	// one POST fully resolves one phase.
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	type Action = 'fast_attack' | 'heavy_attack' | 'block' | 'dodge' | 'special';
	type FighterType = 'tank' | 'assassin' | 'berserker' | 'tactician';
	type EngineCombatant = { hp: number; max_hp: number; energy: number; max_energy: number };
	type Outcome = {
		player_action: string;
		opponent_action: string;
		player_damage: number;
		opponent_damage: number;
		player_counter: boolean;
		opponent_counter: boolean;
	};
	type TrainingState = {
		fighters: { player: FighterType; opponent: FighterType };
		engine: { player: EngineCombatant; opponent: EngineCombatant };
		phase_index: number;
		paused: boolean;
		terminal: boolean;
		result: 'win' | 'draw' | 'technical_loss' | null;
		winner: 'player' | 'opponent' | null;
		last_outcome: Outcome | null;
	};

	type Fighter = { id: FighterType; name: string; role: string; accent: 'pink' | 'cyan' | 'yellow' };
	const fighters: Fighter[] = [
		{ id: 'tank', name: 'Танк', role: 'выносливость и защита', accent: 'cyan' },
		{ id: 'assassin', name: 'Ассасин', role: 'скорость и уклонение', accent: 'pink' },
		{ id: 'berserker', name: 'Берсерк', role: 'риск и урон на низком HP', accent: 'yellow' },
		{ id: 'tactician', name: 'Тактик', role: 'контроль и эффекты', accent: 'cyan' }
	];
	const actionLabels: Record<Action, { name: string; hint: string }> = {
		fast_attack: { name: 'Быстрая атака', hint: 'быстро, но больно' },
		heavy_attack: { name: 'Тяжёлая атака', hint: 'ломает блок' },
		block: { name: 'Блок', hint: 'защити себя' },
		dodge: { name: 'Уклонение', hint: 'полный dodge атаки' },
		special: { name: 'Спецприём', hint: 'потрать энергию' }
	};
	const allActions: Action[] = ['fast_attack', 'heavy_attack', 'block', 'dodge', 'special'];

	let selectedFighter = $state<FighterType>('tank');
	let selectedOpponent = $state<FighterType | 'random'>('random');
	let training = $state<TrainingState | null>(null);
	let starting = $state(false);
	let submitting = $state(false);
	let error = $state<string | null>(null);

	function describeError(value: unknown): string {
		if (value instanceof ApiError) {
			if (value.status === 409) return value.message || 'Тренировка на паузе.';
			if (value.status === 422) return value.message || 'Это действие сейчас недоступно.';
			if (value.status === 503) return 'Тренировка временно недоступна.';
			return value.message || 'Не удалось выполнить действие.';
		}
		return 'Соединение с Arena потеряно.';
	}

	async function startTraining(): Promise<void> {
		if (starting) return;
		starting = true;
		error = null;
		try {
			training = await apiFetch<TrainingState>('/api/v1/arena/training', {
				method: 'POST',
				body: JSON.stringify({
					fighter: selectedFighter,
					opponent_fighter: selectedOpponent === 'random' ? null : selectedOpponent
				})
			});
			haptic('tap');
		} catch (value) {
			error = describeError(value);
			haptic('error');
		} finally {
			starting = false;
		}
	}

	async function sendAction(action: Action): Promise<void> {
		if (submitting || !training || training.terminal || training.paused) return;
		submitting = true;
		error = null;
		try {
			training = await apiFetch<TrainingState>('/api/v1/arena/training/actions', {
				method: 'POST',
				body: JSON.stringify({ action })
			});
			haptic(training.terminal ? (training.winner === 'player' ? 'big-win' : 'lose') : 'tap');
		} catch (value) {
			error = describeError(value);
			haptic('error');
		} finally {
			submitting = false;
		}
	}

	async function togglePause(): Promise<void> {
		if (!training || training.terminal || submitting) return;
		submitting = true;
		try {
			training = await apiFetch<TrainingState>('/api/v1/arena/training/pause', {
				method: 'POST',
				body: JSON.stringify({ paused: !training.paused })
			});
			haptic('tap');
		} catch (value) {
			error = describeError(value);
		} finally {
			submitting = false;
		}
	}

	function backToSetup(): void {
		training = null;
		error = null;
	}

	function fighterName(id: FighterType): string {
		return fighters.find((fighter) => fighter.id === id)?.name ?? id;
	}

	function actionName(action: string): string {
		return actionLabels[action as Action]?.name ?? action;
	}

	const resultLabel = $derived(
		training?.result === 'draw' ? 'НИЧЬЯ' : training?.winner === 'player' ? 'ПОБЕДА' : 'ПОРАЖЕНИЕ'
	);
</script>

<div class="training-screen">
	<div class="training-head">
		<div class="training-kicker">YUVI ARENA · ТРЕНИРОВКА</div>
		<h1 class="menu-title">Тренировка</h1>
		<div class="menu-sub">без ставок · без рейтинга · против AI</div>
	</div>

	{#if !training}
		<section class="training-panel">
			<div class="section-heading">
				<div class="section-kicker">твой боец</div>
			</div>
			<div class="fighter-grid">
				{#each fighters as fighter (fighter.id)}
					<button
						type="button"
						class={`fighter-card fc-${fighter.accent} ${selectedFighter === fighter.id ? 'fighter-selected' : ''}`}
						aria-pressed={selectedFighter === fighter.id}
						onclick={() => (selectedFighter = fighter.id)}
					>
						<span class="fighter-name">{fighter.name}</span>
						<span class="fighter-role">{fighter.role}</span>
					</button>
				{/each}
			</div>

			<div class="section-heading">
				<div class="section-kicker">соперник (AI)</div>
			</div>
			<div class="opponent-row">
				<button
					type="button"
					class={`chip ${selectedOpponent === 'random' ? 'chip-on' : ''}`}
					onclick={() => (selectedOpponent = 'random')}
				>
					случайный
				</button>
				{#each fighters as fighter (fighter.id)}
					<button
						type="button"
						class={`chip ${selectedOpponent === fighter.id ? 'chip-on' : ''}`}
						onclick={() => (selectedOpponent = fighter.id)}
					>
						{fighter.name}
					</button>
				{/each}
			</div>

			{#if error}<div class="training-error" role="alert">{error}</div>{/if}

			<button type="button" class="training-cta" disabled={starting} onclick={startTraining}>
				{starting ? 'ГОТОВИМ БОЙ…' : 'НАЧАТЬ ТРЕНИРОВКУ'}
			</button>
		</section>
	{:else}
		<div class="fight-hud">
			<div class="fighter-hud">
				<div class="fighter-hud-name">{fighterName(training.fighters.player)}</div>
				<div class="bar">
					<span
						style={`width: ${Math.max(0, Math.min(100, (training.engine.player.hp / training.engine.player.max_hp) * 100))}%`}
					></span>
				</div>
				<div class="bar-label">{training.engine.player.hp} HP · {training.engine.player.energy} EN</div>
			</div>
			<div class="fight-vs">VS</div>
			<div class="fighter-hud opponent">
				<div class="fighter-hud-name">{fighterName(training.fighters.opponent)}</div>
				<div class="bar">
					<span
						style={`width: ${Math.max(0, Math.min(100, (training.engine.opponent.hp / training.engine.opponent.max_hp) * 100))}%`}
					></span>
				</div>
				<div class="bar-label">{training.engine.opponent.hp} HP</div>
			</div>
		</div>

		<div class="phase-card">
			<div class="phase-label">ФАЗА {training.phase_index + 1}{training.paused ? ' · ПАУЗА' : ''}</div>
		</div>

		{#if training.last_outcome}
			<div class="outcome-card" class:outcome-counter={training.last_outcome.player_counter}>
				<strong>{actionName(training.last_outcome.player_action)} ↔ {actionName(training.last_outcome.opponent_action)}</strong>
				<span>твой урон {training.last_outcome.player_damage} · входящий {training.last_outcome.opponent_damage}</span>
			</div>
		{/if}

		{#if training.terminal}
			<div class="terminal-card">
				<div class="terminal-title">{resultLabel}</div>
				<div class="terminal-actions">
					<button type="button" class="training-cta" onclick={backToSetup}>ЕЩЁ РАЗ</button>
				</div>
			</div>
		{:else}
			<div class="actions-grid">
				{#each allActions as action (action)}
					<button
						type="button"
						class={`action-button action-${action}`}
						disabled={submitting || training.paused}
						onclick={() => sendAction(action)}
					>
						<span>{actionLabels[action].name}</span>
						<small>{actionLabels[action].hint}</small>
					</button>
				{/each}
			</div>
			<button type="button" class="pause-button" disabled={submitting} onclick={togglePause}>
				{training.paused ? 'продолжить' : 'пауза'}
			</button>
		{/if}

		{#if error}<div class="training-error" role="alert">{error}</div>{/if}
	{/if}
</div>

<style>
	.training-screen {
		padding: 24px 18px 36px;
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
	}
	.training-kicker {
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
		font-family: var(--font-body);
	}
	.training-panel {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-md);
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
	}
	.section-kicker {
		font-family: var(--font-body);
		font-size: 10px;
		letter-spacing: 0.14em;
		text-transform: uppercase;
		color: var(--accent-yellow);
	}
	.fighter-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--space-sm);
	}
	.fighter-card {
		min-height: 84px;
		padding: 12px;
		border: 1px solid var(--border-secondary);
		border-radius: 10px;
		background: var(--bg-secondary-1);
		text-align: left;
		font-family: inherit;
		cursor: pointer;
	}
	.fighter-selected {
		border-color: var(--accent-yellow);
		box-shadow: inset 0 0 0 1px var(--accent-yellow);
	}
	.fighter-name {
		display: block;
		font-family: var(--font-chrome);
		font-size: 15px;
		color: var(--text-primary);
	}
	.fc-pink .fighter-name {
		color: var(--accent-pink);
	}
	.fc-cyan .fighter-name {
		color: var(--accent-cyan);
	}
	.fc-yellow .fighter-name {
		color: var(--accent-yellow);
	}
	.fighter-role {
		display: block;
		font-family: var(--font-body);
		font-size: 11px;
		color: var(--text-secondary);
		margin-top: 5px;
	}
	.opponent-row {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-xs);
	}
	.training-cta {
		min-height: 48px;
		border: none;
		border-radius: 10px;
		background: var(--accent-yellow);
		color: #17120a;
		font-family: var(--font-shout);
		font-size: 17px;
		letter-spacing: 0.04em;
		cursor: pointer;
		box-shadow: 3px 3px 0 #111;
	}
	.training-cta:active:not(:disabled) {
		transform: translate(2px, 2px);
	}
	.training-cta:disabled {
		opacity: 0.55;
		cursor: not-allowed;
	}
	.training-error {
		padding: var(--space-sm) var(--space-md);
		border-radius: 8px;
		background: var(--destructive-bg);
		color: var(--destructive-text);
		font-family: var(--font-body);
		font-size: 13px;
		line-height: 1.5;
	}

	.fight-hud {
		display: grid;
		grid-template-columns: 1fr 40px 1fr;
		align-items: center;
		gap: var(--space-sm);
		padding: var(--space-md);
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
	}
	.fighter-hud.opponent {
		text-align: right;
	}
	.fighter-hud-name {
		overflow: hidden;
		color: var(--text-primary);
		font: 700 13px var(--font-chrome);
		text-overflow: ellipsis;
		white-space: nowrap;
		text-transform: capitalize;
	}
	.bar {
		height: 9px;
		margin-top: 8px;
		overflow: hidden;
		border-radius: 5px;
		background: var(--bg-dominant);
	}
	.bar span {
		display: block;
		height: 100%;
		border-radius: inherit;
		background: var(--accent-cyan);
		transition: width 0.2s ease;
	}
	.opponent .bar span {
		margin-left: auto;
		background: var(--accent-pink);
	}
	.bar-label {
		margin-top: 4px;
		color: var(--text-muted);
		font: 11px var(--font-body);
	}
	.fight-vs {
		text-align: center;
		font-family: var(--font-numeric);
		font-size: 16px;
		color: var(--accent-yellow);
	}
	.phase-card,
	.outcome-card,
	.terminal-card {
		padding: var(--space-md);
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 12px;
	}
	.phase-label {
		color: var(--text-primary);
		font: 13px var(--font-chrome);
		text-align: center;
	}
	.outcome-card {
		display: flex;
		flex-direction: column;
		gap: 4px;
		text-align: center;
	}
	.outcome-card strong {
		color: var(--accent-cyan);
		font-family: var(--font-chrome);
	}
	.outcome-card span {
		color: var(--text-muted);
		font: 12px/1.5 var(--font-body);
	}
	.outcome-counter {
		border-color: var(--accent-yellow);
	}
	.terminal-card {
		border-color: var(--accent-pink);
		text-align: center;
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
	}
	.terminal-title {
		color: var(--accent-pink);
		font: 700 28px var(--font-numeric);
	}
	.actions-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--space-sm);
	}
	.action-button {
		min-height: 76px;
		padding: 10px;
		border: 1px solid var(--border-secondary);
		border-radius: 12px;
		background: var(--bg-secondary-2);
		color: var(--text-primary);
		cursor: pointer;
		font-family: var(--font-chrome);
	}
	.action-button span,
	.action-button small {
		display: block;
	}
	.action-button small {
		margin-top: 5px;
		color: var(--text-muted);
		font: 11px var(--font-body);
	}
	.action-button:disabled {
		opacity: 0.55;
		cursor: not-allowed;
	}
	.action-fast_attack {
		border-color: rgba(123, 230, 255, 0.45);
	}
	.action-heavy_attack {
		border-color: rgba(255, 91, 141, 0.45);
	}
	.action-block {
		border-color: rgba(255, 216, 74, 0.45);
	}
	.action-dodge {
		border-color: rgba(123, 230, 255, 0.45);
	}
	.action-special {
		grid-column: 1 / -1;
		border-color: var(--accent-pink);
	}
	.pause-button {
		min-height: 44px;
		border: 1px solid var(--border-secondary);
		border-radius: 10px;
		background: var(--bg-secondary-2);
		color: var(--text-muted);
		cursor: pointer;
		font: 12px var(--font-body);
	}
</style>
