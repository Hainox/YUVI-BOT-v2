// Client-side mirror of bot/data/slot_data.py (SYMBOLS/PAYLINES only) — used
// PURELY for rendering (symbol thumbnails/names/tints, mapping a win's
// line_index back to grid cell coordinates to highlight). The server is the
// sole source of truth for the outcome (D-03/T-04.1-01): this file never
// computes wins/payouts/RNG, same "informational mirror" pattern already
// established for dice's client-side multiplier readout
// (games/dice/+page.svelte) and roulette's red/black coloring.

export type SlotSymbolMeta = {
	name: string;
	role: 'wild' | 'scatter' | 'high' | 'mid' | 'low';
	tint: string;
};

// Source of truth for these numbers: bot/data/slot_data.py::SYMBOLS
// (role/tint/name only — weights/paytable stay server-only, never needed
// client-side).
export const SLOT_SYMBOLS: Record<string, SlotSymbolMeta> = {
	muscle: { name: 'КАЧОК-ОСАКА', role: 'wild', tint: '#ffd84a' },
	keffiyeh: { name: 'ШЕЙХ-АКА', role: 'scatter', tint: '#ff5b8d' },
	gasp: { name: 'НИХУЯ', role: 'high', tint: '#7be6ff' },
	'lightning-eyes': { name: 'Osaka KYS', role: 'high', tint: '#c4a8ff' },
	dog: { name: 'Bruh….', role: 'mid', tint: '#ffb1c8' },
	'osaka-stand': { name: 'Гроши заработал', role: 'low', tint: '#ffe27a' },
	'bath-chibi': { name: 'Да-да, выиграл хуйню', role: 'low', tint: '#b8e7ff' },
	sakaki: { name: 'WTF OSAKA NIG….', role: 'low', tint: '#d6c4a3' }
};

export function symbolSrc(id: string): string {
	return `/symbols/${id}.jpg`;
}

// Source of truth: bot/data/slot_data.py::PAYLINES (10 lines, row-index per
// column 0..4). Used only to map a win's line_index back to grid cell
// [row, col] coordinates for the highlight flash — never influences payout.
export const SLOT_PAYLINES: number[][] = [
	[1, 1, 1, 1, 1], // 1 · middle
	[0, 0, 0, 0, 0], // 2 · top
	[2, 2, 2, 2, 2], // 3 · bottom
	[0, 1, 2, 1, 0], // 4 · V
	[2, 1, 0, 1, 2], // 5 · Λ
	[0, 0, 1, 2, 2], // 6 · descending
	[2, 2, 1, 0, 0], // 7 · ascending
	[1, 0, 0, 0, 1], // 8 · top U
	[1, 2, 2, 2, 1], // 9 · bottom U
	[0, 1, 0, 1, 0] // 10 · zigzag
];
