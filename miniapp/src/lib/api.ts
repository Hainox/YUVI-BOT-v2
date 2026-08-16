// Backend API client. TypeScript port of webapp/api.js, corrected to the
// real backend contract (api/deps.py::extract_init_data expects
// `X-Telegram-Init-Data`, NOT the prototype's `X-Init-Data`).
//
// Source: REFERENCE-XYLOZ.md §6 ("balance sniffing from any API response")
// + api/deps.py (header/query contract).
import { initData } from './tg';
import { applyBalanceUpdate } from './balance';

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

// Плоский fetch() без таймаута мог зависнуть НАВСЕГДА (потерянное
// соединение, перегруженный пул БД под нагрузкой и т.п.), и вызывающий код
// — например roll() в gacha/+page.svelte — блокировался в `await` без
// единого шанса дойти до своего finally: кнопки оставались задизейбленными
// бессрочно, без ошибки на экране, помогал только перезаход на экран.
// Жёсткий таймаут гарантирует, что любой fetch когда-нибудь ЗАВЕРШИТСЯ
// (успехом или ошибкой) — вызывающий код всегда получит шанс на finally.
//
// AbortController один не гарантирует это: если движок WebView (Telegram
// desktop/mobile — не всегда стоковый Chrome) не соблюдает AbortSignal при
// обрыве, controller.abort() ничего не даст и исходный fetch()-промис
// зависнет навсегда несмотря на таймер. Promise.race с отдельным
// отклоняющимся таймером не зависит от того, слушается ли abort() —
// apiFetch гарантированно осядет через REQUEST_TIMEOUT_MS (или переданный
// timeoutMs) в любом случае (заброшенный fetch-промис просто утилизируется
// сборщиком мусора позже).
const REQUEST_TIMEOUT_MS = 15000;
export const STREAM_CONNECT_TIMEOUT_MS = 10000;

// Найдено 2026-07-28: /api/v1/ai/* (topics/phrase/joke/q/card/digest/
// summary) реально зовут внешнюю LLM (bot/services/ai_client.py,
// AI_CALL_TIMEOUT_SEC=60 на бэкенде) — 15с общего дефолта клиент обрывал
// запрос "timeout" ДО того, как сервер вообще успевал ответить (даже успешным
// результатом), поэтому AI-экраны миниаппа передают этот более длинный
// таймаут явным `timeoutMs` третьим параметром вместо дефолта. Небольшой
// запас (65с) над серверными 60с — чтобы клиент никогда не сдавался раньше,
// чем сервер реально мог бы ответить.
export const AI_REQUEST_TIMEOUT_MS = 65000;export async function apiFetch<T>(
	path: string,
	init?: RequestInit,
	timeoutMs: number = REQUEST_TIMEOUT_MS
): Promise<T> {
	const url = new URL(path, window.location.origin);
	if (chatId !== null) url.searchParams.set('chat_id', String(chatId));

	const controller = new AbortController();
	const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
	const timeoutPromise = new Promise<never>((_, reject) => {
		setTimeout(() => reject(new ApiError(0, 'timeout')), timeoutMs);
	});

	let resp: Response;
	try {
		resp = await Promise.race([
			fetch(url.toString(), {
				...init,
				signal: controller.signal,
				headers: {
					...(init?.body ? { 'Content-Type': 'application/json' } : {}),
					...init?.headers,
					'X-Telegram-Init-Data': initData
				}
			}),
			timeoutPromise
		]);
	} catch (err) {
		if (err instanceof ApiError) throw err;
		if (err instanceof DOMException && err.name === 'AbortError') {
			throw new ApiError(0, 'timeout');
		}
		throw err;
	} finally {
		clearTimeout(timeoutId);
	}

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
	// the source of truth for OTHER tabs/actions. Routed through
	// applyBalanceUpdate (not balance.set directly) so screens with their own
	// multi-second reveal animation (the two slot screens) can hold this off
	// until their reveal actually finishes — see lib/balance.ts.
	const maybeBalance =
		(data as Record<string, unknown>)?.user_balance_after ??
		(data as Record<string, unknown>)?.balance;
	if (typeof maybeBalance === 'number') applyBalanceUpdate(maybeBalance);

	return data;
}

/**
 * Authenticated streaming request for Arena runtime SSE. Native EventSource
 * cannot send X-Telegram-Init-Data, so Arena uses fetch + ReadableStream and
 * keeps initData in the header rather than exposing it in proxy URLs.
 */
export async function apiStream(path: string, init?: RequestInit): Promise<Response> {
	const url = new URL(path, window.location.origin);
	if (chatId !== null) url.searchParams.set('chat_id', String(chatId));
	const controller = new AbortController();
	const externalSignal = init?.signal;
	const forwardAbort = () => controller.abort();
	if (externalSignal?.aborted) controller.abort();
	else externalSignal?.addEventListener('abort', forwardAbort, { once: true });
	const timeoutId = setTimeout(() => controller.abort(), STREAM_CONNECT_TIMEOUT_MS);
	const timeoutPromise = new Promise<never>((_, reject) => {
		setTimeout(() => reject(new ApiError(0, 'timeout')), STREAM_CONNECT_TIMEOUT_MS);
	});
	try {
		const response = await Promise.race([
			fetch(url.toString(), {
				...init,
				signal: controller.signal,
				headers: {
					...init?.headers,
					'X-Telegram-Init-Data': initData
				}
			}),
			timeoutPromise
		]);
		if (!response.ok) {
			let detail = '';
			try {
				detail = (await response.clone().json())?.detail ?? '';
			} catch {
				// Fall back to status text for non-JSON responses.
			}
			throw new ApiError(response.status, detail || response.statusText || `api_error_${response.status}`);
		}
		return response;
	} catch (err) {
		if (err instanceof DOMException && err.name === 'AbortError') throw new ApiError(0, 'timeout');
		throw err;
	} finally {
		clearTimeout(timeoutId);
		externalSignal?.removeEventListener('abort', forwardAbort);
	}
}
