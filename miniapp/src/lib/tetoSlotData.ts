// Client-side mirror of bot/services/teto_megablock.py (symbol ids + tiers) —
// used PURELY for rendering the «Тето Брейнрот: Дрель-Хант» board and its
// paytable UI (symbol artwork/names/cell tints). The server is the sole source
// of truth for the outcome (D-03/T-04.1-01): this file never computes
// wins/payouts/RNG/megablock shapes, same "informational mirror" pattern
// already established for the Azumanga slot (slotData.ts), dice's client-side
// multiplier readout and roulette's red/black coloring.
//
// symbol_id values MUST stay byte-identical to the Python ones in
// bot/services/teto_megablock.py (LOW_SYMBOLS / HIGH_SYMBOLS / SCATTER_ID /
// WILD_ID) — they arrive from the server inside the megablock payload and are
// used verbatim as the key into TETO_SYMBOLS.
//
// Artwork: 512x512 RGBA WebP built by scripts/build_teto_symbols.py from
// docs/assets/teto-symbol-sources/. The symbol → source-image mapping is
// SETTLED with the repo owner — notably `teto_chimera` is the dog-bodied
// многоголовая фотожаба and `baguette_cerberus` is the orange-haired official
// art. See that script's docstring before "fixing" anything here.

export type TetoSymbolTier = 'low' | 'high' | 'wild' | 'scatter';

export type TetoSymbolMeta = {
	/** Russian display name for the paytable UI (docs/roadmap.md §Тето Брейнрот). */
	name: string;
	tier: TetoSymbolTier;
	/** Built asset, 512x512 WebP with alpha. */
	src: string;
	/**
	 * Hex tint for the board cell behind the symbol — same role as `tint` in
	 * slotData.ts.
	 *
	 * ВАЖНО ПРО НАСЫЩЕННОСТЬ: ячейка рисуется как
	 * `color-mix(in srgb, var(--tint) 25%, var(--bg-secondary-2))`
	 * (см. games/slots/azumanga/+page.svelte) — то есть тинт РАЗБАВЛЯЕТСЯ
	 * вчетверо, и расстояние между двумя ячейками в sRGB равно четверти
	 * расстояния между сырыми хексами. Первый набор тинтов был подобран «на
	 * глаз» по сырым цветам и после разбавления схлопнулся: четыре LOW дали
	 * #474240 / #473843 / #393c4b / #3e394b — расстояние
	 * utau_note↔skull_cringe было 5.8, baguette_crumb↔drill_lollipop 10.4,
	 * то есть все четыре читались одним коричнево-лиловым; teto_0401 и
	 * teto_baguette_knight отличались на 14.2 и топ-символ выглядел как
	 * «ещё один HIGH». Поэтому тинты теперь насыщенные и разведены по тону:
	 * ЛЮБАЯ пара LOW и пара HIGH расходятся минимум на 30 ПОСЛЕ смешивания
	 * (проверено расчётом смешанных значений, а не сырых хексов).
	 *
	 * Палитра остаётся системной: три акцента из tokens.css достаются
	 * вайлду/скаттеру/топ-символу, ещё три взяты из семейств --positive,
	 * --destructive и --balance-gradient, остальные — промежуточные тона
	 * между ними (разбавленный вчетверо тинт это подложка ячейки, а не
	 * акцент хрома, поэтому правило «ровно три акцента» тут не нарушается).
	 */
	tint: string;
};

