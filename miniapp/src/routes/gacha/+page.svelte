<script lang="ts">
	// Gacha — roll ×1/×10 + tier-list collection view (GACHA-01, visual half
	// of GACHA-03). 04-UI-SPEC.md §Component Inventory: rarity color-coding
	// R=neutral/SR=cyan/SSR=pink/UR=yellow-glow (Hero/Impact reveal tier on
	// UR pulls specifically); pity counters shown as small label-tier text
	// under the roll button.
	//
	// Server is the sole source of truth for every roll outcome
	// (gacha_service.roll, D-03) — this screen only renders whatever
	// POST /gacha/roll returns. Roll results carry only char_id/tier/stars
	// (roll()/​_grant()/​_apply_dupe() are untouched, per 04.2-05-PLAN.md) —
	// character NAMES come from GET /gacha/collection's catalog-enriched
	// rows, so the collection is always re-fetched right after a roll: the
	// freshly-granted character is guaranteed to already be in the
	// collection (grant always happens for every result), which resolves
	// names for the reveal without duplicating gacha_catalog client-side.
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	// Временное отключение раздела (хаб уже прячет тайл — см. lockedTiles в
	// +page.svelte хаба). Флаг здесь же гасит и прямой заход по /gacha,
	// код ниже не трогается — переключается обратно одной строкой.
	const GACHA_DISABLED = true;

	const ROLL_COST = 300;
	const ROLL10_COST = 2700;
	const PITY_SSR = 50;
	const PITY_UR = 90;
	const TIER_ORDER = ['UR', 'SSR', 'SR', 'R'] as const;

	type Tier = 'R' | 'SR' | 'SSR' | 'UR';
	type Character = {
		char_id: string;
		name: string;
		tier: Tier;
		role: 'worker' | 'heroine';
		stars: number;
		copies: number;
	};
	type CollectionState = {
		characters: Character[];
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

	let loading = $state(true);
	let error = $state<string | null>(null);
	let rolling = $state(false);
	let reveal = $state<RollGrant[] | null>(null);

	let collection = $state<Character[]>([]);
	let pitySsr = $state(0);
	let pityUr = $state(0);
	let bannerId = $state('');

	let byId = $derived(new Map(collection.map((c) => [c.char_id, c])));
	let bannerChar = $derived(byId.get(bannerId) ?? null);
	let grouped = $derived(
		TIER_ORDER.map((tier) => ({ tier, chars: collection.filter((c) => c.tier === tier) })).filter(
			(g) => g.chars.length > 0
		)
	);

	function describeError(err: unknown): string {
		return err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
	}

	async function loadCollection() {
		try {
			const res = await apiFetch<CollectionState>('/api/v1/gacha/collection');
			collection = res.characters;
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
			const hasUr = res.results.some((r) => r.tier === 'UR');
			haptic(hasUr ? 'big-win' : 'win');
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

	function bannerLabel(): string {
		if (!bannerId) return 'баннер не выбран';
		return bannerChar ? bannerChar.name : `персонаж ${bannerId} (ещё не выпал)`;
	}

	onMount(() => {
		if (!GACHA_DISABLED) loadCollection();
	});
</script>

{#if GACHA_DISABLED}
	<div class="screen-loading"><span>гача временно отключена</span></div>
{:else if loading}
	<div class="screen-loading"><span>загрузка баннера…</span></div>
{:else}
	<div class="gacha-screen">
		<div class="menu-head">
			<h1 class="menu-title">Гача</h1>
			<div class="menu-sub">крути баннер, собирай тир-лист</div>
		</div>

		<div class="gacha-banner">
			<div class="gacha-banner-label">Rate-up баннер</div>
			<div class="gacha-banner-name">{bannerLabel()}</div>
		</div>

		{#if error}
			<div class="cf-error">{error}</div>
		{/if}

		{#if reveal}
			<div class="gacha-reveal">
				{#each reveal as grant (grant.char_id + ':' + grant.stars + ':' + grant.refunded)}
					<div class={`gacha-reveal-card gacha-tier-${grant.tier.toLowerCase()}`}>
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
			<button type="button" class="chip gacha-roll-btn" disabled={rolling} onclick={() => roll(1)}>
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
			<span>пити SSR: {pitySsr}/{PITY_SSR}</span>
			<span>пити UR: {pityUr}/{PITY_UR}</span>
		</div>

		<div class="gacha-collection">
			<div class="fc-title">Коллекция</div>
			{#if grouped.length === 0}
				<div class="fc-desc">пока пусто — крути баннер</div>
			{/if}
			{#each grouped as group (group.tier)}
				<div class="gacha-tier-group">
					<div class={`gacha-tier-heading gacha-tier-${group.tier.toLowerCase()}`}>
						{group.tier}
					</div>
					<div class="gacha-tier-grid">
						{#each group.chars as char (char.char_id)}
							<div class={`gacha-char-card gacha-tier-${char.tier.toLowerCase()}`}>
								<div class="gacha-char-name">{char.name}</div>
								<div class="gacha-char-stars">{'★'.repeat(char.stars)}</div>
								{#if char.copies > 1}
									<div class="gacha-char-copies">×{char.copies}</div>
								{/if}
							</div>
						{/each}
					</div>
				</div>
			{/each}
		</div>
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

	.gacha-banner {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-md);
		text-align: center;
	}
	.gacha-banner-label {
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
	}
	.gacha-banner-name {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		color: var(--accent-yellow);
		margin-top: var(--space-xs);
	}

	.gacha-reveal {
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.gacha-reveal-card {
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-md);
		text-align: center;
	}
	.gacha-reveal-tier {
		font-family: var(--font-numeric);
		font-size: var(--font-label-size);
		font-weight: 900;
		letter-spacing: 0.08em;
	}
	.gacha-reveal-name {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		color: var(--text-primary);
		margin-top: var(--space-xs);
	}
	.gacha-reveal-stars {
		font-size: var(--font-heading-size);
		color: var(--accent-yellow);
		margin-top: var(--space-xs);
	}
	.gacha-reveal-dupe {
		font-size: 12px;
		color: var(--text-muted);
		font-family: var(--font-body);
		margin-top: var(--space-xs);
	}

	/* Rarity color-coding (04-UI-SPEC.md §Component Inventory): R=neutral,
	   SR=cyan, SSR=pink, UR=yellow glow (Hero/Impact reveal tier
	   specifically on UR pulls). */
	.gacha-tier-r {
		border-color: #9b97ad;
	}
	.gacha-tier-r .gacha-reveal-tier,
	.gacha-tier-r .gacha-char-name {
		color: #9b97ad;
	}
	.gacha-tier-sr {
		border-color: var(--accent-cyan);
	}
	.gacha-tier-sr .gacha-reveal-tier,
	.gacha-tier-sr .gacha-char-name {
		color: var(--accent-cyan);
	}
	.gacha-tier-ssr {
		border-color: var(--accent-pink);
	}
	.gacha-tier-ssr .gacha-reveal-tier,
	.gacha-tier-ssr .gacha-char-name {
		color: var(--accent-pink);
	}
	.gacha-tier-ur {
		border-color: var(--accent-yellow);
		box-shadow: 0 0 24px rgba(255, 216, 74, 0.45);
	}
	.gacha-tier-ur .gacha-reveal-tier,
	.gacha-tier-ur .gacha-char-name {
		color: var(--accent-yellow);
	}
	/* Hero-tier reveal treatment on UR pulls specifically (04-UI-SPEC.md
	   line 68: gacha UR pull reveal is a "jackpot theater" Hero moment). */
	.gacha-reveal-card.gacha-tier-ur .gacha-reveal-name {
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
	.gacha-tier-heading {
		font-family: var(--font-numeric);
		font-size: var(--font-label-size);
		font-weight: 900;
		letter-spacing: 0.08em;
	}
	.gacha-tier-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--space-sm);
	}
	.gacha-char-card {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 10px;
		padding: var(--space-sm);
	}
	.gacha-char-name {
		font-family: var(--font-chrome);
		font-size: 13px;
		font-weight: 700;
		color: var(--text-primary);
	}
	.gacha-char-stars {
		font-size: 12px;
		color: var(--accent-yellow);
		margin-top: 2px;
	}
	.gacha-char-copies {
		font-size: 11px;
		color: var(--text-muted);
		margin-top: 2px;
		font-family: var(--font-body);
	}
</style>
