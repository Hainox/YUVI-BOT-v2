<script lang="ts">
	// Slots sub-hub — destination of the "Слот" tile on the games hub
	// (games/+page.svelte). Was previously the Azumanga slot screen itself;
	// that screen moved to games/slots/azumanga/+page.svelte once a second
	// slot machine (Тето Брейнрот) needed picking between. Same feature-card
	// primitive/structure as the top-level hub, one level deeper.
	import { goto } from '$app/navigation';

	type Tile = {
		title: string;
		desc: string;
		accent: 'pink' | 'cyan' | 'yellow';
		href: string;
	};

	const tiles: Tile[] = [
		{ title: 'Азуманга', desc: '3×5, 10 линий, до 1000×', accent: 'pink', href: '/games/slots/azumanga' },
		{
			title: 'Тето Брейнрот: Дрель-Хант',
			desc: '6×6, мегаблоки, тумбл-каскады',
			accent: 'cyan',
			href: '/games/slots/teto'
		}
	];
</script>

<div class="menu">
	<div class="menu-head">
		<h1 class="menu-title">Слоты</h1>
		<div class="menu-sub">выбери автомат</div>
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
