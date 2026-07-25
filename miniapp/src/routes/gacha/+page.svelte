<script lang="ts">
	// Gacha — roll ×1/×10 + tier-list collection + world map (GACHA-01,
	// visual half of GACHA-03/GACHA-04). Visual language ported from the
	// design prototype (deploy/index.html §Гача): real tab switcher
	// (Баннер/Коллекция/Карта — three stateful buttons swapping a panel in
	// place, not a route jump or a dropdown), hero banner card with
	// full-bleed portrait art + gradient scrims + particle sparkles, a
	// collection grid that ALWAYS renders the full 15-character roster
	// (locked characters get a tier-colored wash + "?" instead of an empty
	// "пока пусто" text), and a region map (Yuviteria Codex §"Материк ·
	// Регионы" — 5 real geographic regions + a "Легенды" group for the 4
	// heroines with no fixed homeland: the wanderer Селин and the three
	// cosmic-lineage UUR heroines).
	//
	// Server is the sole source of truth for every roll outcome
	// (gacha_service.roll, D-03) — this screen only renders whatever
	// POST /gacha/roll returns. Roll results carry only char_id/tier/stars
	// (roll()/​_grant()/​_apply_dupe() are untouched, per 04.2-05-PLAN.md) —
	// character names/art come from GET /gacha/collection's `roster` (full
	// catalog, GACHA-04), so the collection is always re-fetched right
	// after a roll: the freshly-granted character is guaranteed to already
	// be owned=true in the roster.
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	const ROLL_COST = 300;
	const ROLL10_COST = 2700;
	const PITY_UR = 50;
	const PITY_UUR = 90;
	const TIER_ORDER = ['UUR', 'UR', 'S', 'R'] as const;

	type Tier = 'R' | 'S' | 'UR' | 'UUR';
	type GachaView = 'banner' | 'collection' | 'map';
	type RosterChar = {
		char_id: string;
		name: string;
		tier: Tier;
		art_slug: string;
		owned: boolean;
		stars: number;
		copies: number;
		const_level: number;
	};
	type CollectionState = {
		characters: unknown[];
		roster: RosterChar[];
		pity_ssr: number;
		pity_ur: number;
		banner: string;
	};
	type RollGrant = { char_id: string; tier: Tier; stars: number; refunded: number };
	type RollResult = {
		cost: number;
		results: RollGrant[];
		replay?: boolean;
		user_balance_after: number;
	};

	// Регионы Ювитерии (Yuviteria Codex §"Материк · Регионы") — чисто
	// презентационная группировка для вкладки "Карта", цвета и названия
	// взяты дословно из кодекса. "Легенды" — не официальный регион кодекса,
	// а сборная группа для 4 героинь без фиксированной родины (странница
	// Селин + три UUR с космической, а не географической, принадлежностью).
	type RegionKey = 'falkonia' | 'ignaria' | 'frostheim' | 'ardaria' | 'ferals' | 'legends';
	const REGIONS: { key: RegionKey; name: string; tag: string; color: string }[] = [
		{ key: 'falkonia', name: 'Фалкония', tag: '«Сердце человечества»', color: '#4a5f8a' },
		{ key: 'ignaria', name: 'Игнария', tag: '«Земля Вечного Пламени»', color: '#ff7a2e' },
		{ key: 'frostheim', name: 'Фростхейм', tag: '«Королевство вечной зимы»', color: '#8fe0ff' },
		{ key: 'ardaria', name: 'Ардария', tag: '«Пески Мёртвых»', color: '#caa43d' },
		{ key: 'ferals', name: 'Фералы', tag: '«Дети дикой природы»', color: '#5c8a3f' },
		{ key: 'legends', name: 'Легенды', tag: 'вне регионов — странники и полукровки', color: '#c9a8ff' }
	];
	const CHAR_REGION: Record<string, RegionKey> = {
		r_elis: 'falkonia',
		r_freya: 'falkonia',
		r_selin: 'legends',
		r_sofia: 'falkonia',
		r_nora: 'falkonia',
		s_ignis: 'ignaria',
		s_astrid: 'frostheim',
		s_amira: 'ardaria',
		s_luna: 'falkonia',
		ur_iris: 'falkonia',
		ur_yuna: 'ferals',
		ur_mia: 'ferals',
		uur_astrea: 'legends',
		uur_eliana: 'legends',
		uur_mara: 'legends'
	};

	let view = $state<GachaView>('banner');
	let loading = $state(true);
	let error = $state<string | null>(null);
	let rolling = $state(false);
	let reveal = $state<RollGrant[] | null>(null);

	let roster = $state<RosterChar[]>([]);
	let pitySsr = $state(0);
	let pityUr = $state(0);
	let bannerId = $state('');

	let byId = $derived(new Map(roster.map((c) => [c.char_id, c])));
	let ownedCount = $derived(roster.filter((c) => c.owned).length);
	// Настоящий рейт-ап баннер (если админ его настроил через gacha_banner
	// BotSetting) — иначе "витринный" топ-тир персонаж (первый UUR
	// ростера), чтобы карточка баннера никогда не была пустой. Разница
	// честно отражена в подписи (hasRealBanner) — без rate-up мы не
	// утверждаем, что у витринного персонажа реально повышен шанс.
	let hasRealBanner = $derived(bannerId !== '');
	let bannerChar = $derived(
		byId.get(bannerId) ?? roster.find((c) => c.tier === 'UUR') ?? roster[0] ?? null
	);
	let grouped = $derived(
		TIER_ORDER.map((tier) => ({ tier, chars: roster.filter((c) => c.tier === tier) })).filter(
			(g) => g.chars.length > 0
		)
	);
	let mapGroups = $derived(
		REGIONS.map((reg) => ({
			...reg,
			chars: roster.filter((c) => CHAR_REGION[c.char_id] === reg.key)
		})).filter((g) => g.chars.length > 0)
	);

	function describeError(err: unknown): string {
		return err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
	}

	async function loadCollection() {
		try {
			const res = await apiFetch<CollectionState>('/api/v1/gacha/collection');
			roster = res.roster;
			pitySsr = res.pity_ssr;
			pityUr = res.pity_ur;
			bannerId = res.banner;
		} catch (err) {
			error = describeError(err);
		} finally {
			loading = false;
		}
	}

	async function roll(count: 1 | 10) {
		if (rolling) return;
		rolling = true;
		error = null;
		reveal = null;
		try {
			const res = await apiFetch<RollResult>('/api/v1/gacha/roll', {
				method: 'POST',
				body: JSON.stringify({ count, ref_id: `gacha_roll:${crypto.randomUUID()}` })
			});
			await loadCollection();
			reveal = res.results;
			const hasUur = res.results.some((r) => r.tier === 'UUR');
			haptic(hasUur ? 'big-win' : 'win');
		} catch (err) {
			error = describeError(err);
			haptic('error');
		} finally {
			rolling = false;
		}
	}

	function charName(charId: string): string {
		return byId.get(charId)?.name ?? charId;
	}

	function charArtSlug(charId: string): string | undefined {
		return byId.get(charId)?.art_slug;
	}

	onMount(() => {
		loadCollection();
	});
