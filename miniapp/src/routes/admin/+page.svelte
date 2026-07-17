<script lang="ts">
	// Admin sub-hub — destination of the top-level "Админ" hub tile
	// (04.3-UI-SPEC.md §Component Inventory "2. Admin sub-hub screen").
	// Same feature-card primitive/2-col grid as the Games sub-hub — a thin
	// router screen, no data of its own beyond routing. The real security
	// boundary is server-side require_admin on every /api/v1/admin/* route;
	// this screen (and the hub tile that leads here) is reachable by a
	// non-admin typing the URL directly, so the destination screens must
	// themselves show "Только для админов" on a 403 (handled there, not here).
	import { goto } from '$app/navigation';

	type Tile = {
		title: string;
		desc: string;
		accent: 'cyan' | 'yellow';
		href: string;
	};

	const tiles: Tile[] = [
		{
			title: 'Аналитика',
			desc: 'баланс банка, популярность игр, обороты, DAU',
			accent: 'cyan',
			href: '/admin/analytics'
		},
		{ title: 'Заявки', desc: 'фидбек от участников', accent: 'yellow', href: '/admin/feedback' }
	];
</script>

<div class="menu">
	<div class="menu-head">
		<h1 class="menu-title">Админ</h1>
		<div class="menu-sub">баланс банка, аналитика, заявки</div>
	</div>

	<div class="feature-grid">
		{#each tiles as tile (tile.title)}
			<button
				type="button"
				class={`feature-card fc-${tile.accent}`}
				onclick={() => goto(tile.href)}
			>
				<span class="fc-title">{tile.title}</span>
				<span class="fc-desc">{tile.desc}</span>
				<span class="fc-chev" aria-hidden="true">&rsaquo;</span>
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
	}
	.menu-sub {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		margin-top: var(--space-xs);
		letter-spacing: 0.04em;
		font-family: var(--font-body);
	}
</style>
