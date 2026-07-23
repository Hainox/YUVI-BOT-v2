<script lang="ts">
	// Farm — tap/upgrade/convert clicker economy (FARM-01/02, GACHA-02).
	// 04.2-UI-SPEC.md §Component Inventory: "tap surface = big circular
	// pink-accented button with haptic tap per accepted click; upgrade rows
	// reuse hist-row grid shape; AMM price-impact graphic is a small inline
	// SVG line chart on #1c1827".
	//
	// Server is the sole source of truth for CP (D-03/T-04.1-12/T-04.2-07) —
	// taps are batched client-side (accumulate count + elapsed_ms) and
	// flushed on an interval/threshold to POST /farm/tap; the response
	// ALWAYS reconciles the displayed CP with the server's authoritative
	// value (`syncFarm`), the local optimistic counter is never trusted as
	// truth. tap_level/upgrade-cost formulas rendered here are informational
	// client-side mirrors of the public clicker_service formulas (same
	// pattern already used by the dice/roulette screens' multiplier
	// readouts) — they never influence what the server actually accepts/
	// charges.
	import { onDestroy, onMount } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	const FLUSH_INTERVAL_MS = 1500;
	const FLUSH_THRESHOLD = 20;
	const TAP_UPGRADE_BASE = 50;
	const AUTO_UPGRADE_BASE = 200;
	const UPGRADE_GROWTH = 1.15;
	const ANCHOR_CP_PER_HRYVNA = 100;
	const TIER_FRACTIONS = [0.1, 0.25, 0.5, 0.75, 1];
	const QUOTE_DEBOUNCE_MS = 250;

	type FarmState = {
		cp: number;
		tap_level: number;
		auto_level: number;
		cp_per_sec?: number;
		accepted?: number;
	};
	type MarketQuote = { cp_in: number; hryvnia_out: number };
	type MarketState = {
		price: number | string;
		r_cp: number | string;
		r_h: number | string;
		history: { price: number | string; created_at: string }[];
		quotes: MarketQuote[];
	};
	type ConvertResult = { cp_in: number; hryvnia_out: number; price: number | string };
	type TierQuote = {
		amount: number;
		hryvniaOut: number;
		effectivePrice: number | null;
		impactPct: number | null;
	};

	let loading = $state(true);
	let error = $state<string | null>(null);

	let cp = $state(0);
	let optimisticCp = $state(0);
	let tapLevel = $state(1);
	let autoLevel = $state(0);
	let cpPerSec = $state(0);

	let pendingCount = 0;
	let batchStartedAt: number | null = null;
	let flushing = false;

	let upgradingTap = $state(false);
	let upgradingAuto = $state(false);

	let convertAmount = $state(100);
	let converting = $state(false);
	let convertResult = $state<ConvertResult | null>(null);

	let market = $state<MarketState | null>(null);

	function syncFarm(state: FarmState) {
		cp = state.cp;
		optimisticCp = state.cp;
		tapLevel = state.tap_level;
		autoLevel = state.auto_level;
		if (state.cp_per_sec !== undefined) cpPerSec = state.cp_per_sec;
	}

	function describeError(err: unknown): string {
		return err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
	}

	async function loadFarm() {
		try {
			syncFarm(await apiFetch<FarmState>('/api/v1/farm'));
		} catch (err) {
			error = describeError(err);
		} finally {
			loading = false;
		}
	}

	async function loadMarket(amounts: number[] = []) {
		try {
			const qs = amounts.length ? `?${amounts.map((a) => `amounts=${a}`).join('&')}` : '';
			market = await apiFetch<MarketState>(`/api/v1/farm/market${qs}`);
		} catch {
			// AMM-график/котировки — некритичное украшение экрана, не блокирует остальное.
		}
	}

	function tierAmounts(currentCp: number): number[] {
		if (currentCp <= 0) return [];
		const raw = TIER_FRACTIONS.map((f) => Math.floor(currentCp * f));
		return [...new Set(raw)].filter((v) => v > 0).sort((a, b) => a - b);
	}

	function computeTierQuotes(mkt: MarketState | null, currentCp: number): TierQuote[] {
		if (!mkt) return [];
		const spotPrice = Number(mkt.price);
		const result: TierQuote[] = [];
		for (const amount of tierAmounts(currentCp)) {
			const q = mkt.quotes.find((qq) => qq.cp_in === amount);
			if (!q) continue;
			const effectivePrice = q.hryvnia_out > 0 ? amount / q.hryvnia_out : null;
			const impactPct =
				effectivePrice !== null && spotPrice > 0
					? ((spotPrice - effectivePrice) / spotPrice) * 100
					: null;
			result.push({ amount, hryvniaOut: q.hryvnia_out, effectivePrice, impactPct });
		}
		return result;
	}

	function findQuote(mkt: MarketState | null, amount: number): number | null {
		if (!mkt || amount <= 0) return null;
		const q = mkt.quotes.find((qq) => qq.cp_in === Math.floor(amount));
		return q ? q.hryvnia_out : null;
	}

	function formatCp(n: number): string {
		if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
		if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
		return `${n}`;
	}

	function anchorRatioLabel(price: number | null): string | null {
		if (price === null) return null;
		const ratio = price / ANCHOR_CP_PER_HRYVNA;
		if (Math.abs(ratio - 1) < 0.01) return null;
		return ratio > 1
			? `CP дешевле анкера в ${ratio.toFixed(1)}×`
			: `CP дороже анкера в ${(1 / ratio).toFixed(1)}×`;
	}

	function historyRange(history: MarketState['history']): { min: string; max: string } | null {
		if (history.length < 2) return null;
		const prices = history.map((h) => Number(h.price));
		return { min: Math.min(...prices).toFixed(1), max: Math.max(...prices).toFixed(1) };
	}

	function impactCurvePoints(quotes: TierQuote[]): string {
		if (quotes.length < 2) return '';
		const vals = quotes.map((q) => Math.abs(q.impactPct ?? 0));
		const max = Math.max(...vals, 0.0001);
		return vals
			.map((v, i) => {
				const x = (i / (vals.length - 1)) * 100;
				const y = 32 - (v / max) * 32;
				return `${x.toFixed(1)},${y.toFixed(1)}`;
			})
			.join(' ');
	}

	function tapOnce() {
		optimisticCp += tapLevel;
		pendingCount += 1;
		if (batchStartedAt === null) batchStartedAt = Date.now();
		haptic('tap');
		if (pendingCount >= FLUSH_THRESHOLD) flushTaps();
	}

	async function flushTaps() {
		if (flushing || pendingCount === 0 || batchStartedAt === null) return;
		flushing = true;
		const count = pendingCount;
		const elapsedMs = Math.max(1, Date.now() - batchStartedAt);
		pendingCount = 0;
		batchStartedAt = null;
		try {
			syncFarm(
				await apiFetch<FarmState>('/api/v1/farm/tap', {
					method: 'POST',
					body: JSON.stringify({ count, elapsed_ms: elapsedMs })
				})
			);
		} catch (err) {
			error = describeError(err);
		} finally {
			flushing = false;
		}
	}

	async function upgrade(kind: 'tap' | 'auto') {
		if (kind === 'tap') upgradingTap = true;
		else upgradingAuto = true;
		error = null;
		try {
			syncFarm(await apiFetch<FarmState>(`/api/v1/farm/upgrade/${kind}`, { method: 'POST' }));
			haptic('win');
		} catch (err) {
			error = describeError(err);
			haptic('error');
		} finally {
			if (kind === 'tap') upgradingTap = false;
			else upgradingAuto = false;
		}
	}

	async function convert() {
		if (converting || convertAmount <= 0 || convertAmount > cp) return;
		converting = true;
		error = null;
		convertResult = null;
		try {
			const res = await apiFetch<ConvertResult>('/api/v1/farm/convert', {
				method: 'POST',
				body: JSON.stringify({
					cp_in: Math.floor(convertAmount),
					ref_id: `farm_convert:${crypto.randomUUID()}`
				})
			});
			convertResult = res;
			cp -= res.cp_in;
			optimisticCp = cp;
			haptic('win');
		} catch (err) {
			error = describeError(err);
			haptic('error');
		} finally {
			converting = false;
		}
	}

	function upgradeCost(base: number, level: number): number {
		return Math.round(base * Math.pow(UPGRADE_GROWTH, level));
	}

	let tapUpgradeCost = $derived(upgradeCost(TAP_UPGRADE_BASE, tapLevel));
	let autoUpgradeCost = $derived(upgradeCost(AUTO_UPGRADE_BASE, autoLevel));

	let tierQuotes = $derived(computeTierQuotes(market, cp));
	let liveHryvniaOut = $derived(findQuote(market, convertAmount));
	let priceRange = $derived(market ? historyRange(market.history) : null);
	let ratioLabel = $derived(anchorRatioLabel(market ? Number(market.price) : null));

	function priceHistoryPoints(history: MarketState['history']): string {
		if (history.length < 2) return '';
		const prices = history.map((h) => Number(h.price));
		const min = Math.min(...prices);
		const max = Math.max(...prices);
		const span = max - min || 1;
		return prices
			.map((p, i) => {
				const x = (i / (prices.length - 1)) * 100;
				const y = 32 - ((p - min) / span) * 32;
				return `${x.toFixed(1)},${y.toFixed(1)}`;
			})
			.join(' ');
	}

	let flushIntervalId: ReturnType<typeof setInterval> | undefined;
	let quoteTimeoutId: ReturnType<typeof setTimeout> | undefined;

	// Живая котировка (T-04.2-квота-превью): при изменении суммы/баланса CP
	// перезапрашивает /farm/market с нужными amounts (тиры + введённая
	// сумма), debounced — не долбит бэкенд на каждый символ ввода.
	$effect(() => {
		const typed = Math.floor(convertAmount);
		const amounts = [...new Set([...tierAmounts(cp), ...(typed > 0 ? [typed] : [])])];
		clearTimeout(quoteTimeoutId);
		if (amounts.length === 0) return;
		quoteTimeoutId = setTimeout(() => loadMarket(amounts), QUOTE_DEBOUNCE_MS);
	});

	onMount(() => {
		loadFarm();
		flushIntervalId = setInterval(flushTaps, FLUSH_INTERVAL_MS);
	});

	onDestroy(() => {
		if (flushIntervalId) clearInterval(flushIntervalId);
		clearTimeout(quoteTimeoutId);
		flushTaps();
	});
