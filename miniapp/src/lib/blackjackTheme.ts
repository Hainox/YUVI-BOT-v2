// Card token format: rank + suit glyph (e.g. "A♠", "10♥") — see
// bot/services/blackjack_engine.py::new_shuffled_deck. Black suits (♠♣) are
// themed as Miku, red suits (♥♦) as Teto, per the "Мику vs Тето" card design
// (claude.ai/design project 29287ff0-7367-49ae-ba24-0bcb9553a6f9).

export type Rank = '2' | '3' | '4' | '5' | '6' | '7' | '8' | '9' | '10' | 'J' | 'Q' | 'K' | 'A';
export type SuitGlyph = '♠' | '♣' | '♥' | '♦';
export type Character = 'miku' | 'teto';

export interface CardTheme {
	character: Character;
	dark: string;
	main: string;
	tint: string;
}

const MIKU_THEME: CardTheme = { character: 'miku', dark: '#0C6B66', main: '#2EC4B6', tint: '#EAFBF9' };
const TETO_THEME: CardTheme = { character: 'teto', dark: '#8A1F33', main: '#D63859', tint: '#FDEEF1' };

export const SUIT_THEME: Record<SuitGlyph, CardTheme> = {
	'♠': MIKU_THEME,
	'♣': MIKU_THEME,
	'♥': TETO_THEME,
	'♦': TETO_THEME
};

// Original upload filenames kept their own extensions per rank/character —
// no re-encoding was done, so the mapping has to carry them explicitly.
const MIKU_EXT: Record<Rank, string> = {
	'2': 'jpg', '3': 'jpg', '4': 'jpg', '5': 'webp', '6': 'gif', '7': 'jpg',
	'8': 'jpg', '9': 'jpg', '10': 'jpg', J: 'jpg', Q: 'jpg', K: 'jpg', A: 'jpg'
};
const TETO_EXT: Record<Rank, string> = {
	'2': 'jpg', '3': 'jpg', '4': 'webp', '5': 'webp', '6': 'jpg', '7': 'jpg',
	'8': 'jpg', '9': 'webp', '10': 'webp', J: 'jpg', Q: 'jpg', K: 'jpg', A: 'png'
};

export function parseCard(token: string): { rank: Rank; suit: SuitGlyph } {
	const suit = token.slice(-1) as SuitGlyph;
	const rank = token.slice(0, -1) as Rank;
	return { rank, suit };
}

export function cardImage(rank: Rank, suit: SuitGlyph): string {
	const character = SUIT_THEME[suit].character;
	const ext = character === 'miku' ? MIKU_EXT[rank] : TETO_EXT[rank];
	return `/blackjack/${character}/${rank}.${ext}`;
}
