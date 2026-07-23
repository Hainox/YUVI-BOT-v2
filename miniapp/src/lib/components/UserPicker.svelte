<script lang="ts">
	// Автокомплит получателя по @username/имени (feedback #8) — GET
	// /api/v1/members. Раньше единственный способ адресовать перевод/дуэль
	// был знать числовой Telegram ID наизусть (см. +layout.svelte's bc-id
	// для обратной стороны той же фичи — показ/копирование СВОЕГО ID).
	//
	// value — резолвленный числовой ID: либо выбран из списка, либо введён
	// напрямую (получателя может не быть в /members, если он ни разу не
	// писал в чат и не попал в таблицу users — прямой ввод ID остаётся
	// рабочим фолбэком, не убран).
	import { apiFetch } from '$lib/api';

	type Member = { tg_id: number; username: string | null; fullname: string };

	let {
		value = $bindable(null),
		label = 'Получатель',
		placeholder = '@ник, имя или ID'
	}: {
		value?: number | null;
		label?: string;
		placeholder?: string;
	} = $props();

	let query = $state('');
	let results = $state<Member[]>([]);
	let showResults = $state(false);
	let searching = $state(false);
	let debounceTimer: ReturnType<typeof setTimeout> | undefined;

	function memberLabel(m: Member): string {
		return m.username ? `@${m.username}` : m.fullname;
	}

	function onInput() {
		showResults = true;
		const asNumber = Number(query.trim());
		value = query.trim() && Number.isInteger(asNumber) && asNumber > 0 ? asNumber : null;

		clearTimeout(debounceTimer);
		debounceTimer = setTimeout(search, 220);
	}

	async function search() {
		const q = query.trim();
		if (q.length < 2) {
			results = [];
			return;
		}
		searching = true;
		try {
			const resp = await apiFetch<{ items: Member[] }>(
				`/api/v1/members?q=${encodeURIComponent(q)}`
			);
			results = resp.items;
		} catch {
			results = [];
		} finally {
			searching = false;
		}
	}

	function pick(m: Member) {
		value = m.tg_id;
		query = memberLabel(m);
		showResults = false;
		results = [];
	}
</script>

<label class="up-field">
	<span class="up-label">{label}</span>
	<div class="up-wrap">
		<input
			class="up-input"
			type="text"
			autocomplete="off"
			{placeholder}
			bind:value={query}
			oninput={onInput}
			onfocus={() => (showResults = results.length > 0)}
			onblur={() => setTimeout(() => (showResults = false), 150)}
		/>
		{#if showResults && (results.length > 0 || searching)}
			<ul class="up-dropdown">
				{#if searching}
					<li class="up-hint">поиск…</li>
				{:else}
					{#each results as m (m.tg_id)}
						<li>
							<button type="button" onmousedown={() => pick(m)}>{memberLabel(m)}</button>
						</li>
					{/each}
				{/if}
			</ul>
		{/if}
	</div>
</label>

<style>
	.up-field {
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.up-label {
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
	}
	.up-wrap {
		position: relative;
	}
	.up-input {
		width: 100%;
		box-sizing: border-box;
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 10px;
		padding: var(--space-sm) var(--space-md);
		font-family: var(--font-body);
		font-size: var(--font-heading-size);
		color: var(--text-primary);
	}
	.up-input:focus {
		outline: none;
		border-color: var(--accent-pink);
	}
	.up-dropdown {
		position: absolute;
		z-index: 10;
		top: calc(100% + 4px);
		left: 0;
		right: 0;
		margin: 0;
		padding: var(--space-xs);
		list-style: none;
		background: var(--bg-secondary-1);
		border: 2px solid var(--border-secondary);
		border-radius: 10px;
		max-height: 220px;
		overflow-y: auto;
	}
	.up-dropdown li + li {
		margin-top: 2px;
	}
	.up-dropdown button {
		width: 100%;
		text-align: left;
		background: transparent;
		border: none;
		border-radius: 8px;
		padding: var(--space-sm) var(--space-sm);
		font-family: inherit;
		font-size: var(--font-body-size);
		color: var(--text-primary);
		cursor: pointer;
	}
	.up-dropdown button:hover,
	.up-dropdown button:active {
		background: var(--bg-secondary-2);
	}
	.up-hint {
		padding: var(--space-sm);
		font-size: var(--font-body-size);
		color: var(--text-muted);
	}
</style>
