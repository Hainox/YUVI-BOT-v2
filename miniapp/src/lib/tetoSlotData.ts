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
	 * slotData.ts. Derived from the token palette (styles/tokens.css) so the
	 * board stays on-system: --accent-yellow for the wild, --accent-pink for
	 * the scatter, the cyan/violet family for high, desaturated for low.
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
		// --accent-yellow: единственный золотой в наборе, самый заметный тинт
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
		// --accent-cyan: чистый акцент достаётся топ-символу
		tint: '#7be6ff'
	},
	teto_baguette_knight: {
		name: 'Тето Багет-Рыцарь',
		tier: 'high',
		src: '/slots/teto/teto_baguette_knight.webp',
		// синий из --balance-gradient (#5fb8f3) — глубже, чем accent-cyan
		tint: '#5fb8f3'
	},
	teto_drill_rage: {
		name: 'Тето Дрель-Рейдж',
		tier: 'high',
		src: '/slots/teto/teto_drill_rage.webp',
		// светлый розово-лиловый: заметно бледнее скаттерного --accent-pink
		tint: '#ffa8c6'
	},
	teto_chimera: {
		name: 'Тето-Химера',
		tier: 'high',
		src: '/slots/teto/teto_chimera.webp',
		// фиолетовый (тот же, что у high в slotData.ts)
		tint: '#c4a8ff'
	},

	// ─── LOW — те же семейства, но обесцвеченные: борд не сливается в один
	//           тон, а «дешёвые» символы сразу читаются как дешёвые ──────────
	baguette_crumb: {
		name: 'Багет-Крошка',
		tier: 'low',
		src: '/slots/teto/baguette_crumb.webp',
		// приглушённая жёлтая ветка (грушевый хаки)
		tint: '#c9c08a'
	},
	drill_lollipop: {
		name: 'Дрель-Леденец',
		tier: 'low',
		src: '/slots/teto/drill_lollipop.webp',
		// приглушённая розовая ветка (яблочная терракота)
		tint: '#c99a97'
	},
	utau_note: {
		name: 'УТАУ-Нотка',
		tier: 'low',
		src: '/slots/teto/utau_note.webp',
		// приглушённая циановая ветка
		tint: '#8fa8b8'
	},
	skull_cringe: {
		name: 'Скулл-Кринж',
		tier: 'low',
		src: '/slots/teto/skull_cringe.webp',
		// приглушённая лиловая ветка (скетч сам по себе серый)
		tint: '#a29cb8'
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
