<script lang="ts">
	import '$lib/styles/tokens.css';
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import * as tg from '$lib/tg';
	import { apiFetch, setChatId, ApiError } from '$lib/api';
	import { balance } from '$lib/balance';
	import { connectBalanceStream } from '$lib/sse';

	let { children } = $props();

	let loading = $state(true);
	let error = $state<string | null>(null);
	let sse: EventSource | null = null;
	let userId = $state<number | null>(null);
	let idCopied = $state(false);

	const handle = tg.user
		? `@${tg.user.username || tg.user.first_name || `id${tg.user.id}`}`
		: 'гость';

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
			tg.haptic('light');
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
			error =
				err instanceof ApiError ? `${err.status}: ${err.message}` : String(err ?? 'unknown_error');
			loading = false;
			return;
		}

		if (parsed?.chatId != null) {
			sse = connectBalanceStream(parsed.chatId, tg.initData, (data) => {
				const payload = data as { balance?: number };
				if (typeof payload.balance === 'number') balance.set(payload.balance);
			});
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
</style>
