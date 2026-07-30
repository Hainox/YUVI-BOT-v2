<script lang="ts">
	// Тето Брейнрот: Дрель-Хант — placeholder screen only. The money/engine
	// layer (casino_service.play_teto_slots) and the HTTP route
	// (POST /api/v1/games/teto_slots) are separate in-flight work; the actual
	// 6×6 board/megablock/tumble-cascade/Drill-Hunt rendering layer is its own
	// future task and explicitly out of scope here. This screen makes the
	// Тето tile on games/slots/+page.svelte land somewhere coherent instead
	// of a broken route — no API calls, no bet controls, nothing interactive.
</script>

<div class="menu">
	<div class="menu-head">
		<h1 class="menu-title">Тето Брейнрот</h1>
		<div class="menu-sub">Дрель-Хант</div>
	</div>

	<div class="soon-card">
		<span class="soon-badge">скоро</span>
		<p class="soon-text">Автомат в разработке.</p>
		<p class="soon-hint">
			6×6 поле, мегаблоки и тумбл-каскады — падение и сборка новых комбинаций без перекрута
			барабанов.
		</p>
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

	.soon-card {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-lg) var(--space-md);
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: var(--space-sm);
	}
	.soon-badge {
		font-family: var(--font-chrome);
		font-size: var(--font-label-size);
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--accent-cyan);
		background: color-mix(in srgb, var(--accent-cyan) 16%, transparent);
		border-radius: 999px;
		padding: 4px 12px;
	}
	.soon-text {
		margin: 0;
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		color: var(--text-primary);
	}
	.soon-hint {
		margin: 0;
		font-size: var(--font-body-size);
		color: var(--text-muted);
		line-height: 1.5;
		font-family: var(--font-body);
	}
</style>
