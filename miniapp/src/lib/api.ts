// Backend API client. TypeScript port of webapp/api.js, corrected to the
// real backend contract (api/deps.py::extract_init_data expects
// `X-Telegram-Init-Data`, NOT the prototype's `X-Init-Data`).
//
// Source: REFERENCE-XYLOZ.md §6 ("balance sniffing from any API response")
// + api/deps.py (header/query contract).
import { initData } from './tg';
import { balance } from './balance';

let chatId: number | null = null;

export function setChatId(id: number): void {
	chatId = id;
}

export class ApiError extends Error {
	status: number;
	constructor(status: number, message: string) {
		super(message);
		this.status = status;
		this.name = 'ApiError';
	}
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
	const url = new URL(path, window.location.origin);
	if (chatId !== null) url.searchParams.set('chat_id', String(chatId));

	const resp = await fetch(url.toString(), {
		...init,
		headers: {
			...(init?.body ? { 'Content-Type': 'application/json' } : {}),
			...init?.headers,
			'X-Telegram-Init-Data': initData
		}
	});

	if (!resp.ok) {
		let detail = '';
		try {
			detail = (await resp.json())?.detail ?? '';
		} catch {
			// non-JSON error body — fall back to statusText below
		}
		throw new ApiError(resp.status, detail || resp.statusText || `api_error_${resp.status}`);
	}

	const data = (await resp.json()) as T;

	// Balance sniffing: any response carrying a known balance field updates
	// the shared store immediately — the tab that triggered the action gets
	// instant feedback without waiting for the SSE round-trip. SSE remains
	// the source of truth for OTHER tabs/actions.
	const maybeBalance =
		(data as Record<string, unknown>)?.user_balance_after ??
		(data as Record<string, unknown>)?.balance;
	if (typeof maybeBalance === 'number') balance.set(maybeBalance);

	return data;
}
