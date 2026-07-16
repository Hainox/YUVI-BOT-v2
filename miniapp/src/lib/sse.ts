// Client-side consumer of the already-shipped server SSE channel
// (api/routes/events.py, Phase 04-03). Native EventSource reconnects itself
// on drop — do NOT hand-roll a reconnect/backoff loop (04-RESEARCH.md
// Pattern 3 "Don't Hand-Roll").
//
// init_data travels as a QUERY param, not a header, because EventSource
// cannot set custom request headers — api/deps.py::extract_init_data
// already accepts both header and query for exactly this reason.
export function connectBalanceStream(
	chatId: number,
	initDataValue: string,
	onMessage: (data: unknown) => void
): EventSource {
	const url = `/api/v1/events?chat_id=${chatId}&init_data=${encodeURIComponent(initDataValue)}`;
	const source = new EventSource(url);
	source.onmessage = (event) => {
		try {
			onMessage(JSON.parse(event.data));
		} catch {
			// Malformed/heartbeat payload — ignore, the stream self-heals on the next message.
		}
	};
	return source;
}
