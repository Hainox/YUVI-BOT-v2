<script lang="ts">
	// Rules — destination of the "Правила" hub tile (CASINO-02). 04-UI-SPEC.md
	// §Component Inventory: "Rules accordion (rule-block, pay-grid-rules) —
	// webapp/rules.jsx — extend with one rule-block per game (roulette
	// payouts, blackjack rules, dice odds, gacha rates/pity, duel mechanics)."
	// Static content screen — no API calls. Numbers below are read verbatim
	// from the actual game constants shipped in this phase/04.1 (not
	// re-derived/approximated): bot/services/casino_service.py
	// (COINFLIP_MULT/DICE_HOUSE_EDGE/ROULETTE_*_MULT/BLACKJACK_*_MULT),
	// bot/config.py (economy_start_bonus/transfer_fee_pct/casino_min_bet),
	// bot/services/duel_service.py (MUTE_SECONDS), bot/services/
	// clicker_service.py (AMM_ANCHOR_CP_PER_HRYVNA), gacha_catalog.
	// TIER_WEIGHTS (SR 80% / SSR 18% / UR 2%, R structurally unreachable).
	//
	// Changelog вынесен из этого экрана в отдельную плитку хаба «Что новое»
	// (WHATSNEW-01, запрошено 2026-07-24, отменяет решение 04.2-UI-SPEC.md
	// Hub Tile Inventory #11 "NOT a separate hub tile") — там же теперь живая
	// лента из БД (bot/services/changelog_service.py), а не статичный список
	// версий, который дублировал бы источник новостей.

	let openIndex = $state<number | null>(0);

	function toggle(i: number): void {
		openIndex = openIndex === i ? null : i;
	}

	type Block = { title: string; body: string[] };

	const blocks: Block[] = [
		{
			title: 'Старт и экономика',
			body: [
				'Новый участник получает 1000¥ при первом заходе в казино — это общий баланс на весь чат.',
				'Перевод другому участнику (вкладка «Перевод»): комиссия 5% от суммы (минимум 1¥) уходит в банк чата.',
				'Лидерборд — рейтинг по текущему балансу, обновляется мгновенно.',
				'Банк чата (вкладка «Статистика»/«Экономика») пополняется комиссиями и иногда выплачивает призы — это общий пул, а не чей-то личный кошелёк.'
			]
		},
		{
			title: 'Монетка',
			body: [
				'Орёл/решка, шанс 50/50.',
				'Выигрыш — ставка × 1.98 (небольшая комиссия казино зашита в множитель).'
			]
		},
		{
			title: 'Кости',
			body: [
				'Выбери порог и направление (больше/меньше) — чем ниже твой шанс угадать, тем выше множитель.',
				'Комиссия казино — 2% от честной выплаты (тот же принцип, что и в остальных играх).'
			]
		},
		{
			title: 'Рулетка',
			body: [
				'Европейское колесо, числа 0–36.',
				'Ставка на число — ×36. Ставка на цвет/чёт-нечёт/половину — ×2. Ставка на дюжину — ×3.'
			]
		},
		{
			title: 'Блэкджек',
			body: [
				'Классические правила, дилер берёт карту с мягких 17.',
				'Натуральный блэкджек (21 с двух карт) — выплата ×2.5.',
				'Обычная победа — ×2. Пуш (ничья) — ставка возвращается ×1.'
			]
		},
		{
			title: 'Ферма',
			body: [
				'Тапай — копи CP (кликер-очки). Прокачивай тап-уровень и автокликер, чтобы получать CP даже офлайн.',
				'Конвертация CP → ¥ идёт через внутренний AMM-курс (ориентир: 100 CP ≈ 1¥, курс плавает в зависимости от объёма конверсий — так же честно, как настоящий рынок, без ручных корректировок).'
			]
		},
		{
			title: 'Гача',
			body: [
				'Крути баннер за ювики (доступна ×1 и ×10 крутка) — собирай персонажей R/SR/SSR/UR.',
				'Шансы на редкий тир: SR ≈ 80%, SSR ≈ 18%, UR ≈ 2%.',
				'При x10-крутке гарантирован минимум один SR или выше.',
				'Дубликат персонажа сверх лимита коллекции автоматически возвращается звёздами (не пропадает зря).',
				'Собранные персонажи фермы приносят пассивный доход в CP.'
			]
		},
		{
			title: 'Дуэли',
			body: [
				'Вызови другого участника на бабки (или «дуэльбота» — банк чата) через вкладку «Дуэль» или /duel в чате.',
				'Ставки обоих участников уходят в общий банк раунда, комиссия 5% удерживается при принятии дуэли, победитель забирает остальное.',
				'Проигравший получает мут на 10 минут — это часть механики, не баг.'
			]
		},
		{
			title: 'Рынки ставок',
			body: [
				'Ставь на исход события через вкладку «Рынки» — минимальная ставка 10¥.',
				'При создании/импорте рынка и при разрешении удерживается небольшая комиссия в банк чата.',
				'Портфолио (вкладка «Портфолио») показывает все твои открытые и закрытые позиции.'
			]
		}
	];
</script>

<div class="ru-screen">
	<div class="menu-head">
		<h1 class="menu-title">Правила</h1>
		<div class="menu-sub">как это всё работает</div>
	</div>

	<div class="rule-list">
		{#each blocks as block, i (block.title)}
			<div class="rule-block">
				<button
					type="button"
					class="rule-header"
					onclick={() => toggle(i)}
					aria-expanded={openIndex === i}
				>
					<span class="rule-title">{block.title}</span>
					<span class="rule-chev" class:open={openIndex === i} aria-hidden="true">&rsaquo;</span>
				</button>
				{#if openIndex === i}
					<div class="rule-body">
						<ul>
							{#each block.body as line (line)}
								<li>{line}</li>
							{/each}
						</ul>
					</div>
				{/if}
			</div>
		{/each}
	</div>
</div>

<style>
	.ru-screen {
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

	.rule-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.rule-block {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		overflow: hidden;
	}
	.rule-header {
		width: 100%;
		background: none;
		border: none;
		padding: var(--space-md);
		display: flex;
		align-items: center;
		justify-content: space-between;
		cursor: pointer;
		font-family: inherit;
		color: inherit;
		min-height: 44px;
	}
	.rule-title {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		color: var(--text-primary);
	}
	.rule-chev {
		color: var(--text-muted);
		font-size: 22px;
		line-height: 1;
		transform: rotate(90deg);
		transition: transform 0.15s;
	}
	.rule-chev.open {
		transform: rotate(-90deg);
	}
	.rule-body {
		padding: 0 var(--space-md) var(--space-md);
		font-size: var(--font-body-size);
		color: var(--text-muted);
		font-family: var(--font-body);
		line-height: 1.5;
	}
	.rule-body ul {
		margin: 0;
		padding-left: 18px;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.rule-body li {
		color: var(--text-secondary);
	}
</style>