</script>

{#if loading}
	<div class="screen-loading"><span>загрузка баннера…</span></div>
{:else}
	<div class="gacha-screen">
		<div class="menu-head">
			<h1 class="menu-title">Гача</h1>
			<div class="menu-sub">крути баннер, собирай тир-лист</div>
		</div>

		{#if error}
			<div class="cf-error">{error}</div>
		{/if}

		<div class="gacha-tabs">
			<button
				type="button"
				class="gacha-tab"
				class:gacha-tab-active={view === 'banner'}
				onclick={() => (view = 'banner')}
			>
				Баннер
			</button>
			<button
				type="button"
				class="gacha-tab"
				class:gacha-tab-active={view === 'collection'}
				onclick={() => (view = 'collection')}
			>
				Коллекция · {ownedCount}/{roster.length}
			</button>
			<button
				type="button"
				class="gacha-tab"
				class:gacha-tab-active={view === 'map'}
				onclick={() => (view = 'map')}
			>
				Карта
			</button>
		</div>

		{#if view === 'banner'}
			<div class="gacha-hero">
				<div class="gacha-hero-inner">
					{#if bannerChar}
						<img src="/art/heroines/{bannerChar.art_slug}.webp" alt="" class="gacha-hero-art" />
					{/if}
					<div class="gacha-hero-scrim"></div>
					<div class="gacha-hero-glow"></div>
					<span class="gacha-hero-particle gacha-hero-particle-1"></span>
					<span class="gacha-hero-particle gacha-hero-particle-2"></span>
					<span class="gacha-hero-particle gacha-hero-particle-3"></span>
					{#if bannerChar}
						<div class={`gacha-hero-badge-left gacha-tier-${bannerChar.tier.toLowerCase()}`}>
							{bannerChar.tier} · {hasRealBanner ? 'РЕЙТ-АП' : 'ОСОБЫЙ ГЕРОЙ'}
						</div>
						<div class={`gacha-hero-badge-right gacha-tier-pill-${bannerChar.tier.toLowerCase()}`}>
							{bannerChar.tier}
						</div>
					{/if}
					<div class="gacha-hero-text">
						<div class="gacha-hero-kicker">{hasRealBanner ? 'Лимитный баннер' : 'Топ-тир'}</div>
						<div class="gacha-hero-name">{bannerChar ? bannerChar.name : 'баннер не выбран'}</div>
					</div>
				</div>
			</div>

			{#if reveal}
				<div class="gacha-reveal">
					{#each reveal as grant (grant.char_id + ':' + grant.stars + ':' + grant.refunded)}
						<div class={`gacha-reveal-card gacha-tier-${grant.tier.toLowerCase()}`}>
							{#if charArtSlug(grant.char_id)}
								<img
									src="/art/heroines/{charArtSlug(grant.char_id)}.webp"
									alt=""
									class="gacha-reveal-portrait"
								/>
							{/if}
							<div class="gacha-reveal-tier">{grant.tier}</div>
							<div class="gacha-reveal-name">{charName(grant.char_id)}</div>
							<div class="gacha-reveal-stars">{'★'.repeat(grant.stars)}</div>
							{#if grant.refunded > 0}
								<div class="gacha-reveal-dupe">дубль сверх 5★ — рефанд +{grant.refunded}¥</div>
							{/if}
						</div>
					{/each}
				</div>
			{/if}

			<div class="gacha-roll-row">
				<button
					type="button"
					class="chip gacha-roll-btn"
					disabled={rolling}
					onclick={() => roll(1)}
				>
					{rolling ? '…' : `×1 (${ROLL_COST}¥)`}
				</button>
				<button
					type="button"
					class="chip chip-all gacha-roll-btn"
					disabled={rolling}
					onclick={() => roll(10)}
				>
					{rolling ? '…' : `×10 (${ROLL10_COST}¥)`}
				</button>
			</div>

			<div class="gacha-pity">
				<span>пити UR: {pitySsr}/{PITY_UR}</span>
				<span>пити UUR: {pityUr}/{PITY_UUR}</span>
			</div>
		{:else if view === 'collection'}
			<div class="gacha-collection">
				{#each grouped as group (group.tier)}
					<div class="gacha-tier-group">
						<div class="gacha-tier-group-head">
							<span class={`gacha-tier-pill gacha-tier-pill-${group.tier.toLowerCase()}`}>
								{group.tier}
							</span>
							<span class="gacha-tier-count">
								{group.chars.filter((c) => c.owned).length}/{group.chars.length}
							</span>
						</div>
						<div class="gacha-tier-grid">
							{#each group.chars as char (char.char_id)}
								<div class="gacha-roster-card" class:gacha-roster-locked={!char.owned}>
									<div class={`gacha-roster-art gacha-tier-wash-${char.tier.toLowerCase()}`}>
										{#if char.owned}
											<img src="/art/heroines/{char.art_slug}.webp" alt="" />
										{:else}
											<span class="gacha-roster-lock">?</span>
										{/if}
									</div>
									<div class="gacha-roster-footer">
										<div class="gacha-roster-name">{char.owned ? char.name : '???'}</div>
										<div class="gacha-roster-stars">
											{char.owned ? '★'.repeat(char.stars) : '—'}
											{#if char.owned && char.copies > 1}
												<span class="gacha-roster-copies">×{char.copies}</span>
											{/if}
										</div>
									</div>
								</div>
							{/each}
						</div>
					</div>
				{/each}
			</div>
		{:else}
			<div class="gacha-map">
				<div class="gacha-map-intro">
					Ювитерия — материк, где живут героини. Регионы материка и легенды за его пределами.
				</div>
				{#each mapGroups as group (group.key)}
					<div class="gacha-region-card" style="border-left-color: {group.color}">
						<div class="gacha-region-head">
							<div class="gacha-region-name">{group.name}</div>
							<div class="gacha-region-tag">{group.tag}</div>
						</div>
						<div class="gacha-region-members">
							{#each group.chars as char (char.char_id)}
								<div
									class="gacha-region-pill"
									class:gacha-region-pill-locked={!char.owned}
									style="border-color: {group.color}"
								>
									<span class={`gacha-region-pill-dot gacha-tier-wash-${char.tier.toLowerCase()}`}
									></span>
									<span class="gacha-region-pill-name">{char.owned ? char.name : '???'}</span>
								</div>
							{/each}
						</div>
					</div>
				{/each}
			</div>
		{/if}
	</div>
{/if}

<style>
	.gacha-screen {
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

	/* ─── tab switcher (design-прототип: real state, not a route jump) ──── */
	.gacha-tabs {
		display: flex;
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 10px;
		padding: 3px;
		gap: 2px;
	}
	.gacha-tab {
		flex: 1;
		background: transparent;
		color: var(--text-muted);
		border: none;
		border-radius: 7px;
		padding: 9px;
		font-family: var(--font-chrome);
		font-size: 12.5px;
		font-weight: 700;
		cursor: pointer;
	}
	.gacha-tab-active {
		background: var(--border-secondary);
		color: var(--text-primary);
	}

	/* ─── hero banner card (design-прототип §Гача: full-bleed art + scrims + particles) ── */
	.gacha-hero {
		border-radius: 18px;
		overflow: hidden;
		border: 2px solid var(--accent-yellow);
		box-shadow: 0 0 34px rgba(255, 216, 74, 0.35);
	}
	.gacha-hero-inner {
		position: relative;
		min-height: 300px;
		display: flex;
		flex-direction: column;
		justify-content: flex-end;
		padding: var(--space-lg) var(--space-md) var(--space-md);
		background: linear-gradient(160deg, #170f2e, #0d0821 45%, #3d1e6b 78%, #a175ff 150%);
		overflow: hidden;
	}
	.gacha-hero-art {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		object-fit: cover;
		object-position: top center;
	}
	.gacha-hero-scrim {
		position: absolute;
		inset: 0;
		pointer-events: none;
		background: linear-gradient(
			to top,
			rgba(9, 6, 20, 0.95) 0%,
			rgba(9, 6, 20, 0.6) 26%,
			rgba(9, 6, 20, 0.05) 58%,
			rgba(60, 30, 110, 0.3) 100%
		);
	}
	.gacha-hero-glow {
		position: absolute;
		inset: 0;
		pointer-events: none;
		background: radial-gradient(circle at 50% 30%, rgba(161, 117, 255, 0.28), transparent 66%);
	}
	.gacha-hero-particle {
		position: absolute;
		border-radius: 50%;
		background: var(--accent-yellow);
		width: 5px;
		height: 5px;
		animation: gachaParticle 2.2s ease-in infinite;
	}
	.gacha-hero-particle-1 {
		left: 38%;
		top: 34%;
	}
	.gacha-hero-particle-2 {
		left: 60%;
		top: 38%;
		width: 4px;
		height: 4px;
		background: #fff;
		animation-duration: 2.6s;
		animation-delay: 0.5s;
	}
	.gacha-hero-particle-3 {
		left: 50%;
		top: 30%;
		width: 3px;
		height: 3px;
		background: var(--accent-cyan);
		animation-duration: 1.9s;
		animation-delay: 1s;
	}
	@keyframes gachaParticle {
		0% {
			transform: translateY(20px) scale(0.4);
			opacity: 0;
		}
		40% {
			opacity: 1;
		}
		100% {
			transform: translateY(-120px) scale(1.1);
			opacity: 0;
		}
	}
	.gacha-hero-badge-left,
	.gacha-hero-badge-right {
		position: absolute;
		top: 16px;
		font-family: var(--font-chrome);
	}
	.gacha-hero-badge-left {
		left: 16px;
		font-size: 10px;
		letter-spacing: 0.1em;
		background: rgba(0, 0, 0, 0.35);
		padding: 4px 10px;
		border-radius: 999px;
	}
	.gacha-hero-badge-right {
		right: 16px;
		font-family: var(--font-numeric);
		font-size: 12px;
		font-weight: 900;
		color: #0d0a18;
		padding: 4px 10px;
		border-radius: 6px;
	}
	.gacha-hero-text {
		position: relative;
	}
	.gacha-hero-kicker {
		font-family: var(--font-chrome);
		font-size: 10.5px;
		letter-spacing: 0.12em;
		color: var(--accent-yellow);
		text-transform: uppercase;
	}
	.gacha-hero-name {
		font-family: var(--font-chrome);
		font-size: 30px;
		font-weight: 700;
		color: #fff;
		margin-top: 4px;
	}

	.gacha-reveal {
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.gacha-reveal-card {
		position: relative;
		overflow: hidden;
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-md);
		text-align: center;
	}
	.gacha-reveal-portrait {
		width: 100%;
		height: 140px;
		object-fit: cover;
		object-position: top center;
		border-radius: 10px;
		margin-bottom: var(--space-sm);
		animation: gachaPortraitReveal 0.35s ease-out;
	}
	@keyframes gachaPortraitReveal {
		from {
			opacity: 0;
			transform: scale(0.85);
		}
		to {
			opacity: 1;
			transform: scale(1);
		}
	}
	.gacha-reveal-tier {
		position: relative;
		font-family: var(--font-numeric);
		font-size: var(--font-label-size);
		font-weight: 900;
		letter-spacing: 0.08em;
	}
	.gacha-reveal-name {
		position: relative;
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		color: var(--text-primary);
		margin-top: var(--space-xs);
	}
	.gacha-reveal-stars {
		position: relative;
		font-size: var(--font-heading-size);
		color: var(--accent-yellow);
		margin-top: var(--space-xs);
	}
	.gacha-reveal-dupe {
		position: relative;
		font-size: 12px;
		color: var(--text-muted);
		font-family: var(--font-body);
		margin-top: var(--space-xs);
	}

	/* Rarity color-coding (04-UI-SPEC.md §Component Inventory): R=neutral,
	   S=cyan, UR=pink, UUR=yellow glow (Hero/Impact reveal tier
	   specifically on UUR pulls). Reused for hero badges/tier pills below. */
	.gacha-tier-r {
		border-color: #9b97ad;
	}
	.gacha-tier-r .gacha-reveal-tier,
	.gacha-hero-badge-left.gacha-tier-r {
		color: #9b97ad;
	}
	.gacha-tier-s {
		border-color: var(--accent-cyan);
	}
	.gacha-tier-s .gacha-reveal-tier,
	.gacha-hero-badge-left.gacha-tier-s {
		color: var(--accent-cyan);
	}
	.gacha-tier-ur {
		border-color: var(--accent-pink);
	}
	.gacha-tier-ur .gacha-reveal-tier,
	.gacha-hero-badge-left.gacha-tier-ur {
		color: var(--accent-pink);
	}
	.gacha-tier-uur {
		border-color: var(--accent-yellow);
		box-shadow: 0 0 24px rgba(255, 216, 74, 0.45);
	}
	.gacha-tier-uur .gacha-reveal-tier,
	.gacha-hero-badge-left.gacha-tier-uur {
		color: var(--accent-yellow);
	}
	.gacha-tier-pill-r,
	.gacha-hero-badge-right.gacha-tier-pill-r {
		background: #9b97ad;
	}
	.gacha-tier-pill-s,
	.gacha-hero-badge-right.gacha-tier-pill-s {
		background: var(--accent-cyan);
	}
	.gacha-tier-pill-ur,
	.gacha-hero-badge-right.gacha-tier-pill-ur {
		background: var(--accent-pink);
	}
	.gacha-tier-pill-uur,
	.gacha-hero-badge-right.gacha-tier-pill-uur {
		background: var(--accent-yellow);
	}
	/* Hero-tier reveal treatment on UUR pulls specifically (04-UI-SPEC.md
	   line 68: gacha UUR pull reveal is a "jackpot theater" Hero moment). */
	.gacha-reveal-card.gacha-tier-uur .gacha-reveal-name {
		font-family: var(--font-numeric);
		font-size: var(--font-hero-size);
		font-weight: 900;
	}

	.gacha-roll-row {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--space-sm);
	}
	.gacha-roll-btn {
		padding: var(--space-md);
		font-size: var(--font-heading-size);
		text-align: center;
	}

	.gacha-pity {
		display: flex;
		justify-content: space-between;
		font-size: var(--font-label-size);
		color: var(--text-muted);
		font-family: var(--font-body);
		letter-spacing: 0.04em;
	}

	/* ─── collection: full roster grid, locked heroines get wash + "?" ──── */
	.gacha-collection {
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.gacha-tier-group {
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.gacha-tier-group-head {
		display: flex;
		align-items: center;
		gap: var(--space-sm);
	}
	.gacha-tier-pill {
		font-family: var(--font-numeric);
		font-size: 11px;
		font-weight: 900;
		letter-spacing: 0.04em;
		padding: 3px 9px;
		border-radius: 6px;
		color: #0d0a18;
	}
	.gacha-tier-count {
		font-family: var(--font-body);
		font-size: 11px;
		color: var(--text-muted);
	}
	.gacha-tier-grid {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: var(--space-sm);
	}
	.gacha-roster-card {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 10px;
		overflow: hidden;
		transition: opacity 0.15s;
	}
	.gacha-roster-locked {
		opacity: 0.55;
	}
	.gacha-roster-art {
		height: 72px;
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		overflow: hidden;
		animation: gachaRosterBreathe 4.8s ease-in-out infinite;
	}
	.gacha-roster-art img {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		object-fit: cover;
		object-position: top center;
	}
	.gacha-tier-wash-r {
		background: linear-gradient(135deg, var(--bg-secondary-1), #9b97ad);
	}
	.gacha-tier-wash-s {
		background: linear-gradient(135deg, var(--bg-secondary-1), var(--accent-cyan));
	}
	.gacha-tier-wash-ur {
		background: linear-gradient(135deg, var(--bg-secondary-1), var(--accent-pink));
	}
	.gacha-tier-wash-uur {
		background: linear-gradient(135deg, var(--bg-secondary-1), var(--accent-yellow));
	}
	@keyframes gachaRosterBreathe {
		0%,
		100% {
			transform: scale(1) translateY(0);
		}
		50% {
			transform: scale(1.035) translateY(-2px);
		}
	}
	.gacha-roster-lock {
		position: relative;
		font-family: var(--font-numeric);
		font-size: 20px;
		color: rgba(255, 255, 255, 0.55);
	}
	.gacha-roster-footer {
		padding: 6px 7px;
	}
	.gacha-roster-name {
		font-family: var(--font-chrome);
		font-size: 10.5px;
		font-weight: 700;
		color: var(--text-primary);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.gacha-roster-stars {
		font-family: var(--font-numeric);
		font-size: 9px;
		color: var(--accent-yellow);
		margin-top: 1px;
	}
	.gacha-roster-copies {
		color: var(--text-muted);
		margin-left: 4px;
	}

	/* ─── map: region cards with member pills (Yuviteria Codex §Регионы) ── */
	.gacha-map {
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.gacha-map-intro {
		font-family: var(--font-body);
		font-size: 11.5px;
		color: var(--text-muted);
		line-height: 1.5;
	}
	.gacha-region-card {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-left: 3px solid;
		border-radius: 12px;
		padding: 14px 16px;
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.gacha-region-name {
		font-family: var(--font-chrome);
		font-size: 15px;
		font-weight: 700;
		color: var(--text-primary);
	}
	.gacha-region-tag {
		font-family: var(--font-body);
		font-size: 11px;
		color: var(--text-muted);
	}
	.gacha-region-members {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}
	.gacha-region-pill {
		display: flex;
		align-items: center;
		gap: 6px;
		background: var(--bg-dominant);
		border: 1px solid;
		border-radius: 999px;
		padding: 4px 10px 4px 4px;
	}
	.gacha-region-pill-locked {
		opacity: 0.55;
	}
	.gacha-region-pill-dot {
		width: 18px;
		height: 18px;
		border-radius: 50%;
		display: block;
	}
	.gacha-region-pill-name {
		font-family: var(--font-chrome);
		font-size: 11.5px;
		color: var(--text-primary);
	}
</style>
