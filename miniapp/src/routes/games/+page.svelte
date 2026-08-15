<script lang="ts">
	// Games sub-hub — destination of the top-level "Игры" tile
	// (04.2-UI-SPEC.md §Games sub-hub screen). Same feature-card primitive,
	// second-level grid listing all 5 casino games.
	import { goto } from '$app/navigation';

	type Tile = {
		title: string;
		desc: string;
		accent: 'pink' | 'cyan' | 'yellow';
		href: string;
	};

	// Copy verbatim from 04-UI-SPEC.md/prototype — do not reword, except
	// "Слот" (now a picker between two machines, see games/slots/+page.svelte,
	// so its old Azumanga-specific stats no longer describe the destination)
	// and "Краш" (new game, no spec copy to match — desc written to match the
	// style of the rest: short, mechanic-first, no marketing fluff).
	const tiles: Tile[] = [
		{ title: 'Слоты', desc: '2 автомата на выбор', accent: 'pink', href: '/games/slots' },
		{
			title: 'Рулетка',
			desc: 'европейское колесо, 0–36',
			accent: 'cyan',
			href: '/games/roulette'
		},
		{
			title: 'Блэкджек',
			desc: 'натурал 2.5×, дилер бьёт с мягких 17',
			accent: 'yellow',
			href: '/games/blackjack'
		},
		{ title: 'Кости', desc: 'больше/меньше, выбери множитель', accent: 'pink', href: '/games/dice' },
		{ title: 'Монетка', desc: 'орёл/решка, 50/50', accent: 'cyan', href: '/games/coinflip' },
		{
			title: 'Краш',
			desc: 'растущий множитель — успей забрать',
			accent: 'yellow',
			href: '/games/crash'
		}
	];
</script>

<div class="menu">
	<div class="menu-head">
		<h1 class="menu-title">Игры</h1>
		<div class="menu-sub">выбери стол</div>
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
