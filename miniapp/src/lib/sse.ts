// Client-side consumer of the already-shipped server SSE channel
// (api/routes/events.py, Phase 04-03). Native EventSource reconnects itself
// on drop — do NOT hand-roll a reconnect/backoff loop (04-RESEARCH.md
// Pattern 3 "Don't Hand-Roll"). The one case that must NOT keep retrying
// forever: initData expires after MINI_APP_INIT_DATA_TTL_SEC (1h, same root
// cause already fixed for farm-tap in 0c48c4f) and the browser has no way
// to mint a fresh one without reopening the app — every reconnect after
// that point is guaranteed to fail the same way, silently, forever.
// onerror probes GET /api/v1/me (cheap, already used elsewhere) to tell
// "auth truly expired" (401) apart from a transient network drop; only on
// 401 do we stop the stream and report upward via onAuthExpired.
//
// init_data travels as a QUERY param, not a header, because EventSource
// cannot set custom request headers — api/deps.py::extract_init_data
// already accepts both header and query for exactly this reason.
import { apiFetch, ApiError } from './api';

export function connectBalanceStream(
	chatId: number,
	initDataValue: string,
	onMessage: (data: unknown) => void,
	onAuthExpired: () => void
): EventSource {
	const url = `/api/v1/events?chat_id=${chatId}&init_data=${encodeURIComponent(initDataValue)}`;
	const source = new EventSource(url);
	let probing = false;

	source.onmessage = (event) => {
		try {
			onMessage(JSON.parse(event.data));
		} catch {
			// Malformed/heartbeat payload — ignore, the stream self-heals on the next message.
		}
	};

	source.onerror = () => {
		if (probing) return;
		probing = true;
		apiFetch('/api/v1/me')
			.catch((err) => {
				if (err instanceof ApiError && err.status === 401) {
					source.close();
					onAuthExpired();
				}
				// any other failure (network blip, 5xx) — transient, let EventSource retry on its own.
			})
			.finally(() => {
				probing = false;
			});
	};

	return source;
}
