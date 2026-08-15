<script lang="ts">
	// Квесты + достижения — read-only лента прогресса (тот же паттерн, что
	// history/rules/whatsnew): один GET на mount. GET /api/v1/quests сам
	// проверяет и клеймит выполненные квесты/ачивки на сервере и возвращает
	// итоговый статус — это не новый паттерн, GET /api/v1/farm уже так делает
	// на каждое чтение.
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	// Абсолютный потолок уровня фермы (база + все бонусы квестов/ачивок) —
	// только для UI-подписи "X / 99", сервер считает effective_max_level сам.
	const TOTAL_MAX_LEVEL = 99;

	type Quest = {
		key: string;
		label: string;
		description: string;
		reward: number;
		done: boolean;
		claimed: boolean;
	};
	type Achievement = {
		key: string;
		label: string;
		description: string;
		reward: number;
		bonus_levels: number;
		unlocked: boolean;
	};
	type ClaimedQuest = { key: string; label: string; reward: number };
	type UnlockedAchievement = { key: string; label: string; reward: number; bonus_levels: number };
	type QuestsResponse = {
		quests: Quest[];
		achievements: Achievement[];
		base_max_level: number;
		quest_bonus_levels: number;
		achievement_bonus_levels: number;
		effective_max_level: number;
		newly_claimed_quests: ClaimedQuest[];
		newly_unlocked_achievements: UnlockedAchievement[];
	};

	let loading = $state(true);
	let error = $state<string | null>(null);
	let data = $state<QuestsResponse | null>(null);

	let toastQuests = $state<ClaimedQuest[]>([]);
	let toastAchievements = $state<UnlockedAchievement[]>([]);

	function describeError(err: unknown): string {
		return err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
	}

	function questIcon(q: Quest): string {
		if (q.claimed) return '✅';
		if (q.done) return '🎁';
		return '⏳';
	}

	function questStatus(q: Quest): string {
		if (q.done && q.claimed) return 'выполнено · награда получена';
		if (q.done && !q.claimed) return 'выполнено · начисляем награду…';
		return 'в процессе';
	}

	function achievementIcon(a: Achievement): string {
		return a.unlocked ? '🏆' : '🔒';
	}

	function achievementStatus(a: Achievement): string {
		return a.unlocked ? 'разблокировано' : 'заблокировано';
	}

	async function loadQuests() {
		try {
			const res = await apiFetch<QuestsResponse>('/api/v1/quests');
			data = res;
			if (res.newly_claimed_quests.length > 0 || res.newly_unlocked_achievements.length > 0) {
				toastQuests = res.newly_claimed_quests;
				toastAchievements = res.newly_unlocked_achievements;
				haptic('win');
			}
		} catch (err) {
			error = describeError(err);
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		loadQuests();
	});
</script>

<div class="quests-screen">
	<div class="menu-head">
		<h1 class="menu-title">Квесты</h1>
		<div class="menu-sub">выполняй квесты, качай потолок фермы</div>
	</div>

	{#if loading}
		<div class="screen-loading"><span>загрузка квестов…</span></div>
	{:else if error}
		<div class="cf-error">{error}</div>
	{:else if data}
		{#if toastQuests.length > 0 || toastAchievements.length > 0}
			<div class="quests-toast">
				<div class="qt-title">🎉 только что получено</div>
				{#each toastQuests as tq (tq.key)}
					<div class="qt-item">{tq.label} — +{tq.reward} CP</div>
				{/each}
				{#each toastAchievements as ta (ta.key)}
					<div class="qt-item">{ta.label} — +{ta.reward} CP, +{ta.bonus_levels} ур. потолка</div>
				{/each}
			</div>
		{/if}

		<div class="quests-cap-card">
			<div class="qcc-label">потолок уровня фермы</div>
			<div class="qcc-val">
				{data.effective_max_level}<span class="qcc-of"> / {TOTAL_MAX_LEVEL}</span>
			</div>
			<div class="qcc-breakdown">
				база {data.base_max_level} + квесты {data.quest_bonus_levels} + достижения {data.achievement_bonus_levels}
			</div>
		</div>

		<div class="quests-section">
			<h2 class="qs-title">Квесты дня</h2>
			<div class="quests-list">
				{#each data.quests as q (q.key)}
					<div class="feature-card quest-row" class:is-done={q.done}>
						<span class="fc-title">{questIcon(q)} {q.label}</span>
						<span class="fc-quest-desc">{q.description}</span>
						<span class="fc-desc">{questStatus(q)} · награда {q.reward} CP</span>
					</div>
				{/each}
			</div>
		</div>

		<div class="quests-section">
			<h2 class="qs-title">Достижения</h2>
			<div class="quests-list">
				{#each data.achievements as a (a.key)}
					<div class="feature-card quest-row" class:is-done={a.unlocked}>
						<span class="fc-title">{achievementIcon(a)} {a.label}</span>
						<span class="fc-quest-desc">{a.description}</span>
						<span class="fc-desc">
							{achievementStatus(a)} · +{a.reward} CP, +{a.bonus_levels} ур. потолка
						</span>
					</div>
				{/each}
			</div>
		</div>
	{/if}
</div>

<style>
	.quests-screen {
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

	.cf-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.quests-toast {
		background: var(--bg-secondary-2);
		border: 1px solid var(--accent-cyan);
		border-radius: 14px;
		padding: var(--space-md);
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.qt-title {
		font-family: var(--font-chrome);
		font-size: var(--font-body-size);
		font-weight: 700;
		color: var(--accent-cyan);
	}
	.qt-item {
		font-size: var(--font-body-size);
		font-family: var(--font-body);
		color: var(--text-primary);
	}

	.quests-cap-card {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-lg);
		text-align: center;
	}
	.qcc-label {
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
	}
	.qcc-val {
		font-family: var(--font-numeric);
		font-size: var(--font-hero-size);
		font-weight: 900;
		color: var(--accent-cyan);
		line-height: 1.1;
	}
	.qcc-of {
		font-size: var(--font-heading-size);
		color: var(--text-muted);
		font-weight: 700;
	}
	.qcc-breakdown {
		font-size: 12px;
		color: var(--text-muted);
		font-family: var(--font-body);
		margin-top: var(--space-xs);
	}

	.quests-section {
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.qs-title {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		color: var(--text-primary);
		margin: 0;
	}

	.quests-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.quest-row {
		min-height: auto;
		opacity: 0.65;
	}
	.fc-quest-desc {
		font-size: var(--font-body-size);
		color: var(--text-secondary);
		font-family: var(--font-body);
		line-height: 1.4;
	}
	.quest-row.is-done {
		opacity: 1;
		border-color: var(--accent-cyan);
	}
</style>
