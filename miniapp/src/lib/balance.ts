// Cross-component balance store. Seeded once from GET /me, then kept live by
// two independent updaters: lib/sse.ts (other tabs/actions) and lib/api.ts's
// balance-sniffing (this tab's own action, instant feedback without waiting
// for the SSE round-trip). `writable` (not runes) is used deliberately —
// this is shared cross-file state read by many unrelated screens, which is
// exactly the Store API's job in Svelte 5 (runes are for local/component state).
import { writable } from 'svelte/store';

export const balance = writable<number | null>(null);

// Both updaters above apply the instant their source resolves — fine for
// instant-result games (coinflip/dice/roulette/blackjack), but the two slot
// screens (Azumanga/Teto) reveal their own result over several seconds of
// client-side cascade/bonus-round animation. Without a hold, the header
// balance already shows the new total before the reveal even starts (the
// direct API response) or partway through it (the SSE broadcast, published
// server-side at settle time — same moment, a separate channel) — spoiling
// the result and reading as "the win didn't register" (reported by players
// 2026-07-28 for Azumanga's bonus round specifically; same root cause hits
// the base-game reveal and Teto too, since neither screen's own state is the
// leak — this shared store is). `applyBalanceUpdate` is what both updaters
// call instead of `balance.set` directly; while held, updates queue up (last
// one wins, whichever channel delivers it) and apply the moment the slot
// screen releases the hold at its own reveal-complete point.
let held = false;
let pendingValue: number | null = null;

export function holdBalanceUpdates(hold: boolean): void {
	held = hold;
	if (!hold && pendingValue !== null) {
		balance.set(pendingValue);
		pendingValue = null;
	}
}

export function applyBalanceUpdate(value: number): void {
	if (held) {
		pendingValue = value;
	} else {
		balance.set(value);
	}
}
