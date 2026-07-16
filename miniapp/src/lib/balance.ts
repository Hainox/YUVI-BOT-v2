// Cross-component balance store. Seeded once from GET /me, then kept live by
// two independent updaters: lib/sse.ts (other tabs/actions) and lib/api.ts's
// balance-sniffing (this tab's own action, instant feedback without waiting
// for the SSE round-trip). `writable` (not runes) is used deliberately —
// this is shared cross-file state read by many unrelated screens, which is
// exactly the Store API's job in Svelte 5 (runes are for local/component state).
import { writable } from 'svelte/store';

export const balance = writable<number | null>(null);
