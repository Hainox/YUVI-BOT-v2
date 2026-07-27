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
	import { onMount, onDestroy } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	const ROLL_COST = 300;
	const ROLL10_COST = 2700;
	const PITY_UR = 50;
	const PITY_UUR = 90;
	const TIER_ORDER = ['UUR', 'UR', 'S', 'R'] as const;

	type Tier = 'R' | 'S' | 'UR' | 'UUR';
	// Ранг тира для сравнений "что лучше" — используется, чтобы выбрать
	// цвет разрыва/вспышки анимации ролла и порядок раскрытия карточек в x10.
	const TIER_RANK: Record<Tier, number> = { R: 0, S: 1, UR: 2, UUR: 3 };
	type GachaView = 'banner' | 'collection' | 'map';
	// Фазы кинематографичной анимации ролла: заряд → разрыв → вспышка →
	// раскрытие. 'idle' — анимация не идёт, обычный экран баннера.
	type RollPhase = 'idle' | 'charge' | 'rift' | 'flash' | 'reveal';
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

	// ── Состояние анимации ролла (заряд/разрыв/вспышка/раскрытие) ──────────
	let rollPhase = $state<RollPhase>('idle');
	let pullCount = $state<1 | 10>(1);
	// Реальный результат сервера. Пока null — сервер ещё не ответил, поэтому
	// цвет разрыва/вспышки нейтральный, а не угаданный.
	let pullResults = $state<RollGrant[] | null>(null);
	// Игрок тапнул по анимации до того, как пришёл ответ сервера — как
	// только ответ придёт, сразу переходим в reveal, не дожидаясь таймеров.
	let skipRequested = false;
	let resolveAnimation: (() => void) | null = null;
	let rollTimers: ReturnType<typeof setTimeout>[] = [];

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

	// Лучший тир в текущем ролле — красит разрыв/вспышку (честный "тизер"
	// результата, как в дизайн-прототипе). Пока сервер не ответил — null,
	// и оверлей остаётся нейтрально-серым, а не подделанным цветом.
	let pullBestTier = $derived(pullResults ? bestTierOf(pullResults) : null);

	// Орбы фазы "заряд": x1 — 4 орбы по 90°, x10 — 10 орбит по 36°, как в
	// прототипе (chargeOrbs, deploy/index.html:1245).
	let chargeOrbs = $derived.by(() => {
		const total = pullCount === 10 ? 10 : 4;
		return Array.from({ length: total }, (_, i) => ({
			id: i,
			angle: (i * (360 / total)).toFixed(1) + 'deg',
			delay: (i * 0.12).toFixed(2) + 's'
		}));
	});

	// Осколки фазы "разрыв": один на каждый результат ролла, цвет — по тиру
	// персонажа, так что на x10 игрок может "посчитать" золотые ещё до
	// раскрытия карточек (deploy/index.html:1252).
	let riftShards = $derived(
		(pullResults ?? []).map((r, i) => ({
			id: r.char_id + '-' + i,
			tier: r.tier,
			size: (r.tier === 'UUR' ? 11 : r.tier === 'UR' ? 9 : 7) + 'px',
			left: (14 + ((i * 8.6) % 72)).toFixed(1) + '%',
			delay: (0.06 + i * 0.075).toFixed(2) + 's',
			dur: (1.05 + (i % 3) * 0.12).toFixed(2) + 's'
		}))
	);

	// Порядок раскрытия карточек результата: по возрастанию тира (худшие
	// первыми), лучшая карточка — последней (deploy/index.html:1277). Для x1
	// сортировка не нужна — там всего одна карточка.
	let orderedReveal = $derived.by(() => {
		if (!reveal || reveal.length <= 1) return reveal ?? [];
		return reveal
			.map((grant, i) => ({ grant, i }))
			.sort((a, b) => TIER_RANK[a.grant.tier] - TIER_RANK[b.grant.tier] || a.i - b.i)
			.map((entry) => entry.grant);
	});

	function bestTierOf(results: RollGrant[]): Tier {
		return results.reduce<Tier>(
			(best, r) => (TIER_RANK[r.tier] > TIER_RANK[best] ? r.tier : best),
			'R'
		);
	}

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

	function clearRollTimers() {
		rollTimers.forEach(clearTimeout);
		rollTimers = [];
	}

	async function roll(count: 1 | 10) {
		// `rolling` — единственный флаг реентерабельности (перекрывает весь
		// цикл заряд→разрыв→вспышка→ожидание ответа, см. finally ниже).
		// rollPhase НЕ используется для этой проверки: после успешного ролла
		// он навсегда остаётся 'reveal' (карточки должны быть видны до
		// следующего ролла, п.10 задачи), а не возвращается в 'idle'.
		if (rolling) return;
		rolling = true;
		// ВЕСЬ корпус — внутри try/finally от старта до конца: сборка
		// fetchPromise/animationDone ниже тоже может синхронно бросить
		// (например apiFetch на плохом входе, crypto.randomUUID в редком
		// WebView) — раньше такой бросок происходил ДО try и оставлял
		// rolling=true навсегда без ошибки на экране (кнопки зависали в '…',
		// помогал только перезаход на экран). Теперь любой сбой на любом
		// шаге гарантированно освобождает rolling и показывает error.
		try {
			error = null;
			reveal = null;
			pullCount = count;
			pullResults = null;
			skipRequested = false;
			rollPhase = 'charge';
			// Таймеры прошлого ролла уже отработали к этому моменту (rolling
			// гарантирует, что новый roll() не стартует поверх незавершённого) —
			// сбрасываем массив, а не аккумулируем в нём отработавшие id вечно.
			rollTimers = [];

			// Тайминги — абсолютное смещение от старта ролла (не дельты между
			// фазами), verbatim из дизайн-прототипа: x1 короче и резче, x10 тратит
			// лишнюю секунду на осколки, которые игрок успевает пересчитать.
			const timing =
				count === 10 ? { rift: 900, flash: 1950, reveal: 2200 } : { rift: 800, flash: 1550, reveal: 1800 };

			const animationDone = new Promise<void>((resolve) => {
				resolveAnimation = resolve;
				rollTimers.push(
					setTimeout(() => {
						rollPhase = 'rift';
					}, timing.rift)
				);
				rollTimers.push(
					setTimeout(() => {
						rollPhase = 'flash';
					}, timing.flash)
				);
				rollTimers.push(setTimeout(resolve, timing.reveal));
			});

			// ОТКЛОНЕНИЕ ОТ ПРОТОТИПА (сознательное, см. задачу): в прототипе
			// результат ролла был известен мгновенно — мок-данные генерировались
			// локально ещё до старта таймеров, поэтому разрыв/вспышку можно было
			// красить под лучший тир сразу. Здесь результат — ответ реального
			// сервера (POST /api/v1/gacha/roll), который может прийти в любой
			// момент анимации. Поэтому reveal-фаза наступает только когда готовы
			// ОБА условия — Promise.all ждёт более медленное из двух: минимальную
			// длительность анимации (таймеры выше) и реальный ответ API. Ни разу
			// не показываем локально угаданный результат раньше настоящего.
			const fetchPromise = apiFetch<RollResult>('/api/v1/gacha/roll', {
				method: 'POST',
				body: JSON.stringify({ count, ref_id: 'gacha_roll:' + crypto.randomUUID() })
			}).then((res) => {
				pullResults = res.results;
				// Игрок тапнул "скип" ещё до ответа сервера — таймеры анимации уже
				// отменены в skipBuild(), поэтому сами будим reveal-фазу сейчас.
				if (skipRequested && resolveAnimation) {
					clearRollTimers();
					resolveAnimation();
					resolveAnimation = null;
				}
				return res;
			});

			const [res] = await Promise.all([fetchPromise, animationDone]);
			rollPhase = 'reveal';
			await loadCollection();
			reveal = res.results;
			const hasUur = res.results.some((r) => r.tier === 'UUR');
			haptic(hasUur ? 'big-win' : 'win');
		} catch (err) {
			clearRollTimers();
			rollPhase = 'idle';
			error = describeError(err);
			haptic('error');
		} finally {
			rolling = false;
			resolveAnimation = null;
		}
	}

	// Тап по анимации во время заряда/разрыва/вспышки — досрочно завершить
	// её. Если реальный результат уже пришёл — сразу раскрываем; если ещё
	// нет — ждём его (см. fetchPromise.then выше), но экрана ожидания без
	// анимации не показываем дольше, чем нужно.
	function skipBuild() {
		if (rollPhase !== 'charge' && rollPhase !== 'rift' && rollPhase !== 'flash') return;
		skipRequested = true;
		clearRollTimers();
		if (pullResults && resolveAnimation) {
			resolveAnimation();
			resolveAnimation = null;
		}
	}

	function handleSkipKeydown(event: KeyboardEvent) {
		if (event.key === 'Enter' || event.key === ' ') skipBuild();
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

	onDestroy(() => {
		clearRollTimers();
	});
</script>

{#if loading}
	<div class="screen-loading"><span>загрузка баннера…</span></div>
{:else}
	{#if rollPhase === 'charge' || rollPhase === 'rift' || rollPhase === 'flash'}
		<!-- Полноэкранный оверлей анимации ролла (заряд → разрыв → вспышка).
		     Тап в любом месте — досрочно пропустить (skipBuild). -->
		<div
			class={`gacha-pull-overlay gacha-charge-${(pullBestTier ?? 'pending').toLowerCase()}`}
			role="button"
			tabindex="0"
			onclick={skipBuild}
			onkeydown={handleSkipKeydown}
		>
			{#if rollPhase === 'charge'}
				<div class="gacha-charge-glow"></div>
				<div class="gacha-charge-ring-outer"></div>
				<div class="gacha-charge-ring-inner"></div>
				<div class="gacha-charge-core"></div>
				{#each chargeOrbs as orb (orb.id)}
					<div class="gacha-charge-orb-wrap" style="transform: rotate({orb.angle})">
						<div class="gacha-charge-orb" style="animation-delay: {orb.delay}"></div>
					</div>
				{/each}
				<div class="gacha-charge-label">
					{pullCount === 10 ? 'десять судеб сходятся…' : 'судьба сходится…'}
				</div>
			{:else if rollPhase === 'rift'}
				<div class="gacha-rift-glow"></div>
				<div class="gacha-rift-beam"></div>
				{#each riftShards as shard (shard.id)}
					<div
						class="gacha-rift-shard gacha-shard-{shard.tier.toLowerCase()}"
						style="left: {shard.left}; width: {shard.size}; height: {shard.size}; animation-delay: {shard.delay}; animation-duration: {shard.dur};"
					></div>
				{/each}
				<div class="gacha-rift-label">
					{pullBestTier === 'UUR'
						? 'разрыв не выдерживает'
						: pullBestTier === 'UR'
							? 'что-то сильное на подходе'
							: 'разрыв открывается'}
				</div>
			{:else}
				<div class="gacha-flash"></div>
			{/if}
		</div>
	{/if}
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
					{#each orderedReveal as grant, order (grant.char_id + ':' + grant.stars + ':' + grant.refunded)}
						{@const isTop = TIER_RANK[grant.tier] >= 2}
						{@const isUur = grant.tier === 'UUR'}
						<div
							class={`gacha-reveal-card gacha-tier-${grant.tier.toLowerCase()}`}
							class:gacha-reveal-card-hero={isTop}
							class:gacha-reveal-card-uur={isUur}
							style="--gacha-reveal-delay: {(order * 0.11).toFixed(2)}s"
						>
							{#if isUur}
								<div class="gacha-reveal-hero-kicker">hero-момент · джекпот-театр</div>
							{/if}
							<div class="gacha-reveal-portrait-wrap">
								{#if charArtSlug(grant.char_id)}
									<img
										src="/art/heroines/{charArtSlug(grant.char_id)}.webp"
										alt=""
										class="gacha-reveal-portrait"
									/>
								{/if}
								{#if isTop}
									<div class="gacha-reveal-sheen"></div>
								{/if}
								{#if isUur}
									<span class="gacha-reveal-star gacha-reveal-star-1"></span>
									<span class="gacha-reveal-star gacha-reveal-star-2"></span>
									<span class="gacha-reveal-star gacha-reveal-star-3"></span>
								{/if}
							</div>
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

	/* ─── roll animation overlay: charge → rift → flash (design-прототип §Гача,
	   verbatim CSS ported with a `gacha` prefix to avoid name collisions) ── */
	.gacha-pull-overlay {
		position: fixed;
		inset: 0;
		z-index: 500;
		background: #050308;
		display: flex;
		align-items: center;
		justify-content: center;
		overflow: hidden;
		cursor: pointer;
	}
	/* Цвет разрыва/вспышки = лучший тир результата (TIER_COLOR прототипа).
	   Пока сервер не ответил — нейтральный "pending" цвет, не угаданный. */
	.gacha-charge-pending,
	.gacha-charge-r {
		--gacha-tier-color: #9b97ad;
	}
	.gacha-charge-s {
		--gacha-tier-color: var(--accent-cyan);
	}
	.gacha-charge-ur {
		--gacha-tier-color: var(--accent-pink);
	}
	.gacha-charge-uur {
		--gacha-tier-color: var(--accent-yellow);
	}

	.gacha-charge-glow {
		position: absolute;
		inset: 0;
		background: radial-gradient(circle at 50% 52%, rgba(161, 117, 255, 0.32), #050308 68%);
		animation: gachaPulseGlow 1.05s ease-in-out infinite;
	}
	.gacha-charge-ring-outer {
		position: absolute;
		width: 250px;
		height: 250px;
		border: 1px solid rgba(255, 216, 74, 0.35);
		border-radius: 50%;
		animation: gachaSigilSpin 2.6s linear infinite;
	}
	.gacha-charge-ring-inner {
		position: absolute;
		width: 186px;
		height: 186px;
		border: 2px dashed rgba(161, 117, 255, 0.5);
		border-radius: 50%;
		animation: gachaSigilCounter 3.4s linear infinite;
	}
	.gacha-charge-core {
		position: absolute;
		width: 104px;
		height: 104px;
		border-radius: 50%;
		background: radial-gradient(circle, rgba(255, 255, 255, 0.35), rgba(161, 117, 255, 0.08) 72%);
		animation: gachaPulseGlow 0.9s ease-in-out infinite;
	}
	.gacha-charge-orb-wrap {
		position: absolute;
		width: 9px;
		height: 9px;
		display: flex;
		align-items: center;
		justify-content: center;
	}
	.gacha-charge-orb {
		width: 9px;
		height: 9px;
		border-radius: 50%;
		background: #fff;
		box-shadow: 0 0 12px rgba(255, 255, 255, 0.9);
		animation: gachaOrbConverge 1s ease-in forwards;
	}
	.gacha-charge-label,
	.gacha-rift-label {
		position: absolute;
		bottom: 74px;
		left: 0;
		right: 0;
		text-align: center;
		font-family: var(--font-chrome);
		font-size: 12.5px;
		letter-spacing: 0.16em;
		text-transform: uppercase;
	}
	.gacha-charge-label {
		color: rgba(245, 243, 255, 0.6);
	}
	.gacha-rift-label {
		color: var(--gacha-tier-color);
	}

	.gacha-rift-glow {
		position: absolute;
		inset: 0;
		opacity: 0.32;
		animation: gachaRiftFlicker 0.45s ease-in-out infinite;
		background: radial-gradient(ellipse at 50% 50%, var(--gacha-tier-color) 0%, rgba(0, 0, 0, 0) 62%);
	}
	.gacha-rift-beam {
		position: absolute;
		top: 14%;
		bottom: 14%;
		left: 50%;
		width: 8px;
		margin-left: -4px;
		border-radius: 999px;
		animation: gachaRiftOpen 1s cubic-bezier(0.3, 0.8, 0.2, 1) forwards;
		background: linear-gradient(
			180deg,
			rgba(255, 255, 255, 0) 0%,
			#fff 18%,
			var(--gacha-tier-color) 50%,
			#fff 82%,
			rgba(255, 255, 255, 0) 100%
		);
		box-shadow: 0 0 46px 16px var(--gacha-tier-color);
	}
	.gacha-rift-shard {
		position: absolute;
		bottom: 30%;
		border-radius: 2px;
		animation-name: gachaShardFly;
		animation-timing-function: ease-in;
		animation-fill-mode: forwards;
	}
	/* Цвет осколка = тир его персонажа (не лучший тир ролла) — тот же
	   TIER_COLOR, что и рамки reveal-карточек ниже. */
	.gacha-shard-r {
		background: #9b97ad;
	}
	.gacha-shard-s {
		background: var(--accent-cyan);
	}
	.gacha-shard-ur {
		background: var(--accent-pink);
		box-shadow: 0 0 11px rgba(255, 91, 141, 0.75);
	}
	.gacha-shard-uur {
		background: var(--accent-yellow);
		box-shadow: 0 0 16px rgba(255, 216, 74, 0.9);
	}

	.gacha-flash {
		position: absolute;
		inset: 0;
		animation: gachaFlash 0.55s ease-out forwards;
		background: radial-gradient(circle, #fff 0%, var(--gacha-tier-color) 38%, rgba(0, 0, 0, 0) 74%);
	}

	@keyframes gachaPulseGlow {
		0%,
		100% {
			opacity: 0.5;
			transform: scale(1);
		}
		50% {
			opacity: 1;
			transform: scale(1.06);
		}
	}
	@keyframes gachaSigilSpin {
		0% {
			transform: rotate(0deg) scale(1);
		}
		100% {
			transform: rotate(360deg) scale(0.82);
		}
	}
	@keyframes gachaSigilCounter {
		0% {
			transform: rotate(0deg);
		}
		100% {
			transform: rotate(-360deg);
		}
	}
	@keyframes gachaOrbConverge {
		0% {
			transform: translateX(122px) scale(0.55);
			opacity: 0;
		}
		25% {
			opacity: 1;
		}
		100% {
			transform: translateX(13px) scale(1.25);
			opacity: 0.95;
		}
	}
	@keyframes gachaRiftOpen {
		0% {
			transform: scaleY(0.02) scaleX(0.4);
			opacity: 0;
		}
		22% {
			transform: scaleY(1) scaleX(0.5);
			opacity: 1;
		}
		70% {
			transform: scaleY(1) scaleX(1);
			opacity: 1;
		}
		100% {
			transform: scaleY(1.05) scaleX(1.5);
			opacity: 0.9;
		}
	}
	@keyframes gachaRiftFlicker {
		0%,
		100% {
			opacity: 0.55;
		}
		42% {
			opacity: 1;
		}
		68% {
			opacity: 0.7;
		}
	}
	@keyframes gachaShardFly {
		0% {
			transform: translateY(0) scale(0.3);
			opacity: 0;
		}
		18% {
			opacity: 1;
		}
		100% {
			transform: translateY(-190px) scale(1);
			opacity: 0;
		}
	}
	@keyframes gachaFlash {
		0% {
			opacity: 0;
			transform: scale(0.3);
		}
		35% {
			opacity: 1;
			transform: scale(1.4);
		}
		100% {
			opacity: 0;
			transform: scale(2.2);
		}
	}
	@keyframes gachaCardFlip {
		0% {
			opacity: 0;
			transform: perspective(700px) rotateY(-92deg) scale(0.9);
		}
		62% {
			opacity: 1;
			transform: perspective(700px) rotateY(8deg) scale(1.03);
		}
		100% {
			opacity: 1;
			transform: perspective(700px) rotateY(0) scale(1);
		}
	}
	@keyframes gachaCardHeroFlip {
		0% {
			opacity: 0;
			transform: perspective(700px) rotateY(-110deg) scale(0.82);
		}
		55% {
			opacity: 1;
			transform: perspective(700px) rotateY(12deg) scale(1.12);
		}
		78% {
			transform: perspective(700px) rotateY(-4deg) scale(1.04);
		}
		100% {
			opacity: 1;
			transform: perspective(700px) rotateY(0) scale(1);
		}
	}
	@keyframes gachaSheen {
		0% {
			transform: translateX(-140%) skewX(-18deg);
		}
		100% {
			transform: translateX(240%) skewX(-18deg);
		}
	}
	@keyframes gachaStarFall {
		0% {
			transform: translateY(-30px) scale(0.5);
			opacity: 0;
		}
		20% {
			opacity: 1;
		}
		100% {
			transform: translateY(230px) scale(1);
			opacity: 0;
		}
	}
	@keyframes gachaNameIn {
		0% {
			opacity: 0;
			letter-spacing: 0.5em;
			transform: translateY(10px);
		}
		100% {
			opacity: 1;
			letter-spacing: 0.01em;
			transform: translateY(0);
		}
	}
	@keyframes gachaShake {
		0%,
		100% {
			transform: translate(0, 0);
		}
		20% {
			transform: translate(-4px, 2px);
		}
		40% {
			transform: translate(4px, -2px);
		}
		60% {
			transform: translate(-3px, -1px);
		}
		80% {
			transform: translate(3px, 1px);
		}
	}
	@keyframes gachaPulseBorder {
		0%,
		100% {
			box-shadow: 0 0 18px rgba(255, 216, 74, 0.35);
		}
		50% {
			box-shadow: 0 0 34px rgba(255, 216, 74, 0.65);
		}
	}

	.gacha-reveal {
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.gacha-reveal-card {
		--gacha-reveal-delay: 0s;
		position: relative;
		overflow: hidden;
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-md);
		text-align: center;
		/* Раскрытие карточки (deploy/index.html yvCardFlip) — стагger через
		   --gacha-reveal-delay, выставляемый инлайн по порядку раскрытия
		   (см. orderedReveal). */
		animation: gachaCardFlip 0.58s cubic-bezier(0.2, 0.9, 0.3, 1.1) both;
		animation-delay: var(--gacha-reveal-delay);
	}
	/* UR/UUR карточки — более размашистый флип (yvCardHeroFlip) + видимый sheen. */
	.gacha-reveal-card-hero {
		animation-name: gachaCardHeroFlip;
		animation-duration: 0.7s;
		animation-timing-function: cubic-bezier(0.2, 0.9, 0.3, 1.2);
	}
	/* UUR — тот же hero-флип плюс лёгкий "shake" сразу после его завершения
	   (yvShake, delay = свой стаггер + 0.7s флипа), заданы отдельными
	   значениями на позицию в списке анимаций, чтобы не конфликтовать со
	   стаггером через CSS-переменную. */
	.gacha-reveal-card-uur {
		animation-name: gachaCardHeroFlip, gachaShake;
		animation-duration: 0.7s, 0.4s;
		animation-timing-function: cubic-bezier(0.2, 0.9, 0.3, 1.2), ease-out;
		animation-delay: var(--gacha-reveal-delay), calc(var(--gacha-reveal-delay) + 0.7s);
		animation-fill-mode: both, both;
	}
	.gacha-reveal-hero-kicker {
		position: relative;
		font-family: var(--font-chrome);
		font-size: 11px;
		letter-spacing: 0.2em;
		color: var(--accent-yellow);
		text-transform: uppercase;
		margin-bottom: var(--space-xs);
	}
	.gacha-reveal-portrait-wrap {
		position: relative;
		overflow: hidden;
		border-radius: 10px;
		margin-bottom: var(--space-sm);
	}
	.gacha-reveal-card-uur .gacha-reveal-portrait-wrap {
		border: 3px solid var(--accent-yellow);
		animation: gachaPulseBorder 1.8s ease-in-out infinite;
	}
	.gacha-reveal-sheen {
		position: absolute;
		top: 0;
		bottom: 0;
		width: 38%;
		background: linear-gradient(
			90deg,
			rgba(255, 255, 255, 0) 0%,
			rgba(255, 255, 255, 0.55) 50%,
			rgba(255, 255, 255, 0) 100%
		);
		animation: gachaSheen 1.8s ease-out 0.35s;
	}
	.gacha-reveal-star {
		position: absolute;
		top: 0;
		width: 4px;
		height: 4px;
		border-radius: 50%;
		background: var(--accent-yellow);
		animation: gachaStarFall 2.4s ease-in infinite;
	}
	.gacha-reveal-star-1 {
		left: 18%;
	}
	.gacha-reveal-star-2 {
		left: 52%;
		width: 3px;
		height: 3px;
		background: #fff;
		animation-duration: 2.8s;
		animation-delay: 0.7s;
	}
	.gacha-reveal-star-3 {
		left: 78%;
		animation-duration: 2.2s;
		animation-delay: 1.3s;
	}
	.gacha-reveal-card-uur .gacha-reveal-name {
		animation: gachaNameIn 0.7s cubic-bezier(0.2, 0.9, 0.3, 1) 0.15s both;
	}
	.gacha-reveal-portrait {
		width: 100%;
		height: 140px;
		object-fit: cover;
		object-position: top center;
		display: block;
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