</script>

{#if loading}
	<div class="screen-loading"><span>загрузка фермы…</span></div>
{:else}
	<div class="farm-screen">
		<div class="menu-head">
			<h1 class="menu-title">Ферма</h1>
			<div class="menu-sub">тапай, копи CP, качай апгрейды</div>
		</div>

		{#if error}
			<div class="cf-error">{error}</div>
		{/if}

		<div class="farm-cp-card">
			<div class="farm-cp-label">CP</div>
			<div class="farm-cp-val">{optimisticCp}</div>
			<div class="farm-cp-rate">+{cpPerSec.toFixed(2)} CP/сек (авто + коллекция)</div>
		</div>

		<button type="button" class="farm-tap-btn" onclick={tapOnce}>
			<span class="farm-tap-label">ТАП</span>
			<span class="farm-tap-sub">+{tapLevel} CP за тап</span>
		</button>

		<div class="farm-upgrades">
			<button
				type="button"
				class="feature-card farm-upgrade-row"
				disabled={upgradingTap || cp < tapUpgradeCost}
				onclick={() => upgrade('tap')}
			>
				<span class="fc-title">Апгрейд тапа (ур. {tapLevel})</span>
				<span class="fc-desc">{upgradingTap ? 'качаем…' : `стоимость ${tapUpgradeCost} CP`}</span>
			</button>
			<button
				type="button"
				class="feature-card farm-upgrade-row"
				disabled={upgradingAuto || cp < autoUpgradeCost}
				onclick={() => upgrade('auto')}
			>
				<span class="fc-title">Автокликер (ур. {autoLevel})</span>
				<span class="fc-desc">{upgradingAuto ? 'качаем…' : `стоимость ${autoUpgradeCost} CP`}</span>
			</button>
		</div>

		<div class="farm-convert feature-card">
			<span class="fc-title">Обмен CP → ¥</span>
			<span class="fc-desc">
				спот-курс {market ? Number(market.price).toFixed(1) : '…'} CP за 1¥ · анкер {ANCHOR_CP_PER_HRYVNA}
				{#if ratioLabel}
					· {ratioLabel}
				{/if}
			</span>

			{#if market && market.history.length > 1}
				<svg viewBox="0 0 100 32" class="farm-market-chart" preserveAspectRatio="none" aria-hidden="true">
					<polyline
						points={priceHistoryPoints(market.history)}
						fill="none"
						stroke="var(--accent-cyan)"
						stroke-width="1.5"
					/>
				</svg>
				{#if priceRange}
					<div class="farm-chart-range">
						<span>мин {priceRange.min}</span>
						<span>сейчас {Number(market.price).toFixed(1)}</span>
						<span>макс {priceRange.max}</span>
					</div>
				{/if}
			{/if}

			<div class="farm-convert-row">
				<input
					class="farm-convert-input"
					type="number"
					min="1"
					max={cp}
					step="1"
					disabled={converting}
					bind:value={convertAmount}
				/>
				<button
					type="button"
					class="chip"
					disabled={converting || cp <= 0}
					onclick={() => (convertAmount = Math.min(1000, cp))}
				>
					1k
				</button>
				<button
					type="button"
					class="chip"
					disabled={converting || cp <= 0}
					onclick={() => (convertAmount = Math.min(10000, cp))}
				>
					10k
				</button>
				<button
					type="button"
					class="chip chip-all"
					disabled={converting || cp <= 0}
					onclick={() => (convertAmount = cp)}
				>
					max
				</button>
			</div>

			<div class="farm-convert-preview">
				{#if convertAmount <= 0}
					<span class="fc-desc">введи сумму</span>
				{:else if convertAmount > cp}
					<span class="farm-preview-err">недостаточно CP (есть {cp})</span>
				{:else if liveHryvniaOut !== null}
					<span class="farm-preview-val">получишь ≈ {liveHryvniaOut}¥</span>
				{:else}
					<span class="fc-desc">считаем…</span>
				{/if}
			</div>

			<button
				type="button"
				class="chip chip-all farm-convert-btn"
				disabled={converting || convertAmount <= 0 || convertAmount > cp}
				onclick={convert}
			>
				{converting ? 'обмениваем…' : 'обменять'}
			</button>

			{#if convertResult}
				<div class="farm-convert-result">получено {convertResult.hryvnia_out}¥</div>
			{/if}

			{#if tierQuotes.length > 0}
				<div class="farm-impact">
					<span class="fc-desc">чем больше продаёшь за раз — тем хуже курс (price impact):</span>
					<svg viewBox="0 0 100 32" class="farm-impact-chart" preserveAspectRatio="none" aria-hidden="true">
						<polyline
							points={impactCurvePoints(tierQuotes)}
							fill="none"
							stroke="var(--accent-pink)"
							stroke-width="1.5"
						/>
					</svg>
					<div class="farm-impact-table">
						{#each tierQuotes as tq (tq.amount)}
							<div class="farm-impact-row">
								<span class="fir-amount">{formatCp(tq.amount)} CP</span>
								<span class="fir-out">{tq.hryvniaOut}¥</span>
								<span class="fir-impact">{tq.impactPct !== null ? `${tq.impactPct.toFixed(1)}%` : '—'}</span>
							</div>
						{/each}
					</div>
				</div>
			{/if}
		</div>
	</div>
{/if}

<style>
	.farm-screen {
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

	.cf-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.farm-cp-card {
		background: var(--bg-secondary-2);
		border: 1px solid var(--border-secondary);
		border-radius: 14px;
		padding: var(--space-lg);
		text-align: center;
	}
	.farm-cp-label {
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
	}
	.farm-cp-val {
		font-family: var(--font-numeric);
		font-size: var(--font-hero-size);
		font-weight: 900;
		color: var(--accent-cyan);
		line-height: 1.1;
	}
	.farm-cp-rate {
		font-size: 12px;
		color: var(--text-muted);
		font-family: var(--font-body);
		margin-top: var(--space-xs);
	}

	.farm-tap-btn {
		background: var(--accent-pink);
		border: none;
		border-radius: 50%;
		width: 180px;
		height: 180px;
		margin: 0 auto;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: var(--space-xs);
		cursor: pointer;
		box-shadow: 4px 4px 0 #111;
		transition: transform 0.06s;
	}
	.farm-tap-btn:active {
		transform: translate(2px, 2px) scale(0.97);
		box-shadow: 2px 2px 0 #111;
	}
	.farm-tap-label {
		font-family: var(--font-shout);
		font-size: 28px;
		color: #1a0f12;
		letter-spacing: 0.04em;
	}
	.farm-tap-sub {
		font-size: 12px;
		color: #3a1420;
		font-family: var(--font-body);
	}

	.farm-upgrades {
		display: flex;
		flex-direction: column;
		gap: var(--space-sm);
	}
	.farm-upgrade-row {
		min-height: auto;
	}

	.farm-convert-row {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-sm);
		align-items: center;
	}
	.farm-convert-input {
		flex: 1 1 100%;
		background: var(--bg-dominant);
		border: 2px solid var(--border-secondary);
		border-radius: 8px;
		padding: var(--space-sm);
		color: var(--text-primary);
		font-family: var(--font-numeric);
		font-size: var(--font-heading-size);
	}
	.farm-convert-row .chip {
		flex: 1;
	}
	.farm-convert-preview {
		min-height: 20px;
		display: flex;
		align-items: center;
	}
	.farm-preview-val {
		font-family: var(--font-numeric);
		font-size: var(--font-heading-size);
		color: var(--accent-cyan);
		font-weight: 900;
	}
	.farm-preview-err {
		font-size: var(--font-body-size);
		color: var(--destructive-text);
		font-family: var(--font-body);
	}
	.farm-convert-btn {
		width: 100%;
	}
	.farm-convert-result {
		font-family: var(--font-numeric);
		font-size: var(--font-heading-size);
		color: var(--accent-pink);
		font-weight: 900;
	}

	.farm-market-chart,
	.farm-impact-chart {
		width: 100%;
		height: 32px;
		margin-top: var(--space-xs);
	}
	.farm-chart-range {
		display: flex;
		justify-content: space-between;
		font-size: 11px;
		color: var(--text-muted);
		font-family: var(--font-body);
	}

	.farm-impact {
		border-top: 1px solid var(--border-secondary);
		padding-top: var(--space-sm);
		margin-top: var(--space-xs);
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
	}
	.farm-impact-table {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.farm-impact-row {
		display: grid;
		grid-template-columns: 1fr 1fr auto;
		gap: var(--space-sm);
		font-family: var(--font-numeric);
		font-size: 12px;
		padding: 4px 0;
	}
	.fir-amount {
		color: var(--text-secondary);
	}
	.fir-out {
		color: var(--accent-cyan);
		text-align: right;
	}
	.fir-impact {
		color: var(--destructive-text);
		text-align: right;
	}
</style>
