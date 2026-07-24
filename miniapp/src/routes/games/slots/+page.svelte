<script lang="ts">
	// Slot ("Azumanga") — server-side port of webapp/slot-machine.jsx +
	// slot-reel.jsx (04.2-10, polished per user request; 04.2-11 added the
	// mid-bonus retrigger reveal below). Server is the SOLE source of truth
	// for the grid/wins/freespins/retriggers (D-03/T-04.1-01, slot_engine.py)
	// — this screen only animates a cosmetic reel-drum spin (random filler
	// symbols scrolling, landing on whatever POST /games/slots actually
	// returned), a win/lose color-grade flash, and — new — a cosmetic
	// step-by-step REPLAY of the bonus round the server already fully
	// resolved in one shot (scatter glow -> freespin badge -> one toast per
	// `outcome.retrigger_awards` entry). No client-side RNG affects payout,
	// no client-side win computation (SLOT_SYMBOLS/SLOT_PAYLINES in
	// lib/slotData.ts are rendering metadata only — name/tint/cell-
	// highlighting, filler symbols during the spin are cosmetic noise, never
	// probability/payout). The replay pacing is informational only — it does
	// not invent outcomes, it only staggers the reveal of numbers the server
	// already sent in `outcome`.
	import { onDestroy } from 'svelte';
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';
	import { SLOT_SYMBOLS, SLOT_PAYLINES, symbolSrc } from '$lib/slotData';

	const BET_CHIPS = [10, 50, 100, 500, 1000]; // все кратны TOTAL_LINES=10 (slot_engine.py)
	// Скорость барабана (04.2-11: заметно быстрее прежних 700/140/1340мс —
	// "быстрый и хлёсткий" базовый спин).
	const SPIN_BASE_MS = 420;
	const SPIN_PER_COL_MS = 90;
	const REVEAL_DELAY_MS = SPIN_BASE_MS + SPIN_PER_COL_MS * 4 + 60;

	// Visual drum: FILLER_ROWS of cosmetic random symbols scroll past before
	// the strip settles on the real final 3 rows for that column. Row count
	// is fixed so the CSS translateY(%) landing position never changes.
	const FILLER_ROWS = 9;
	const STRIP_ROWS = FILLER_ROWS + 3;

	// Пэйсинг реплея бонуса (04.2-11) — сколько держится scatter-пульс до
	// объявления счётчика фриспинов, и тайминг тост-реплея ретриггеров.
	// Чисто информационная задержка (не "motion"), поэтому НЕ гейтится
	// prefers-reduced-motion — сама анимация (пульс/тост) гейтится в CSS.
	const SCATTER_GLOW_MS = 650;
	const RETRIGGER_TOAST_GAP_MS = 350;
	const RETRIGGER_TOAST_VISIBLE_MS = 900;
	const BONUS_SETTLE_MS = 350;

	// Автоспин: серия одинаковых по ставке раундов без ручного тапа на каждый.
	// Тиры — фиксированный набор (не "тап циклит следующий уровень" — ряд из
	// 5 чипов сразу виден игроку целиком, см. .auto-picker ниже). 'inf' крутит
	// до явного "Стоп"/ошибки/недостатка средств.
	const AUTO_SPIN_TIERS: (number | 'inf')[] = [5, 15, 25, 50, 'inf'];
	// Доп. пауза ПОВЕРХ времени реального реплея раунда (REVEAL_DELAY_MS +
	// возможный бонус-реплей уже отрабатывают внутри spin() до её resolve) —
	// петля никогда не крутит быстрее, чем реально идёт анимация барабана.
	const AUTO_SPIN_PAUSE_MS = 400;

	type SlotWin = { line_index: number; symbol: string; count: number; payout: number };
	type SlotResult = {
		game: string;
		bet: number;
		payout: number;
		outcome: {
			grid: string[][];
			wins: SlotWin[];
			freespins: number;
			scatter: number;
			retrigger_awards: number[];
		};
	};

	// Символ-scatter выводится из той же метаданной, что рисует ячейки
	// (SLOT_SYMBOLS[id].role) — не дублируем ID отдельной строкой.
	const SCATTER_SYMBOL_ID = Object.keys(SLOT_SYMBOLS).find(
		(id) => SLOT_SYMBOLS[id].role === 'scatter'
	)!;

	let bet = $state(BET_CHIPS[0]);
	let spinning = $state(false);
	let grid = $state<string[][]>(_placeholderGrid());
	let reelStrips = $state<string[][]>(_stripsFromGrid(grid));
	let wins = $state<SlotWin[]>([]);
	let freespins = $state(0);
	let scatterCount = $state(0);
	let lastPayout = $state<number | null>(null);
	let activeWinIdx = $state(0);
	let error = $state<string | null>(null);
	let outcomeTint = $state<'win' | 'lose' | null>(null);

	// 04.2-11: scatter-глоу (до объявления счётчика) + реплей бонусного
	// раунда (растущий счётчик + тост на каждый ретриггер).
	let scatterGlow = $state(false);
	let bonusActive = $state(false);
	let bonusFreespinsShown = $state(0);
	let bonusToast = $state<string | null>(null);

	// Автоспин: null = не запущен. tier — выбранный тир (число или 'inf'),
	// remaining — сколько раундов ещё осталось (не используется для 'inf').
	let autoSpin = $state<{ tier: number | 'inf'; remaining: number } | null>(null);
	let autoPickerOpen = $state(false);

	function _placeholderGrid(): string[][] {
		const ids = Object.keys(SLOT_SYMBOLS);
		return [0, 1, 2].map((r) => [0, 1, 2, 3, 4].map((c) => ids[(r * 5 + c) % ids.length]));
	}

	function _randomSymbolId(): string {
		const ids = Object.keys(SLOT_SYMBOLS);
		return ids[Math.floor(Math.random() * ids.length)];
	}

	// Builds one column's scroll strip: FILLER_ROWS cosmetic random symbols
	// followed by the real 3 final symbols (top-to-bottom) for that column.
	function _buildStrip(finalCol: string[]): string[] {
		const filler = Array.from({ length: FILLER_ROWS }, _randomSymbolId);
		return [...filler, ...finalCol];
	}

	function _stripsFromGrid(g: string[][]): string[][] {
		return [0, 1, 2, 3, 4].map((col) => _buildStrip([g[0][col], g[1][col], g[2][col]]));
	}

	const highlightCells = $derived.by(() => {
		if (spinning || wins.length === 0) return new Set<string>();
		const win = wins[activeWinIdx];
		if (!win) return new Set<string>();
		const line = SLOT_PAYLINES[win.line_index];
		if (!line) return new Set<string>();
		return new Set(line.map((row, col) => `${row}:${col}`));
	});

	// (c) Клетки со scatter на приземлившейся сетке — подсвечиваются глоу-
	// пульсом СРАЗУ при посадке барабанов, пока scatterGlow==true (см.
	// _revealScatterAndBonus), т.е. ДО того как объявлен счётчик фриспинов.
	const scatterCellKeys = $derived.by(() => {
		if (!scatterGlow) return new Set<string>();
		const keys = new Set<string>();
		for (let row = 0; row < 3; row++) {
			for (let col = 0; col < 5; col++) {
				if (grid[row][col] === SCATTER_SYMBOL_ID) keys.add(`${row}:${col}`);
			}
		}
		return keys;
	});

	// Cycle through multiple line-wins every 1.3s (mirrors webapp/slot-machine.jsx).
	$effect(() => {
		if (spinning || wins.length <= 1) {
			activeWinIdx = 0;
			return;
		}
		const t = setInterval(() => {
			activeWinIdx = (activeWinIdx + 1) % wins.length;
		}, 1300);
		return () => clearInterval(t);
	});

	function _wait(ms: number): Promise<void> {
		return new Promise((resolve) => window.setTimeout(resolve, ms));
	}

	// (c)+(d) Реплей бонусного раунда ПОСЛЕ того, как барабаны уже
	// приземлились на стартовую сетку. Сервер уже всё решил одним расчётом
	// (slot_engine.evaluate_grid) — здесь только пэйсинг показа готовых
	// чисел: scatter-глоу -> счётчик фриспинов -> по тосту на каждый
	// ретриггер из `outcome.retrigger_awards`, пока счётчик не дойдёт до
	// итогового `outcome.freespins` (это ИТОГО сыграно, включая ретриггеры —
	// см. докстринг SlotResult.freespins в slot_engine.py).
	async function _revealScatterAndBonus(res: SlotResult): Promise<void> {
		const scatterN = res.outcome.scatter;
		const totalFreespins = res.outcome.freespins;

		if (scatterN < 3) {
			scatterCount = scatterN;
			freespins = totalFreespins;
			return;
		}

		// scatterCount выставляется СРАЗУ (не после паузы) — тикер ниже уже
		// может честно показать "СКАТТЕР ×N!" во время глоу-паузы, но именно
		// БЕЗ числа фриспинов (freespins/bonusFreespinsShown ещё не тронуты
		// на этом шаге) — это и есть "глоу до объявления счётчика" из (c).
		scatterCount = scatterN;
		scatterGlow = true;
		haptic('scatter');
		await _wait(SCATTER_GLOW_MS);
		scatterGlow = false;

		const retriggerAwards = res.outcome.retrigger_awards ?? [];
		const initialAward = totalFreespins - retriggerAwards.reduce((sum, n) => sum + n, 0);
		bonusActive = true;
		bonusFreespinsShown = initialAward;
		freespins = initialAward;

		for (const award of retriggerAwards) {
			await _wait(RETRIGGER_TOAST_GAP_MS);
			bonusFreespinsShown += award;
			freespins = bonusFreespinsShown;
			bonusToast = `+${award} ФРИСПИНОВ — РЕТРИГГЕР!`;
			haptic('retrigger');
			await _wait(RETRIGGER_TOAST_VISIBLE_MS);
			bonusToast = null;
		}

		await _wait(BONUS_SETTLE_MS);
		bonusActive = false;
	}

	// Возвращает true, если раунд успешно settle'ился (и полностью доиграл
	// свой визуальный реплей — включая бонус-раунд, если он был), false —
	// если API вернул ошибку (недостаточно средств/лимит ставки/троттлинг
	// авто-спина/сеть). Manual-тап по-прежнему просто игнорирует результат
	// (`onclick={spin}`); автоспин использует его как сигнал "раунд готов,
	// можно планировать следующий" (см. runAutoSpin ниже) — раньше это
	// определялось fire-and-forget `window.setTimeout`, теперь тот же тайминг
	// просто awaited, ничего в самой анимации/пэйсинге не поменялось.
	async function spin(): Promise<boolean> {
		if (spinning || bonusActive) return false;
		error = null;
		wins = [];
		lastPayout = null;
		scatterCount = 0;
		freespins = 0;
		scatterGlow = false;
		bonusToast = null;
		outcomeTint = null;
		spinning = true;
		haptic('spin');
		// Kick the drum off immediately with cosmetic filler so the player
		// sees motion the instant they tap — the strip's tail (last 3 rows)
		// still shows the previous grid until the real result lands below.
		reelStrips = _stripsFromGrid(grid);

		let res: SlotResult;
		try {
			res = await apiFetch<SlotResult>('/api/v1/games/slots', {
				method: 'POST',
				body: JSON.stringify({ bet, idem_key: `slots:${crypto.randomUUID()}` })
			});
		} catch (err) {
			spinning = false;
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
			haptic('error');
			return false;
		}

		// Real result is known — swap the strip tails to the true final
		// symbols. The CSS translateY animation keeps running uninterrupted
		// (row count/position never changed), so this never causes a jump.
		reelStrips = _stripsFromGrid(res.outcome.grid);

		await _wait(REVEAL_DELAY_MS);

		grid = res.outcome.grid;
		wins = res.outcome.wins;
		lastPayout = res.payout;
		spinning = false;
		outcomeTint = res.payout > 0 ? 'win' : 'lose';
		haptic('reel-stop');

		// Scatter haptic/reveal is handled inside _revealScatterAndBonus
		// (fires at the glow moment, not here) so it isn't duplicated.
		if (res.outcome.scatter < 3) {
			if (res.payout >= bet * 20) {
				haptic('big-win');
			} else if (res.payout > 0) {
				haptic('win');
			} else {
				haptic('lose');
			}
		}

		await _revealScatterAndBonus(res);
		return true;
	}

	function autoSpinLabel(tier: number | 'inf'): string {
		return tier === 'inf' ? '∞' : String(tier);
	}

	function autoSpinStatusText(a: { tier: number | 'inf'; remaining: number }): string {
		if (a.tier === 'inf') return 'авто ∞ · жми, чтобы остановить';
		const done = a.tier - a.remaining + 1;
		return `авто ${done}/${a.tier} · жми, чтобы остановить`;
	}

	// Цикл авто-спина: последовательные вызовы spin() (никогда параллельно),
	// каждый следующий раунд стартует не раньше, чем предыдущий полностью
	// доиграл свой визуальный реплей (await spin()) + небольшая доп. пауза.
	// Останавливается сама на первой же ошибке (недостаточно средств/лимит
	// ставки/троттлинг/сеть) — БЕЗ ретрая, как и обычный ручной спин (см.
	// докстринг spin() выше); внешняя остановка (Стоп/скрытие
	// вкладки/размонтирование) просто обнуляет `autoSpin`, и цикл выходит на
	// следующей проверке условия while.
	async function runAutoSpin(): Promise<void> {
		while (autoSpin) {
			const ok = await spin();
			if (!autoSpin) break;
			if (!ok) {
				autoSpin = null;
				break;
			}
			if (autoSpin.tier === 'inf') {
				await _wait(AUTO_SPIN_PAUSE_MS);
				continue;
			}
			const remaining = autoSpin.remaining - 1;
			if (remaining <= 0) {
				autoSpin = null;
				break;
			}
			autoSpin = { ...autoSpin, remaining };
			await _wait(AUTO_SPIN_PAUSE_MS);
		}
	}

	async function startAutoSpin(tier: number | 'inf'): Promise<void> {
		if (spinning || bonusActive || autoSpin) return;
		autoPickerOpen = false;
		haptic('tap');
		autoSpin = { tier, remaining: tier === 'inf' ? Infinity : tier };
		await runAutoSpin();
	}

	function stopAutoSpin(): void {
		autoSpin = null;
	}

	// Авто-спин не должен продолжать дёргать API в фоне, если игрок ушёл со
	// страницы или свернул Mini App — обнуление `autoSpin` останавливает
	// цикл на следующей проверке `while (autoSpin)` (текущий в моменте раунд
	// доигрывается до конца, следующий уже не стартует).
	onDestroy(() => {
		autoSpin = null;
	});

	$effect(() => {
		function onVisibilityChange() {
			if (document.hidden) autoSpin = null;
		}
		document.addEventListener('visibilitychange', onVisibilityChange);
		return () => document.removeEventListener('visibilitychange', onVisibilityChange);
	});
