<script lang="ts">
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';

	type FighterProgress = { fighter: string; xp: number; level: number; xp_to_next_level: number | null };
	type Profile = { rating: number; total_matches: number; wins: number; losses: number; draws: number; technical_wins: number; technical_losses: number; knockouts: number; fighters: FighterProgress[] };
	const names: Record<string, string> = { tank: 'Танк', assassin: 'Ассасин', berserker: 'Берсерк', tactician: 'Тактик' };
	let profile = $state<Profile | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);

	onMount(async () => {
		try { profile = await apiFetch<Profile>('/api/v1/arena/profile'); }
		catch (value) { error = value instanceof ApiError ? value.message : 'Не удалось загрузить профиль Arena.'; }
		finally { loading = false; }
	});
</script>

<div class="arena-profile">
	<div class="profile-head"><div><div class="kicker">YUVI ARENA · ПРОФИЛЬ</div><h1>Твой бойцовский след</h1><p>Рейтинг общий, прогресс — у каждого бойца свой</p></div><div class="profile-mark">VS</div></div>
	{#if loading}<div class="screen-loading"><span>загрузка профиля…</span></div>
	{:else if error}<div class="profile-error" role="alert">{error}</div>
	{:else if profile}<div class="rating-card"><span>ARENA RATING</span><strong>{profile.rating}</strong><small>стартовый рейтинг — 1000</small></div><div class="stats-grid"><div><strong>{profile.total_matches}</strong><span>боёв</span></div><div><strong>{profile.wins}</strong><span>побед</span></div><div><strong>{profile.losses}</strong><span>поражений</span></div><div><strong>{profile.draws}</strong><span>ничьих</span></div></div><section class="profile-panel"><h2>Прогресс бойцов</h2>{#if profile.fighters.length === 0}<div class="profile-empty">XP появится после первого завершённого боя.</div>{:else}<div class="fighter-list">{#each profile.fighters as fighter (fighter.fighter)}<div class="fighter-progress"><div><strong>{names[fighter.fighter] ?? fighter.fighter}</strong><span>уровень {fighter.level}</span></div>{#if fighter.xp_to_next_level === null}<div class="xp-row"><span style="width: 100%"></span></div><small>максимальный уровень</small>{:else}<div class="xp-row"><span style={`width: ${Math.min(100, (fighter.xp / fighter.xp_to_next_level) * 100)}%`}></span></div><small>{fighter.xp} / {fighter.xp_to_next_level} XP до следующего уровня</small>{/if}</div>{/each}</div>{/if}</section>{/if}
</div>

<style>
	.arena-profile { padding: 24px 18px 40px; display: flex; flex-direction: column; gap: var(--space-md); } .profile-head { display: flex; justify-content: space-between; gap: var(--space-md); } .kicker { color: var(--accent-yellow); font: 10px var(--font-body); letter-spacing: .14em; text-transform: uppercase; } h1 { margin: 3px 0 0; color: var(--text-primary); font: 700 var(--font-display-size) var(--font-chrome); } .profile-head p { margin: 5px 0 0; color: var(--text-muted); font: 13px var(--font-body); } .profile-mark { width: 54px; height: 54px; display: grid; place-items: center; color: var(--accent-pink); border: 2px solid var(--accent-pink); border-radius: 50%; font: 22px var(--font-numeric); transform: rotate(-8deg); }
	.rating-card { padding: 20px; display: flex; flex-direction: column; gap: 5px; background: linear-gradient(135deg, rgba(255,216,74,.14), var(--bg-secondary-2)); border: 1px solid rgba(255,216,74,.45); border-radius: 16px; } .rating-card span { color: var(--accent-yellow); font: 10px var(--font-body); letter-spacing: .15em; } .rating-card strong { color: var(--text-primary); font: 58px var(--font-numeric); line-height: 1; } .rating-card small { color: var(--text-muted); font: 11px var(--font-body); }
	.stats-grid { display: grid; grid-template-columns: repeat(4,1fr); gap: 5px; } .stats-grid div { min-height: 70px; padding: 9px 5px; display: flex; flex-direction: column; justify-content: center; gap: 2px; background: var(--bg-secondary-2); border: 1px solid var(--border-secondary); border-radius: 10px; text-align: center; } .stats-grid strong { color: var(--text-primary); font: 25px var(--font-numeric); } .stats-grid span { color: var(--text-muted); font: 10px var(--font-body); }
	.profile-panel { padding: var(--space-md); background: var(--bg-secondary-2); border: 1px solid var(--border-secondary); border-radius: 14px; } h2 { margin: 0 0 var(--space-md); color: var(--text-primary); font: 17px var(--font-chrome); } .profile-empty { color: var(--text-muted); font: 13px var(--font-body); } .fighter-list { display: flex; flex-direction: column; gap: var(--space-md); } .fighter-progress { display: flex; flex-direction: column; gap: 6px; } .fighter-progress > div:first-child { display: flex; justify-content: space-between; gap: var(--space-sm); } .fighter-progress strong { color: var(--text-primary); font: 14px var(--font-chrome); } .fighter-progress span, .fighter-progress small { color: var(--text-muted); font: 11px var(--font-body); } .xp-row { height: 8px; overflow: hidden; background: var(--bg-dominant); border-radius: 5px; } .xp-row span { display: block; height: 100%; background: linear-gradient(90deg, var(--accent-cyan), var(--accent-pink)); border-radius: inherit; }
	.profile-error { padding: 12px 16px; color: var(--destructive-text); background: var(--destructive-bg); border-radius: 8px; font: 13px var(--font-body); }
</style>
