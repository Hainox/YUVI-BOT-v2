<script lang="ts">
	import '$lib/styles/tokens.css';
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import * as tg from '$lib/tg';
	import { apiFetch, setChatId, ApiError } from '$lib/api';
	import { balance, applyBalanceUpdate } from '$lib/balance';
	import { connectBalanceStream } from '$lib/sse';

	let { children } = $props();

	let loading = $state(true);
	let error = $state<string | null>(null);
	let notSubscribed = $state(false);
	let sseExpired = $state(false);
	let sse: EventSource | null = null;
	let userId = $state<number | null>(null);
	let idCopied = $state(false);
	let showTwinPrompt = $state(false);
	let twinDeciding = $state(false);

	const handle = tg.user
		? `@${tg.user.username || tg.user.first_name || `id${tg.user.id}`}`
		: 'гость';

	// HAVD-01 (запрошено 2026-07-24) — держим в синхроне со
	// settings.havd_channel_username (bot/config.py), сама ссылка не
	// секретна и не требует отдельного эндпоинта ради одной константы.
	const HAVD_CHANNEL_URL = 'https://t.me/havdaily';

	// feedback #8: раньше единственным способом сообщить свой ID
	// получателю перевода/дуэли было написать его числом в чате вручную —
	// нет ни отображения, ни копирования. userId приходит из /me (ниже),
	// не из initDataUnsafe.user.id (не валидированное Telegram-поле, api.ts
	// его специально не использует ни для чего серверного).
	async function copyUserId() {
		if (userId === null) return;
		try {
			await navigator.clipboard.writeText(String(userId));
			idCopied = true;
			tg.haptic('tap');
			setTimeout(() => (idCopied = false), 1500);
		} catch {
			// Clipboard API недоступен (старый WebView) — тихо игнорируем,
			// ID всё равно виден на экране для ручного копирования.
		}
	}

	async function decideTwin(optIn: boolean) {
		if (twinDeciding) return;
		twinDeciding = true;
		try {
			await apiFetch(optIn ? '/api/v1/twin/optin' : '/api/v1/twin/decline', { method: 'POST' });
			tg.haptic(optIn ? 'win' : 'tap');
		} catch {
			// Не удалось сохранить ответ — промпт всё равно закрываем, чтобы не
			// блокировать вход; при следующем открытии status снова окажется
			// "не спрошен" и вопрос повторится.
		} finally {
			showTwinPrompt = false;
			twinDeciding = false;
		}
	}

	onMount(async () => {
		tg.init();

		// Deep-link: t.me/<bot>?startapp=<chatId>[_route] (04.2-RESEARCH.md).
		const parsed = tg.parseStartParam(tg.startParam);
		if (parsed?.chatId != null) setChatId(parsed.chatId);

		try {
			const me = await apiFetch<{ balance: number; user_id: number }>('/api/v1/me');
			balance.set(me.balance);
			userId = me.user_id;
		} catch (err) {
			// Spoofing mitigation (T-04.2-05): on 401/membership failure, show the
			// locked error screen — never fall back to a degraded/fake-data mode.
			// "not_subscribed_to_channel" (HAVD-01, api/deps.py::require_membership)
			// is a machine-readable detail — matched by value to show a dedicated
			// "subscribe to the channel" screen instead of the generic error one.
			if (err instanceof ApiError && err.message === 'not_subscribed_to_channel') {
				notSubscribed = true;
			} else {
				error =
					err instanceof ApiError ? `${err.status}: ${err.message}` : String(err ?? 'unknown_error');
			}
			loading = false;
			return;
		}

		if (parsed?.chatId != null) {
			sse = connectBalanceStream(
				parsed.chatId,
				tg.initData,
				(data) => {
					const payload = data as { balance?: number };
					// applyBalanceUpdate (not balance.set) — a slot screen mid-reveal
					// may be holding updates back (see lib/balance.ts) so its own
					// animation isn't spoiled by this broadcast arriving early.
					if (typeof payload.balance === 'number') applyBalanceUpdate(payload.balance);
				},
				() => {
					sseExpired = true;
				}
			);
		}

		loading = false;

		// Onboarding-промпт AI-двойника (запрошено пользователем 2026-07-23):
		// показывается ОДИН раз — пока у участника нет строки twin_opt_ins
		// (asked=false). Best-effort, как и остальные некритичные фоновые
		// вызовы этого layout — сбой не должен блокировать сам вход в приложение.
		try {
			const twinStatus = await apiFetch<{ asked: boolean }>('/api/v1/twin/status');
			showTwinPrompt = !twinStatus.asked;
		} catch {
			// Не блокирует загрузку — просто не покажем промпт в этот раз.
		}

		if (parsed?.route) {
			const target = `/${parsed.route}`;
			if (target !== page.url.pathname) {
				goto(target).catch(() => {
					// Unbuilt route — SPA-fallback stays on the current screen.
				});
			}
		}
	});

	onDestroy(() => {
		sse?.close();
	});

	// BackButton: shown on every screen except the hub root.
	$effect(() => {
		if (page.url.pathname === '/') {
			tg.hideBack();
		} else {
			tg.showBack(() => history.back());
		}
	});
