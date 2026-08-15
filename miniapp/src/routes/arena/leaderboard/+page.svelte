<script lang="ts">
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';

	type Row = { rank: number; user_id: number; name: string | null; rating: number; wins: number; total_matches: number; is_me: boolean };
	let rows = $state<Row[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	onMount(async () => {
		try { rows = await apiFetch<Row[]>('/api/v1/arena/leaderboard?limit=50'); }
		catch (value) { error = value instanceof ApiError ? value.message : 'Не удалось загрузить рейтинг Arena.'; }
		finally { loading = false; }
	});
</script>

<div class="arena-leaderboard">
	<div class="leader-head"><div><div class="kicker">YUVI ARENA · РЕЙТИНГ</div><h1>Кто здесь босс?</h1><p>Общий рейтинг за все PvP-бои</p></div><div class="leader-mark">#1</div></div>
	{#if loading}<div class="screen-loading"><span>считаем рейтинг…</span></div>
	{:else if error}<div class="leader-error" role="alert">{error}</div>
	{:else if rows.length === 0}<div class="leader-empty"><div>☆</div><strong>Рейтинг пока пуст</strong><span>Сыграй первый матч и займи вершину.</span></div>
	{:else}<div class="leader-list">{#each rows as row (row.user_id)}<div class:me={row.is_me} class="leader-row"><span class={`leader-rank rank-${Math.min(row.rank, 3)}`}>{row.rank}</span><span class="leader-avatar">{(row.name ?? `id${row.user_id}`).slice(0, 1).toUpperCase()}</span><span class="leader-user"><strong>{row.name ?? `id${row.user_id}`}{#if row.is_me}<small>ты</small>{/if}</strong><span>{row.wins} побед · {row.total_matches} боёв</span></span><span class="leader-rating">{row.rating}<small>R</small></span></div>{/each}</div>{/if}
</div>

<style>
	.arena-leaderboard { padding: 24px 18px 40px; display: flex; flex-direction: column; gap: var(--space-md); }
	.leader-head { display: flex; justify-content: space-between; gap: var(--space-md); } .kicker { color: var(--accent-yellow); font: 10px var(--font-body); letter-spacing: .14em; text-transform: uppercase; } h1 { margin: 3px 0 0; color: var(--text-primary); font: 700 var(--font-display-size) var(--font-chrome); } .leader-head p { margin: 5px 0 0; color: var(--text-muted); font: 13px var(--font-body); }
	.leader-mark { width: 54px; height: 54px; display: grid; place-items: center; color: var(--accent-yellow); border: 2px solid var(--accent-yellow); border-radius: 50%; font: 22px var(--font-numeric); transform: rotate(8deg); }
	.leader-list { display: flex; flex-direction: column; gap: var(--space-sm); } .leader-row { display: grid; grid-template-columns: 26px 36px 1fr auto; align-items: center; gap: var(--space-sm); min-height: 66px; padding: 10px 12px; background: var(--bg-secondary-2); border: 1px solid var(--border-secondary); border-radius: 12px; } .leader-row.me { border-color: var(--accent-pink); box-shadow: 0 0 18px rgba(255,91,141,.1); }
	.leader-rank { color: var(--text-muted); font: 18px var(--font-numeric); text-align: center; } .leader-rank.rank-1 { color: var(--accent-yellow); } .leader-rank.rank-2 { color: #d2d2df; } .leader-rank.rank-3 { color: #d49156; }
	.leader-avatar { width: 34px; height: 34px; display: grid; place-items: center; color: var(--text-primary); background: linear-gradient(135deg, var(--accent-cyan), var(--accent-pink)); border-radius: 50%; font: 700 15px var(--font-chrome); } .leader-user { min-width: 0; display: flex; flex-direction: column; gap: 3px; } .leader-user strong { display: flex; align-items: center; gap: 6px; overflow: hidden; color: var(--text-primary); font: 13px var(--font-chrome); text-overflow: ellipsis; white-space: nowrap; } .leader-user strong small { flex-shrink: 0; padding: 2px 5px; color: #1a0f12; background: var(--accent-pink); border-radius: 999px; font: 10px var(--font-body); } .leader-user > span { color: var(--text-muted); font: 11px var(--font-body); }
	.leader-rating { color: var(--accent-yellow); font: 24px var(--font-numeric); } .leader-rating small { margin-left: 2px; color: var(--text-muted); font: 11px var(--font-body); }
	.leader-empty { padding: 55px 20px; display: flex; flex-direction: column; align-items: center; gap: 8px; color: var(--text-muted); background: var(--bg-secondary-2); border: 1px dashed var(--border-secondary); border-radius: 14px; text-align: center; font: 13px var(--font-body); } .leader-empty > div { color: var(--accent-yellow); font-size: 36px; } .leader-empty strong { color: var(--text-primary); font: 17px var(--font-chrome); }
	.leader-error { padding: 12px 16px; color: var(--destructive-text); background: var(--destructive-bg); border-radius: 8px; font: 13px var(--font-body); }
</style>
