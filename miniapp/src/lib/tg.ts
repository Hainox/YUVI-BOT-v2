// Telegram WebApp SDK shim (TypeScript port of webapp/telegram.js, the
// already-approved React-prototype shim referenced by 04-UI-SPEC.md's
// Component Inventory). Every method is a safe no-op outside Telegram.
//
// Source: webapp/telegram.js + core.telegram.org/bots/webapps (CSS vars),
// extended with parseStartParam (REFERENCE-XYLOZ.md §6) and
// enableClosingConfirmation (prevents accidental swipe-close mid-bet).

export type TgUser = { id: number; username?: string; first_name?: string };

type TelegramWebApp = {
	initData: string;
	initDataUnsafe?: { user?: TgUser; start_param?: string };
	ready: () => void;
	expand: () => void;
	enableClosingConfirmation?: () => void;
	disableVerticalSwipes?: () => void;
	HapticFeedback?: {
		impactOccurred: (style: 'light' | 'rigid' | 'soft' | 'medium' | 'heavy') => void;
		notificationOccurred: (type: 'success' | 'warning' | 'error') => void;
	};
	BackButton?: {
		show: () => void;
		hide: () => void;
		onClick: (cb: () => void) => void;
	};
};

declare global {
	interface Window {
		Telegram?: { WebApp?: TelegramWebApp };
	}
}

const raw: TelegramWebApp | undefined =
	typeof window !== 'undefined' ? window.Telegram?.WebApp : undefined;

export const inTelegram = !!(raw && raw.initData);
export const initData = inTelegram ? raw!.initData : '';
export const user: TgUser | null = inTelegram ? (raw!.initDataUnsafe?.user ?? null) : null;

export function init(): void {
	if (!inTelegram) return;
	try {
		raw!.ready();
		raw!.expand();
		raw!.enableClosingConfirmation?.();
		raw!.disableVerticalSwipes?.();
	} catch {
		// Telegram WebApp bridge unavailable — no-op outside a real client.
	}
}

/**
 * Parses the `start_param` from a `t.me/<bot>?startapp=<chatId>[_route]`
 * deep-link. Source: REFERENCE-XYLOZ.md §6.
 */
export function parseStartParam(
	rawParam: string | undefined
): { chatId: number; route: string | null } | null {
	if (!rawParam) return null;
	const match = /^(-?\d+)(?:_([a-z]+))?$/.exec(rawParam);
	if (!match) return null;
	return { chatId: Number(match[1]), route: match[2] ?? null };
}

export type HapticKind =
	| 'tap'
	| 'spin'
	| 'reel-stop'
	| 'win'
	| 'big-win'
	| 'lose'
	| 'scatter'
	| 'error';

export function haptic(kind: HapticKind): void {
	if (!inTelegram || !raw!.HapticFeedback) return;
	try {
		switch (kind) {
			case 'tap':
			case 'spin':
				raw!.HapticFeedback.impactOccurred('light');
				break;
			case 'reel-stop':
				raw!.HapticFeedback.impactOccurred('rigid');
				break;
			case 'win':
			case 'big-win':
				raw!.HapticFeedback.notificationOccurred('success');
				break;
			case 'lose':
				raw!.HapticFeedback.impactOccurred('soft');
				break;
			case 'scatter':
				raw!.HapticFeedback.notificationOccurred('warning');
				break;
			case 'error':
				raw!.HapticFeedback.notificationOccurred('error');
				break;
		}
	} catch {
		// Haptics unavailable outside a mobile client — no-op.
	}
}

// BackButton: a single active onBack callback, same shim shape as the prototype.
let backHandler: (() => void) | null = null;

export function showBack(onBack: () => void): void {
	backHandler = onBack;
	try {
		raw?.BackButton?.show();
	} catch {
		// no-op
	}
}

export function hideBack(): void {
	backHandler = null;
	try {
		raw?.BackButton?.hide();
	} catch {
		// no-op
	}
}

if (inTelegram && raw?.BackButton) {
	try {
		raw.BackButton.onClick(() => backHandler?.());
	} catch {
		// no-op
	}
}
