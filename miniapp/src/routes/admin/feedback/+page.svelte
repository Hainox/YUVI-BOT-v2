<script lang="ts">
	// Admin feedback inbox — destination of the admin sub-hub's "Заявки" tile
	// (CASINO-03, D-05). Consumes GET/PATCH /api/v1/admin/feedback built in
	// plan 01 (api/routes/feedback.py), both gated by require_admin
	// server-side. A non-admin reaching this screen directly gets 403 on the
	// GET call, shown as the benign "Только для админов" state
	// (04.3-UI-SPEC.md Copywriting Contract), never a generic error.
	//
	// Row shape extends hist-row (history/+page.svelte) with a category tag
	// and a resolved/unresolved status pill; unlike history rows the full
	// text is shown (no truncation — admin needs it to act) and each row
	// grows to fit, so the layout is a column stack rather than hist-row's
	// single-line flex-between.
	import { onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	type FeedbackRow = {
		id: number;
		user_id: number | null;
		category: string;
		text: string;
		resolved: boolean;
		created_at: string;
	};

	// Same category taxonomy as feedback/+page.svelte's CATEGORY_CHIPS.
	const CATEGORY_LABELS: Record<string, string> = {
		bug: 'баг',
		idea: 'идея',
		complaint: 'жалоба',
		other: 'другое'
	};

	function categoryLabel(category: string): string {
		return CATEGORY_LABELS[category] ?? category;
	}

	function formatTime(iso: string): string {
		const d = new Date(iso);
		return d.toLocaleString('ru-RU', {
			day: '2-digit',
			month: '2-digit',
			hour: '2-digit',
			minute: '2-digit'
		});
	}

	let loading = $state(true);
	let error = $state<string | null>(null);
	let forbidden = $state(false);
	let items = $state<FeedbackRow[]>([]);
	let togglingId = $state<number | null>(null);

	onMount(async () => {
		try {
			items = await apiFetch<FeedbackRow[]>('/api/v1/admin/feedback');
		} catch (err) {
			if (err instanceof ApiError && err.status === 403) {
				forbidden = true;
			} else {
				error =
					err instanceof ApiError ? `${err.status}: ${err.message}` : String(err ?? 'unknown_error');
			}
		} finally {
			loading = false;
		}
	});

	async function toggleResolved(row: FeedbackRow) {
		if (togglingId !== null) return;
		togglingId = row.id;
		const next = !row.resolved;
		try {
			await apiFetch<{ status: string }>(`/api/v1/admin/feedback/${row.id}`, {
				method: 'PATCH',
				body: JSON.stringify({ resolved: next })
			});
			items = items.map((it) => (it.id === row.id ? { ...it, resolved: next } : it));
			haptic('tap');
		} catch (err) {
			error =
				err instanceof ApiError ? `${err.status}: ${err.message}` : String(err ?? 'unknown_error');
			haptic('error');
		} finally {
			togglingId = null;
		}
	}
</script>

<div class="fi-screen">
	<div class="menu-head">
		<h1 class="menu-title">Заявки</h1>
		<div class="menu-sub">фидбек от участников</div>
	</div>

	{#if loading}
		<div class="screen-loading"><span>загрузка заявок…</span></div>
	{:else if forbidden}
		<div class="fi-forbidden">
			<h2>Только для админов</h2>
			<div class="fi-forbidden-body">У тебя нет прав администратора в этом чате.</div>
		</div>
	{:else if error}
		<div class="fi-error">{error}</div>
	{:else if items.length === 0}
		<div class="fi-empty">
			<div class="fi-empty-jp">しずか</div>
			<div class="fi-empty-ru">заявок пока нет</div>
			<div class="fi-empty-hint">Как только кто-то отправит фидбек — он появится здесь.</div>
		</div>
	{:else}
		<div class="fi-list">
			{#each items as row (row.id)}
				<div class="fi-row">
					<div class="fi-row-meta">
						<span class="fi-category">{categoryLabel(row.category)}</span>
						<span class={`fi-pill ${row.resolved ? 'fi-pill-resolved' : 'fi-pill-unresolved'}`}>
							{row.resolved ? 'решено' : 'не решено'}
						</span>
					</div>
					<div class="fi-text">{row.text}</div>
					<div class="fi-author">id{row.user_id ?? '—'} · {formatTime(row.created_at)}</div>
					<button
						type="button"
						class="fi-toggle"
						disabled={togglingId === row.id}
						onclick={() => toggleResolved(row)}
					>
						{row.resolved ? 'Вернуть в работу' : 'Отметить решённым'}
					</button>
				</div>
			{/each}
		</div>
	{/if}
</div>

<style>
	.fi-screen {
		padding: 24px 18px 32px;
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
	}
	.menu-head {
		margin-bottom: var(--space-xs);
	}
	.menu-title {
		font-family: var(--font-chrome);
		font-size: var(--font-display-size);
		font-weight: 700;
		margin: 0;
		color: var(--text-primary);
	}
	.menu-sub {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		margin-top: var(--space-xs);
		letter-spacing: 0.04em;
		font-family: var(--font-body);
	}

	.fi-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.fi-forbidden {
		text-align: center;
		padding: var(--space-2xl) var(--space-lg);
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.fi-forbidden h2 {
		font-family: var(--font-chrome);
		font-size: var(--font-display-size);
		font-weight: 700;
		color: var(--text-secondary);
		margin: 0;
	}
	.fi-forbidden-body {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		font-family: var(--font-body);
		line-height: 1.5;
		max-width: 320px;
		margin: 0 auto;
	}

	.fi-empty {
		text-align: center;
		padding: var(--space-2xl) var(--space-lg);
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.fi-empty-jp {
		font-family: var(--font-jp);
		font-size: var(--font-heading-size);
		color: var(--text-locked);
	}
	.fi-empty-ru {
		font-family: var(--font-chrome);
		font-size: var(--font-display-size);
		font-weight: 700;
		color: var(--text-secondary);
		text-transform: lowercase;
	}
	.fi-empty-hint {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		font-family: var(--font-body);
		line-height: 1.5;
		max-width: 320px;
		margin: var(--space-xs) auto 0;
	}

	.fi-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.fi-row {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 12px;
		padding: var(--space-sm) var(--space-md);
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.fi-row-meta {
		display: flex;
		align-items: center;
		gap: var(--space-sm);
	}
	.fi-category {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		font-family: var(--font-body);
	}
	.fi-pill {
		border-radius: 20px;
		font-size: 10px;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 2px 8px;
		font-family: var(--font-body);
	}
	.fi-pill-resolved {
		background: var(--positive-bg);
		color: var(--positive-text);
	}
	.fi-pill-unresolved {
		background: var(--destructive-bg);
		color: var(--destructive-text);
	}
	.fi-text {
		font-size: var(--font-body-size);
		color: var(--text-secondary);
		font-family: var(--font-body);
		line-height: 1.5;
		white-space: pre-wrap;
		word-break: break-word;
	}
	.fi-author {
		font-size: 11px;
		color: var(--text-muted);
		font-family: var(--font-body);
	}
	.fi-toggle {
		align-self: flex-start;
		background: var(--bg-secondary-1);
		border: 1px solid var(--border-secondary);
		border-radius: 10px;
		padding: var(--space-xs) var(--space-md);
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		color: var(--text-primary);
		cursor: pointer;
		min-height: 44px;
	}
	.fi-toggle:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
</style>