</script>

{#if loading}
	<div class="screen-loading"><span>загрузка…</span></div>
{:else if notSubscribed}
	<div class="screen-error">
		<h2>Нужна подписка на канал</h2>
		<div class="err-hint">
			Доступ к играм и остальным функциям бота открыт только подписчикам HAVD. Подпишись на канал
			и попробуй снова.
		</div>
		<a class="havd-subscribe-link" href={HAVD_CHANNEL_URL} target="_blank" rel="noopener"
			>Подписаться</a
		>
		<button type="button" onclick={() => location.reload()}>Я подписался, проверить снова</button>
	</div>
{:else if error}
	<div class="screen-error">
		<h2>Ошибка соединения</h2>
		<div class="err-msg">{error}</div>
		<div class="err-hint">
			Сервер недоступен или доступ закрыт. Если открыл это вне Telegram — так и должно быть;
			запусти через бота командой /casino.
		</div>
		<button type="button" onclick={() => location.reload()}>Повторить</button>
	</div>
{:else}
	<div class="webapp-root">
		{#if sseExpired}
			<div class="sse-expired-banner">
				<span>Сессия истекла — живые обновления остановлены.</span>
				<button type="button" onclick={() => location.reload()}>Перезайти</button>
			</div>
		{/if}
		<div class="screen">
			<div class="balance-card app-balance-header">
				<div class="bc-handle-row">
					<div class="bc-handle">{handle}</div>
					{#if userId !== null}
						<button type="button" class="bc-id" onclick={copyUserId} title="Скопировать свой ID">
							{idCopied ? 'скопировано ✓' : `ID ${userId}`}
						</button>
					{/if}
				</div>
				<div class="bc-amount">
					<span class="bc-val">{($balance ?? 0).toLocaleString('ru-RU')}</span>
					<span class="bc-unit">¥ юви</span>
				</div>
			</div>
			{@render children()}
		</div>
	</div>

	{#if showTwinPrompt}
		<div class="twin-prompt-backdrop">
			<div class="twin-prompt">
				<div class="twin-prompt-title">🤖 AI-двойник</div>
				<div class="twin-prompt-desc">
					Хочешь подключить своего AI-двойника? Он сможет отвечать в твоём стиле, когда его
					позовут командой /twin. Решение можно поменять в любой момент (/twin_pause,
					/twin_optout).
				</div>
				<div class="twin-prompt-actions">
					<button
						type="button"
						class="chip"
						disabled={twinDeciding}
						onclick={() => decideTwin(false)}
					>
						нет
					</button>
					<button
						type="button"
						class="chip chip-all"
						disabled={twinDeciding}
						onclick={() => decideTwin(true)}
					>
						подключить
					</button>
				</div>
			</div>
		</div>
	{/if}
{/if}

<style>
	.app-balance-header {
		margin: var(--space-md) var(--space-md) 0;
	}

	.sse-expired-banner {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-sm);
		padding: var(--space-sm) var(--space-md);
		background: var(--bg-secondary-2);
		border-bottom: 1px solid var(--border-secondary);
		font-size: var(--font-body-size);
		color: var(--text-muted);
	}

	.sse-expired-banner button {
		flex-shrink: 0;
	}

	.havd-subscribe-link {
		display: inline-block;
		background: #2a6fdb;
		color: #fff;
		border-radius: 8px;
		padding: 10px 20px;
		font-size: var(--font-body-size);
		font-family: var(--font-body);
		font-weight: 700;
		text-decoration: none;
	}

	.twin-prompt-backdrop {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.6);
		display: flex;
		align-items: center;
		justify-content: center;
		padding: var(--space-lg);
		z-index: 100;
	}
	.twin-prompt {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-lg);
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
		max-width: 360px;
	}
	.twin-prompt-title {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		font-weight: 700;
		color: var(--text-primary);
	}
	.twin-prompt-desc {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		line-height: 1.5;
		font-family: var(--font-body);
	}
	.twin-prompt-actions {
		display: flex;
		gap: var(--space-sm);
		margin-top: var(--space-xs);
	}
	.twin-prompt-actions .chip {
		flex: 1;
	}
</style>