</script>

<div class="slot-screen">
	<div class="menu-head">
		<h1 class="menu-title">Слот</h1>
		<div class="menu-sub">3×5 · 10 линий · вайлд/скаттер/фриспины</div>
	</div>

	{#if bonusActive}
		<!-- (d) Живой бэдж бонуса: счётчик растёт по ходу реплея ретриггеров
		     (см. _revealScatterAndBonus). {#key} пересоздаёт узел на каждое
		     изменение числа, чтобы CSS-анимация "бампа" реально переигрывала. -->
		<div class="slot-bonus-badge">
			<span class="slot-bonus-badge-label">✦ БОНУС</span>
			{#key bonusFreespinsShown}
				<span class="slot-bonus-badge-count">{bonusFreespinsShown}</span>
			{/key}
			<span class="slot-bonus-badge-sub">фриспинов</span>
		</div>
	{:else if freespins > 0}
		<div class="slot-freespins-pill">✦ {freespins} фриспинов доиграно автоматически</div>
	{/if}

	{#if bonusToast}
		<div class="slot-bonus-toast">{bonusToast}</div>
	{/if}

	<div
		class="slot-reels {spinning ? 'slot-spinning' : ''} {outcomeTint === 'win'
			? 'slot-reels-win'
			: ''} {outcomeTint === 'lose' ? 'slot-reels-lose' : ''}"
	>
		{#each [0, 1, 2, 3, 4] as col (col)}
			{#if spinning}
				<div
					class="slot-col-viewport"
					style={`--col-delay: ${col * SPIN_PER_COL_MS}ms; --spin-duration: ${SPIN_BASE_MS}ms`}
				>
					<div class="slot-reel-strip">
						{#each reelStrips[col] as symId, i (i)}
							{@const sym = SLOT_SYMBOLS[symId]}
							<div class="slot-cell slot-cell-strip" style={`--tint: ${sym?.tint ?? '#333'}`}>
								<img src={symbolSrc(symId)} alt="" draggable="false" />
							</div>
						{/each}
					</div>
				</div>
			{:else}
				<div class="slot-col">
					{#each [0, 1, 2] as row (row)}
						{@const symId = grid[row][col]}
						{@const sym = SLOT_SYMBOLS[symId]}
						{@const hit = highlightCells.has(`${row}:${col}`)}
						{@const scatterHit = scatterCellKeys.has(`${row}:${col}`)}
						<div
							class="slot-cell {hit ? 'slot-cell-hit' : ''} {scatterHit
								? 'slot-cell-scatter-glow'
								: ''}"
							style={`--tint: ${sym?.tint ?? '#333'}`}
						>
							<img src={symbolSrc(symId)} alt={sym?.name ?? symId} draggable="false" />
							{#if sym?.role === 'wild'}<span class="slot-badge slot-badge-wild">WILD</span>{/if}
							{#if sym?.role === 'scatter'}<span class="slot-badge slot-badge-scatter"
									>SCATTER</span
								>{/if}
						</div>
					{/each}
				</div>
			{/if}
		{/each}
	</div>

	<div class="slot-ticker">
		{#if spinning}
			<span class="slot-ticker-spin">крутимся…</span>
		{:else if scatterGlow}
			<!-- (c) Scatter уже виден (глоу на клетках), но число фриспинов ещё
			     сознательно не объявлено — см. _revealScatterAndBonus. -->
			<span class="slot-ticker-scatter">СКАТТЕР ×{scatterCount}!</span>
		{:else if bonusActive}
			<span class="slot-ticker-scatter">СКАТТЕР ×{scatterCount}! бонус в разгаре…</span>
		{:else if scatterCount >= 3}
			<span class="slot-ticker-scatter"
				>СКАТТЕР ×{scatterCount}! → {freespins} фриспинов отыграно</span
			>
		{:else if wins.length > 0}
			{@const w = wins[activeWinIdx]}
			<span class="slot-ticker-win">
				{SLOT_SYMBOLS[w.symbol]?.name ?? w.symbol} ×{w.count} · линия {w.line_index + 1} · +{w.payout}¥
			</span>
		{:else if lastPayout !== null}
			<span class="slot-ticker-lose">не в этот раз — крути ещё</span>
		{:else}
			<span class="slot-ticker-idle">жми · крути барабаны</span>
		{/if}
	</div>

	{#if lastPayout !== null && !spinning}
		<div class={`slot-result ${lastPayout > 0 ? 'slot-win' : 'slot-lose'}`}>
			{lastPayout > 0 ? `+${lastPayout}¥` : `−${bet}¥`}
		</div>
	{/if}

	{#if error}
		<div class="slot-error">{error}</div>
	{/if}

	<div class="bet-row">
		<div class="bet-display">
			<span class="bet-label">ставка</span>
			<div class="bet-amount">{bet}<small>¥</small></div>
		</div>
		<div class="bet-chips">
			{#each BET_CHIPS as v (v)}
				<button
					type="button"
					class={`chip ${bet === v ? 'chip-on' : ''}`}
					disabled={spinning || bonusActive || !!autoSpin}
					onclick={() => (bet = v)}
				>
					{v}
				</button>
			{/each}
		</div>
	</div>

	{#if autoSpin}
		<button type="button" class="slot-cta slot-cta-auto" onclick={stopAutoSpin}>
			<span class="slot-cta-label">СТОП</span>
			<span class="slot-cta-sub">{autoSpinStatusText(autoSpin)}</span>
		</button>
	{:else}
		{#if autoPickerOpen}
			<div class="auto-picker">
				{#each AUTO_SPIN_TIERS as tier (tier)}
					<button type="button" class="auto-chip" onclick={() => startAutoSpin(tier)}>
						{autoSpinLabel(tier)}
					</button>
				{/each}
			</div>
		{/if}
		<div class="cta-row">
			<button type="button" class="slot-cta" disabled={spinning || bonusActive} onclick={spin}>
				<span class="slot-cta-label"
					>{spinning ? 'крутим…' : bonusActive ? 'БОНУС…' : 'КРУТИТЬ'}</span
				>
				<span class="slot-cta-sub">{spinning || bonusActive ? '' : `ставка ${bet}¥`}</span>
			</button>
			<button
				type="button"
				class={`auto-toggle ${autoPickerOpen ? 'auto-toggle-on' : ''}`}
				disabled={spinning || bonusActive}
				aria-expanded={autoPickerOpen}
				onclick={() => (autoPickerOpen = !autoPickerOpen)}
			>
				АВТО
			</button>
		</div>
	{/if}
</div>

<style>
	.slot-screen {
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

	.slot-freespins-pill {
		align-self: flex-start;
		background: var(--accent-yellow);
		color: #1a0f12;
		border-radius: 999px;
		padding: 4px 12px;
		font-size: 12px;
		font-weight: 700;
		font-family: var(--font-body);
	}

	/* 04.2-11: живой бэдж бонуса — тот же жёлтый акцент, что и у финального
	   .slot-freespins-pill выше (эта же смысловая роль "бонус/фриспины"), но
	   крупнее и со sticker-обводкой/тенью (общий приём кнопок этого экрана,
	   см. .slot-cta ниже) — читается как "активный счётчик", а не пассивная
	   плашка-факт. */
	.slot-bonus-badge {
		align-self: flex-start;
		display: flex;
		align-items: baseline;
		gap: 6px;
		background: var(--accent-yellow);
		color: #1a0f12;
		border: 2px solid #111;
		border-radius: 10px;
		padding: 5px 12px;
		box-shadow: 3px 3px 0 #111;
		font-family: var(--font-body);
	}
	.slot-bonus-badge-label {
		font-size: 11px;
		font-weight: 900;
		letter-spacing: 0.06em;
	}
	.slot-bonus-badge-count {
		display: inline-block;
		font-family: var(--font-numeric);
		font-size: 22px;
		font-weight: 900;
		line-height: 1;
		animation: slotBonusCountBump 0.3s ease-out;
	}
	.slot-bonus-badge-sub {
		font-size: 11px;
		opacity: 0.8;
	}
	@keyframes slotBonusCountBump {
		0% {
			transform: scale(1.5);
		}
		100% {
			transform: scale(1);
		}
	}

	/* (d) Тост на каждый ретриггер (см. _revealScatterAndBonus) — тот же
	   sticker-приём (жёсткая тень/2px обводка), что у .slot-bonus-badge, но
	   в тёмной инверсии, чтобы читаться как "уведомление поверх", не как
	   часть основного счётчика. Полностью пересоздаётся Svelte-ом на каждый
	   новый текст ({#if bonusToast}), поэтому анимация ниже переигрывает
	   сама, без {#key}. */
	.slot-bonus-toast {
		align-self: center;
		background: #1a0f12;
		color: var(--accent-yellow);
		border: 2px solid var(--accent-yellow);
		border-radius: 10px;
		padding: 8px 16px;
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		letter-spacing: 0.04em;
		text-align: center;
		box-shadow: 3px 3px 0 #111;
		animation: slotToastPop 0.25s ease-out;
	}
	@keyframes slotToastPop {
		0% {
			transform: translateY(-6px) scale(0.9);
			opacity: 0;
		}
		100% {
			transform: translateY(0) scale(1);
			opacity: 1;
		}
	}

	.slot-reels {
		display: flex;
		gap: 6px;
		padding: 12px;
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 14px;
		/* Pitfall 4: keep animated reel content padded off the screen edge so
		   it never collides with Telegram's own edge-swipe-to-close gesture. */
		margin: 0 2px;
		overflow: hidden;
		transition:
			border-color 0.25s ease-out,
			box-shadow 0.25s ease-out;
	}
	/* Win/lose color-grade flash on the whole board (cleared on next spin
	   since outcomeTint resets to null in spin()). */
	.slot-reels-win {
		border-color: var(--positive);
		box-shadow:
			0 0 0 2px var(--positive),
			0 0 26px rgba(46, 224, 106, 0.35);
	}
	.slot-reels-lose {
		border-color: var(--destructive);
		box-shadow:
			0 0 0 2px var(--destructive),
			0 0 20px rgba(255, 56, 56, 0.22);
	}

	.slot-col {
		display: flex;
		flex-direction: column;
		gap: 6px;
		flex: 1;
		min-width: 0;
	}
	.slot-cell {
		position: relative;
		aspect-ratio: 1;
		border-radius: 8px;
		overflow: hidden;
		background: color-mix(in srgb, var(--tint) 25%, var(--bg-secondary-2));
		border: 1px solid var(--border-secondary);
	}
	.slot-cell img {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
	}
	.slot-cell-hit {
		border-color: var(--accent-pink);
		box-shadow: 0 0 0 2px var(--accent-pink);
	}
	/* (c) Scatter-глоу: пульс на каждой scatter-ячейке В МОМЕНТ приземления,
	   до того как объявлен счётчик фриспинов (scatterGlow — короткое окно
	   времени, см. _revealScatterAndBonus). Жёлтый — та же роль-акцента
	   "бонус/scatter", что и .slot-bonus-badge/.slot-ticker-scatter ниже, не
	   смешивается с розовым (.slot-cell-hit — обычные выигрышные линии). */
	.slot-cell-scatter-glow {
		border-color: var(--accent-yellow);
		animation: slotScatterPulse 0.42s ease-in-out 2;
	}
	@keyframes slotScatterPulse {
		0%,
		100% {
			box-shadow: 0 0 0 2px var(--accent-yellow);
			transform: scale(1);
		}
		50% {
			box-shadow:
				0 0 0 3px var(--accent-yellow),
				0 0 16px rgba(255, 216, 74, 0.85);
			transform: scale(1.06);
		}
	}
	.slot-badge {
		position: absolute;
		bottom: 2px;
		left: 2px;
		right: 2px;
		font-size: 8px;
		text-align: center;
		font-weight: 900;
		border-radius: 4px;
		padding: 1px 0;
		font-family: var(--font-body);
	}
	.slot-badge-wild {
		background: #ffd84a;
		color: #1a0f12;
	}
	.slot-badge-scatter {
		background: #ff5b8d;
		color: #1a0f12;
	}

	/* Real scrolling reel/drum: a fixed 3-cell-tall viewport (matches the
	   static column's height so nothing jumps on the spin<->idle switch)
	   clips a taller strip of FILLER_ROWS+3 symbols. Each column's strip
	   animates via CSS translateY(%) — percentages are relative to the
	   strip's OWN height, so the math is independent of actual pixel size. */
	.slot-col-viewport {
		position: relative;
		overflow: hidden;
		aspect-ratio: 1 / 3;
		flex: 1;
		min-width: 0;
		border-radius: 8px;
	}
	.slot-reel-strip {
		display: flex;
		flex-direction: column;
		position: absolute;
		inset: 0;
		/* STRIP_ROWS visible rows stacked = STRIP_ROWS/3 × viewport height */
		height: calc(100% * 12 / 3);
		/* Длительность идёт из --spin-duration (JS SPIN_BASE_MS), не задублирована
		   магическим числом — 04.2-11 ускорил базовый спин до 420мс. */
		animation: slotReelSpin var(--spin-duration, 420ms) cubic-bezier(0.16, 0.86, 0.32, 1) both;
		animation-delay: var(--col-delay);
	}
	.slot-cell-strip {
		flex: 0 0 calc(100% / 12);
		aspect-ratio: 1;
		border-radius: 0;
		border: none;
	}
	/* Lands on translateY(-75%): with a 12-row strip that reveals exactly
	   the last 3 rows (the real final symbols) inside the 3-row viewport.
	   Slight overshoot past -75% then eases back — the "mechanical settle"
	   feel of a real slot drum stopping. */
	@keyframes slotReelSpin {
		0% {
			transform: translateY(0);
		}
		72% {
			transform: translateY(-80%);
		}
		100% {
			transform: translateY(-75%);
		}
	}

	.slot-ticker {
		min-height: 22px;
		text-align: center;
		font-family: var(--font-body);
		font-size: var(--font-body-size);
	}
	.slot-ticker-win {
		color: var(--positive-text);
		font-weight: 700;
	}
	.slot-ticker-scatter {
		color: var(--accent-yellow);
		font-weight: 700;
	}
	.slot-ticker-idle,
	.slot-ticker-lose {
		color: var(--text-muted);
	}

	.slot-result {
		align-self: center;
		text-align: center;
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		padding: 2px 16px;
		border-radius: 10px;
	}
	.slot-win {
		color: var(--positive-text);
		background: var(--positive-bg);
	}
	.slot-lose {
		color: var(--destructive-text);
		background: var(--destructive-bg);
	}

	.slot-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.bet-row {
		display: flex;
		align-items: center;
		gap: var(--space-md);
	}
	.bet-display {
		display: flex;
		flex-direction: column;
	}
	.bet-label {
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
	}
	.bet-amount {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		color: var(--text-primary);
	}
	.bet-amount small {
		font-size: 12px;
		color: var(--accent-pink);
		margin-left: 1px;
	}
	.bet-chips {
		display: grid;
		grid-template-columns: repeat(5, 1fr);
		gap: var(--space-xs);
		flex: 1;
	}

	.cta-row {
		display: flex;
		gap: var(--space-xs);
		align-items: stretch;
	}
	.cta-row .slot-cta {
		flex: 1;
	}

	.slot-cta {
		background: var(--accent-pink);
		border: none;
		border-radius: 14px;
		padding: var(--space-md);
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--space-xs);
		cursor: pointer;
		box-shadow: 4px 4px 0 #111;
		transition: transform 0.08s;
	}
	.slot-cta:active:not(:disabled) {
		transform: translate(2px, 2px);
		box-shadow: 2px 2px 0 #111;
	}
	.slot-cta:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.slot-cta-label {
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		color: #1a0f12;
		letter-spacing: 0.04em;
	}
	.slot-cta-sub {
		font-size: 12px;
		color: #3a1420;
		font-family: var(--font-body);
	}

	/* Пока идёт авто-серия, весь CTA превращается в один widescreen "СТОП" —
	   тот же sticker-приём (жёсткая тень/скругление), что у обычного
	   .slot-cta, но на cyan-акценте: своя, ни с чем не смешиваемая роль
	   ("автоматика", отдельно от розового ручного спина и жёлтого бонуса). */
	.slot-cta-auto {
		background: var(--accent-cyan);
	}
	.slot-cta-auto .slot-cta-label,
	.slot-cta-auto .slot-cta-sub {
		color: #0a2a30;
	}

	/* Компактная кнопка-переключатель ряда тиров рядом с основным "КРУТИТЬ" —
	   тот же цвет-роль cyan, что и .slot-cta-auto (обе — "автоматика"). */
	.auto-toggle {
		flex: 0 0 auto;
		width: 64px;
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 14px;
		color: var(--text-muted);
		font-family: var(--font-body);
		font-weight: 700;
		font-size: 11px;
		letter-spacing: 0.04em;
		cursor: pointer;
		transition:
			border-color 0.15s,
			color 0.15s,
			background 0.15s;
	}
	.auto-toggle:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.auto-toggle-on {
		border-color: var(--accent-cyan);
		color: var(--accent-cyan);
		background: color-mix(in srgb, var(--accent-cyan) 14%, var(--bg-secondary-2));
	}

	/* Ряд из всех тиров сразу (вариант B — не "тап циклит следующий уровень",
	   игрок сразу видит весь набор 5/15/25/50/∞ и жмёт нужный). */
	.auto-picker {
		display: grid;
		grid-template-columns: repeat(5, 1fr);
		gap: var(--space-xs);
	}
	.auto-chip {
		background: var(--bg-secondary-2);
		border: 2px solid var(--accent-cyan);
		border-radius: 10px;
		padding: 8px 4px;
		color: var(--accent-cyan);
		font-family: var(--font-numeric);
		font-weight: 900;
		font-size: 14px;
		cursor: pointer;
		transition: transform 0.08s;
	}
	.auto-chip:active {
		transform: translate(1px, 1px);
	}

	/* Respect prefers-reduced-motion: kill translateY drum spin, the scatter
	   pulse and the toast pop-in — all pure "motion" pieces. Timing/pacing
	   (setTimeout delays in the script) is left alone, that's informational
	   sequencing, not motion, and reduced-motion doesn't require collapsing
	   it. The reel strip snaps straight to its landed position (-75%) so
	   there's no jump once the real grid swaps in. */
	@media (prefers-reduced-motion: reduce) {
		.slot-reel-strip {
			animation: none;
			transform: translateY(-75%);
		}
		.slot-cell-scatter-glow {
			animation: none;
		}
		.slot-bonus-badge-count {
			animation: none;
		}
		.slot-bonus-toast {
			animation: none;
		}
	}
</style>
