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

	type BannerInfo = {
		featured_id: string;
		featured_name: string;
		featured_tier: 'R' | 'S' | 'UR' | 'UUR';
		featured_art_slug: string;
		is_rate_up: boolean;
	};

	// Admin tile gating is cosmetic only (04.3-UI-SPEC.md "Admin tile gating"):
	// hides the tile from non-admins as a UX nicety. The real boundary is
	// require_admin on every /api/v1/admin/* route, re-checked server-side
	// independently of this flag.
	let isAdmin = $state(false);
	let banner = $state<BannerInfo | null>(null);

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

		try {
			banner = await apiFetch<BannerInfo>('/api/v1/gacha/banner');
		} catch {
			// Промо-тизер — некритичное украшение хаба (design-прототип §Хаб):
			// на любой сбой просто не показываем карточку, сам хаб не должен
			// падать из-за неё.
			banner = null;
		}
	});

	// Порядок и первая пара (Гача/Игры) — 1:1 с design-прототипом (deploy/index.html
	// §Хаб). Квесты — фича этого репозитория, которой нет в исходном прототипе,
	// поставлена рядом с Фермой (её бонусные уровни разблокируют именно квесты).
	const baseTiles: Tile[] = [
		{ title: 'Гача', desc: 'крути баннер, собирай коллекцию героинь', accent: 'pink', href: '/gacha' },
		{ title: 'Игры', desc: 'слоты · рулетка · блэкджек · кости · монетка', accent: 'pink', href: '/games' },
		{ title: 'Ферма', desc: 'тапай, копи CP, качай апгрейды', accent: 'cyan', href: '/farm' },
		{ title: 'Квесты', desc: 'ежедневные задания и достижения', accent: 'yellow', href: '/quests' },
		{ title: 'Дуэль', desc: 'вызови кого-нибудь на бабки', accent: 'pink', href: '/duel' },
		{ title: 'Рынки', desc: 'ставь на исход, следи за котировкой', accent: 'cyan', href: '/markets' },
		{ title: 'Биржа', desc: 'продай ювики за что угодно — вне бота', accent: 'cyan', href: '/exchange' },
		{ title: 'Портфолио', desc: 'твои открытые позиции', accent: 'yellow', href: '/portfolio' },
		{ title: 'История', desc: 'все твои ставки, тапы и переводы', href: '/history' },
		{ title: 'Лидерборд', desc: 'топ богачей чата', accent: 'yellow', href: '/leaderboard' },
		{ title: 'Статистика', desc: 'баланс, стрик, большие выигрыши', accent: 'cyan', href: '/stats' },
		{ title: 'Настроение', desc: 'настроение и токсичность чата', accent: 'cyan', href: '/mood' },
		{ title: 'Жертва дня', desc: 'приз, титул и дебафф на 24ч', accent: 'pink', href: '/victim' },
		{ title: 'Итоги дня', desc: '7 номинаций и игра дня из Steam', accent: 'yellow', href: '/awards' },
		{ title: 'Перевод', desc: 'закинь другу ювиков', href: '/transfer' },
		{
			title: 'Донат',
			desc: 'задонать звёздами Telegram — получи ювики',
			accent: 'yellow',
			href: '/donate'
		},
		{ title: 'Правила', desc: 'как это всё работает', href: '/rules' },
		{ title: 'Что новое', desc: 'обновления и планы разработки', href: '/whatsnew' },
		{ title: 'Фидбек', desc: 'баг, идея или жалоба — админы увидят', href: '/feedback' },
		{ title: 'Магазин', desc: 'поукай, обними, закажи анекдот или роаст', accent: 'pink', href: '/shop' },
		{ title: 'Теги', desc: 'арендуй тег над своим именем', accent: 'yellow', href: '/tags' },
		{ title: 'Спросить чат', desc: 'AI-поиск по истории переписки', accent: 'cyan', href: '/ai/ask' },
		{ title: 'Карточка участника', desc: 'AI-портрет, статистика, настроение', accent: 'cyan', href: '/ai/card' },
		{ title: 'Дайджест', desc: 'AI-пересказ жизни чата за период', accent: 'cyan', href: '/ai/digest' },
		{ title: 'Пересказ', desc: 'AI-пересказ последних сообщений', accent: 'cyan', href: '/ai/summary' },
		{ title: 'AI-развлечения', desc: 'темы обсуждений · фраза дня · анекдот', accent: 'yellow', href: '/ai/fun' }
	];

	const adminTile: Tile = {
		title: 'Админ',
		desc: 'баланс банка, аналитика, заявки',
		href: '/admin'
	};

	const lockedTiles: Tile[] = [];

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

	{#if banner}
		<button type="button" class="gacha-teaser" onclick={() => goto('/gacha')}>
			<div class="gacha-teaser-portrait">
				<img src="/art/heroines/{banner.featured_art_slug}.webp" alt="" />
			</div>
			<div class="gacha-teaser-text">
				<div class="gacha-teaser-kicker">{banner.is_rate_up ? 'Rate-up баннер' : 'Топ-тир'}</div>
				<div class="gacha-teaser-name">{banner.featured_name}</div>
				<div class="gacha-teaser-sub">крути девочек, собирай коллекцию</div>
			</div>
			<div class="gacha-teaser-chev" aria-hidden="true">&rsaquo;</div>
		</button>
	{/if}

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

	/* ─── gacha rate-up teaser (design-прототип §Хаб, deploy/index.html) ── */
	.gacha-teaser {
		text-align: left;
		border: 2px solid var(--accent-yellow);
		border-radius: 16px;
		padding: 0;
		overflow: hidden;
		cursor: pointer;
		background: linear-gradient(135deg, #0d0821, #a175ff);
		box-shadow: 0 0 26px rgba(255, 216, 74, 0.28);
	}
	.gacha-teaser {
		display: flex;
		align-items: center;
		gap: var(--space-md);
		padding: var(--space-md) 18px;
	}
	.gacha-teaser-portrait {
		width: 60px;
		height: 60px;
		border-radius: 12px;
		overflow: hidden;
		position: relative;
		border: 1px solid rgba(255, 255, 255, 0.35);
		flex-shrink: 0;
		animation: teaserFloat 3s ease-in-out infinite;
		background: radial-gradient(circle at 40% 30%, rgba(255, 255, 255, 0.3), rgba(161, 117, 255, 0.2) 70%);
	}
	.gacha-teaser-portrait img {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		object-fit: cover;
		object-position: center 18%;
	}
	@keyframes teaserFloat {
		0%,
		100% {
			transform: translateY(0);
		}
		50% {
			transform: translateY(-6px);
		}
	}
	.gacha-teaser-text {
		flex: 1;
		min-width: 0;
	}
	.gacha-teaser-kicker {
		font-family: var(--font-chrome);
		font-size: 10px;
		letter-spacing: 0.12em;
		color: var(--accent-yellow);
		text-transform: uppercase;
	}
	.gacha-teaser-name {
		font-family: var(--font-chrome);
		font-size: 18px;
		font-weight: 700;
		color: #fff;
		margin-top: 2px;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.gacha-teaser-sub {
		font-size: 11.5px;
		color: rgba(255, 255, 255, 0.75);
		margin-top: 2px;
		font-family: var(--font-body);
	}
	.gacha-teaser-chev {
		font-family: var(--font-chrome);
		font-size: 22px;
		color: var(--accent-yellow);
		flex-shrink: 0;
	}
</style>
