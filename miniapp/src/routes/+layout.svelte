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

</style>