// Source of truth for ids/tiers: bot/services/teto_megablock.py
// (paytable numbers and reel weights stay server-only — never needed here).
export const TETO_SYMBOLS: Record<string, TetoSymbolMeta> = {
	// ─── WILD — динамический, вес 0 на барабанах, приходит из Дрель-Ханта ───
	golden_drill: {
		name: 'Золотая Дрель',
		tier: 'wild',
		src: '/slots/teto/golden_drill.webp',
		// --accent-yellow. Золото принадлежит ТОЛЬКО вайлду: у него же
		// единственное золотое кольцо в наборе (build_teto_symbols.py)
		tint: '#ffd84a'
	},

	// ─── SCATTER — игрок считает их глазами, тинт максимально «свой» ────────
	baguette_cerberus: {
		name: 'Багет-Цербер',
		tier: 'scatter',
		src: '/slots/teto/baguette_cerberus.webp',
		// --accent-pink
		tint: '#ff5b8d'
	},

	// ─── HIGH ──────────────────────────────────────────────────────────────
	teto_0401: {
		name: 'Тето 0401',
		tier: 'high',
		src: '/slots/teto/teto_0401.webp',
		// --accent-cyan: чистый акцент достаётся топ-символу, и его же
		// холодное кольцо на ассете — ячейка и рант больше не спорят
		tint: '#7be6ff'
	},
	teto_baguette_knight: {
		name: 'Тето Багет-Рыцарь',
		tier: 'high',
		src: '/slots/teto/teto_baguette_knight.webp',
		// индиго. Прежний #5fb8f3 из --balance-gradient после разбавления
		// отходил от teto_0401 всего на 14.2 — второй HIGH выглядел как
		// топ-символ; индиго даёт 40.7
		tint: '#7a45e8'
	},
	teto_drill_rage: {
		name: 'Тето Дрель-Рейдж',
		tier: 'high',
		src: '/slots/teto/teto_drill_rage.webp',
		// пурпурно-розовый: ярче прежнего #ffa8c6, но по тону уведён от
		// скаттерного --accent-pink
		tint: '#ff8fd8'
	},
	teto_chimera: {
		name: 'Тето-Химера',
		tier: 'high',
		src: '/slots/teto/teto_chimera.webp',
		// ядовито-зелёный из семейства --positive (#2ee06a): фиолетовый
		// сектор занят рыцарем и черепом, а «уродливая собака» с зелёной
		// подложкой читается ровно так, как задумано
		tint: '#4ef58c'
	},

	// ─── LOW — те же семейства, но обесцвеченные: борд не сливается в один
	//           тон, а «дешёвые» символы сразу читаются как дешёвые ──────────
	baguette_crumb: {
		name: 'Багет-Крошка',
		tier: 'low',
		src: '/slots/teto/baguette_crumb.webp',
		// лайм между --accent-yellow и --positive (груша жёлто-зелёная);
		// чистое золото занято вайлдом
		tint: '#a8ee3c'
	},
	drill_lollipop: {
		name: 'Дрель-Леденец',
		tier: 'low',
		src: '/slots/teto/drill_lollipop.webp',
		// терракота из семейства --destructive (#ff3838) — яблоко красное
		tint: '#f0503a'
	},
	utau_note: {
		name: 'УТАУ-Нотка',
		tier: 'low',
		src: '/slots/teto/utau_note.webp',
		// лазурь из --balance-gradient (#5fb8f3/#4990d8) — бирюзовое платье
		tint: '#3d8cf5'
	},
	skull_cringe: {
		name: 'Скулл-Кринж',
		tier: 'low',
		src: '/slots/teto/skull_cringe.webp',
		// орхидея: сам скетч серый, но его бумага перекрашена в лиловый
		// #d8d2e4, так что фиолетовая ветка — «свой» тон символа
		tint: '#d84af0'
	}
};

/** Порядок для пейтейбла: wild → scatter → high (сверху вниз) → low. */
export const TETO_PAYTABLE_ORDER: readonly string[] = [
	'golden_drill',
	'baguette_cerberus',
	'teto_0401',
	'teto_baguette_knight',
	'teto_drill_rage',
	'teto_chimera',
	'baguette_crumb',
	'drill_lollipop',
	'utau_note',
	'skull_cringe'
];

export function tetoSymbolSrc(id: string): string {
	return TETO_SYMBOLS[id]?.src ?? `/slots/teto/${id}.webp`;
}
