<script lang="ts">
	// Hub — the casino home (04.2-UI-SPEC.md Hub Tile Inventory, D-04/D-05).
	// Same feature-card primitive/2-col grid as the Phase-4 prototype
	// (webapp/menu.jsx), extended from 6 to the final 13-tile set, and now
	// 15 with the Фидбек/Админ tiles from 04.3-UI-SPEC.md §"1. Hub tile
	// additions". The persistent balance card itself lives in +layout.svelte,
	// not here.
	import { goto } from '$app/navigation';
	import { onMount } from 'svelte';
	import { apiFetch } from '$lib/api';

	type Tile = {
		title: string;
		desc: string;
		accent?: 'pink' | 'cyan' | 'yellow';
		locked?: string;
		href?: string;
	};

	// Admin tile gating is cosmetic only (04.3-UI-SPEC.md "Admin tile gating"):
	// hides the tile from non-admins as a UX nicety. The real boundary is
	// require_admin on every /api/v1/admin/* route, re-checked server-side
	// independently of this flag.
	let isAdmin = $state(false);

	onMount(async () => {
		try {
			const me = await apiFetch<{ is_admin: boolean }>('/api/v1/me');
			isAdmin = me.is_admin;
		} catch {
			// Dedicated call, separate from +layout.svelte's own /me fetch — on
			// any failure (network hiccup, non-admin's already-handled 403/401
			// elsewhere) just keep the tile hidden rather than surfacing a
			// second error state on top of the layout's own error screen.
			isAdmin = false;
		}
	});

	const baseTiles: Tile[] = [
		{ title: 'Игры', desc: 'слоты · рулетка · блэкджек · кости · монетка', accent: 'pink', href: '/games' },
		{ title: 'Ферма', desc: 'тапай, копи CP, качай апгрейды', accent: 'cyan', href: '/farm' },
		{ title: 'Гача', desc: 'крути баннер, собирай тир-лист', accent: 'yellow', href: '/gacha' },
		{ title: 'Дуэль', desc: 'вызови кого-нибудь на бабки', accent: 'pink', href: '/duel' },
		{ title: 'Рынки', desc: 'ставь на исход, следи за котировкой', accent: 'cyan', href: '/markets' },
		{ title: 'Портфолио', desc: 'твои открытые позиции', accent: 'yellow', href: '/portfolio' },
		{ title: 'История', desc: 'все твои ставки, тапы и переводы', href: '/history' },
		{ title: 'Лидерборд', desc: 'топ богачей чата', accent: 'yellow', href: '/leaderboard' },
		{ title: 'Статистика', desc: 'баланс, стрик, большие выигрыши', accent: 'cyan', href: '/stats' },
		{ title: 'Перевод', desc: 'закинь другу ювиков', href: '/transfer' },
		{
			title: 'Донат',
			desc: 'задонать звёздами Telegram — получи ювики',
			accent: 'yellow',
			href: '/donate'
		},
		{ title: 'Правила', desc: 'как это всё работает', href: '/rules' },
		{ title: 'Фидбек', desc: 'баг, идея или жалоба — админы увидят', href: '/feedback' }
	];

	const adminTile: Tile = {
		title: 'Админ',
		desc: 'баланс банка, аналитика, заявки',
		href: '/admin'
	};

	const lockedTiles: Tile[] = [
		{ title: 'Магазин', desc: 'скоро', locked: 'скоро' },
		{ title: 'Теги', desc: 'скоро', locked: 'скоро' }
	];

	let tiles = $derived([...baseTiles, ...(isAdmin ? [adminTile] : []), ...lockedTiles]);

	function open(tile: Tile) {
		if (tile.locked || !tile.href) return;
		goto(tile.href);
	}
</script>

<div class="menu">
	<div class="menu-head">
		<h1 class="menu-title">Yuvi скам</h1>
		<div class="menu-sub">казино · ставки · абсурд</div>
	</div>

	<div class="feature-grid">
		{#each tiles as tile (tile.title)}
			<button
				type="button"
				class={`feature-card ${tile.accent ? `fc-${tile.accent}` : ''} ${tile.locked ? 'fc-locked' : ''}`}
				disabled={!!tile.locked}
				onclick={() => open(tile)}
			>
				<span class="fc-title">{tile.title}</span>
				<span class="fc-desc">{tile.locked || tile.desc}</span>
				{#if !tile.locked}
					<span class="fc-chev" aria-hidden="true">&rsaquo;</span>
				{/if}
			</button>
		{/each}
	</div>
</div>

<style>
	.menu {
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
		letter-spacing: -0.01em;
	}
	.menu-sub {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		margin-top: var(--space-xs);
		letter-spacing: 0.04em;
		font-family: var(--font-body);
	}
</style>
