<script lang="ts">
	// «Тето Брейнрот: Дрель-Хант» — игровой экран 6×6 мегаблок-слота.
	// Сервер — единственный источник истины (D-03): экран только проигрывает
	// покадровый сценарий `animation.ops` из POST /api/v1/games/teto_slots
	// (контракт: докстринг api/routes/games.py «КОНТРАКТ ФРОНТА» + докстринг
	// teto_slot_engine.serialize_animation + docs/teto-slot-design-direction.md).
	// Клиент не рандомит и не считает деньги НИЧЕГО: доски, выигравшие блоки,
	// линии, лестница — всё приходит готовым в op'ах; TETO_SYMBOLS — чисто
	// рендер-метаданные (имя/тинт/спрайт), как slotData.ts у Азуманги.
	//
	// ДЕНЬГИ (жёсткий контракт, §5.1 дизайн-направления): единственное
	// авторитетное число — `payout` (== animation.payout_paid). Бегущий счётчик
	// после раунда k = min(prefix_sum(final_round_payout 0..k), payout_paid) —
	// min ВСЕГДА, тогда код один и для обычного, и для капнутого банком спина.
	// Грейдинг («большой выигрыш», хаптик, размер цифр) — ТОЛЬКО от payout;
	// payout_engine_total — исключительно для честной подписи
	// «выплачено X из Y — банк чата пуст».
	//
	// Реплеер — последовательный async-цикл по ops c отменяемыми паузами:
	// тап по доске / кнопка «пропустить» / prefers-reduced-motion ведут в один
	// и тот же settle()-путь (финальная доска из outcome.final_blocks, деньги
	// из payout). animation:null (replay идемпотентного idem_key) — нормальный
	// 200: без реплеера вовсе, тикер говорит «повтор раунда».
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';
	import { TETO_SYMBOLS, TETO_PAYTABLE_ORDER, tetoSymbolSrc } from '$lib/tetoSlotData';
	import BetControl from '$lib/components/BetControl.svelte';

	// TOTAL_LINES движка (teto_slot_engine.py) — ставка кратна 50.
	const BET_STEP = 50;
	const BET_CHIPS = [50, 250, 500, 1000, 2500];
	const AUTO_SPIN_PRESETS = [5, 10, 25, 50];
	const GRID = 6;

	// Пэйсинг реплеера: типичный спин с 1-3 каскадами обязан укладываться в
	// ~4 c (дизайн-направление §2 — «играют подряд десятки спинов»). Один
	// каскад: fill 450 + evaluate 420 + shatter 250 + tumble 320 + round_end
	// 380 ≈ 1.8 c; каждый доп. каскад ≈ +1 c. Дрель-Хант чуть длиннее
	// (180+480) — это «денежный момент», ему положено читаться.
	const FILL_MS = 450;
	const EVAL_MS = 420;
	const SHATTER_MS = 250;
	const TUMBLE_MS = 320;
	const DRILL_DIM_MS = 180;
	const DRILL_BURST_MS = 480;
	const ROUND_END_MS = 380;
	const LADDER_CLUNK_EXTRA_MS = 170;
	const SCATTER_EXTRA_MS = 260;
	const INTERSTITIAL_MS = 560;
	const TRUNC_NOTE_MS = 550;
	// Пауза между авто-спинами — тот же урок, что AUTO_SPIN_GAP_MS Азуманги:
	// без реального макротаска между спинами браузер не успевает отрисовать
	// результат прежде, чем следующий спин его сотрёт.
	const AUTO_SPIN_GAP_MS = 550;

	// --- типы ответа (зеркало serialize_animation/serialize_spin_result) ----
	type TetoBlock = {
		block_id: number;
		symbol_id: string;
		row: number;
		col: number;
		height: number;
		width: number;
	};
	type WinningLine = {
		line_index: number;
		symbol_id: string;
		length: number;
		block_ids: number[];
		cells: number[][];
	};
	type LadderUpdate = {
		score_after: number;
		multiplier_after: number;
		crossed_thresholds: number[];
		extra_freespins_awarded: number;
		next_threshold: number | null;
		next_multiplier: number | null;
		score_to_next: number | null;
	};
	type OpCommon = { seq: number; phase: 'base' | 'freespin'; round: number; blocks: TetoBlock[] };
	type FillOp = OpCommon & { op: 'fill' };
	type EvaluateOp = OpCommon & {
		op: 'evaluate';
		source: string;
		has_win: boolean;
		winning_block_ids: number[];
		winning_lines: WinningLine[];
		cells_won: number;
		payout: number;
	};
	type TumbleOp = OpCommon & {
		op: 'tumble';
		source: string;
		step: number;
		removed_block_ids: number[];
	};
	type DrillHuntOp = OpCommon & {
		op: 'drill_hunt';
		fired: boolean;
		wild_block_id: number | null;
		cells_converted: number;
		wave_fired: boolean;
	};
	type RoundEndOp = OpCommon & {
		op: 'round_end';
		raw_round_payout: number;
		multiplier_applied: number;
		final_round_payout: number;
		cells_converted_by_drill_hunt: number;
		tumble_steps_used: number;
		tumble_hard_cap_hit: boolean;
		scatter_count: number | null;
		freespins_awarded: number | null;
		ladder: LadderUpdate | null;
	};
	type TetoOp = FillOp | EvaluateOp | TumbleOp | DrillHuntOp | RoundEndOp;
	type TetoAnimation = {
		version: number;
		payout_paid: number;
		payout_engine_total: number;
		bank_capped: boolean;
		ladder_thresholds: { score: number; multiplier: number }[];
		ladder_max_score: number;
		ops: TetoOp[];
		ops_recorded: number;
		ops_total: number;
		rounds_recorded: number;
		rounds_total: number;
		complete_through_round: number | null;
		truncated: boolean;
		truncated_reason: string | null;
	};
	type TetoResult = {
		game: string;
		bet: number;
		payout: number;
		outcome: {
			scatter_count: number;
			freespins_awarded: number;
			freespins_played: number;
			freespins_hard_cap_hit: boolean;
			ladder_final_state: { score: number; multiplier: number; crossed_thresholds: number[] };
			total_payout: number;
			final_blocks: TetoBlock[];
		};
		user_balance_after: number;
		bank_capped: boolean;
		animation: TetoAnimation | null;
	};

	// Косметическая доска до первого спина. Только low/high символы — вайлд и
	// скаттер на ней были бы ложью («скаттеры считаются взглядом мгновенно»,
	// чеклист §8: 4 фейковых SCATTER-бэджа до спина — ровно анти-паттерн).
	const PLACEHOLDER_SYMBOLS = TETO_PAYTABLE_ORDER.filter((id) => {
		const tier = TETO_SYMBOLS[id]?.tier;
		return tier === 'low' || tier === 'high';
	});

	function placeholderBoard(): TetoBlock[] {
		const out: TetoBlock[] = [];
		for (let r = 0; r < GRID; r++) {
			for (let c = 0; c < GRID; c++) {
				out.push({
					block_id: r * GRID + c,
					symbol_id: PLACEHOLDER_SYMBOLS[(r * GRID + c * 3) % PLACEHOLDER_SYMBOLS.length],
					row: r,
					col: c,
					height: 1,
					width: 1
				});
			}
		}
		return out;
	}

	// --- состояние доски и реплеера -----------------------------------------
	let bet = $state(500);
	let lastBet = $state(500);
	let playing = $state(false);
	let blocks = $state<TetoBlock[]>(placeholderBoard());
	let winningIds = $state<Set<number>>(new Set());
	let removingIds = $state<Set<number>>(new Set());
	let enteringIds = $state<Set<number>>(new Set());
	let winLines = $state<WinningLine[]>([]);
	let drillBurstId = $state<number | null>(null);
	let boardDim = $state(false);
	let interstitial = $state<string | null>(null);

	// --- лестница множителя (шкала-вал дрели, §4/§5.2) ----------------------
	// Пороги/потолок — ТОЛЬКО из конверта (клиентской копии не заводим — §5.2:
	// перекалибруются вместе с паутейблом). До первого спина шкала стоит
	// приглушённой без насечек.
	let ladderThresholds = $state<{ score: number; multiplier: number }[] | null>(null);
	let ladderMaxScore = $state<number | null>(null);
	let ladderScore = $state(0);
	let ladderMultiplier = $state(1);
	let ladderCrossed = $state<Set<number>>(new Set());
	// Вариант Y (teto_drillhunt): взятый порог действует со СЛЕДУЮЩЕГО раунда
	// — честная подпись под шкалой, чтобы интерфейс не врал про уже
	// показанную выплату.
	let gaugeNote = $state<string | null>(null);
	let stampKey = $state(0);

	// --- фриспины -----------------------------------------------------------
	let fsActive = $state(false);
	let fsRound = $state(0);
	let fsTotal = $state(0);
	let bonusWinShown = $state(0);

	// --- деньги на экране ---------------------------------------------------
	let spinCounter = $state<number | null>(null);
	let roundRaw = $state(0);
	let roundStamp = $state<number | null>(null);
	let finalPayout = $state<number | null>(null);
	let outcomeTint = $state<'win' | 'lose' | null>(null);
	let bankCapInfo = $state<{ paid: number; total: number } | null>(null);
	let truncNote = $state<string | null>(null);
	let replayMode = $state(false);
	let scatterMsg = $state<string | null>(null);

	let error = $state<string | null>(null);
	let throttleMsg = $state<string | null>(null);

	let autoSpinsLeft = $state<number | null>(null);
	const autoSpinning = $derived(autoSpinsLeft !== null);

	// Грейдинг — ТОЛЬКО от payout (не payout_engine_total), §5.1.
	const bigWin = $derived(finalPayout !== null && finalPayout > 0 && finalPayout >= lastBet * 10);

	// --- отменяемые паузы реплеера ------------------------------------------
	// Тап по доске/«пропустить» будит ВСЕ ожидающие паузы разом; каждый шаг
	// цикла проверяет skipNow и выходит в settle(). Обязательная механика:
	// фриспин-спин может играться до 20 раундов.
	let skipNow = false;
	const pendingWakes = new Set<() => void>();

	function wait(ms: number): Promise<void> {
		if (skipNow) return Promise.resolve();
		return new Promise((resolve) => {
			const wake = () => {
				clearTimeout(t);
				pendingWakes.delete(wake);
				resolve();
			};
			const t = window.setTimeout(wake, ms);
			pendingWakes.add(wake);
		});
	}

	function requestSkip() {
		if (!playing || skipNow) return;
		skipNow = true;
		for (const wake of [...pendingWakes]) wake();
	}

	// Смена доски. Блоки рендерятся keyed по block_id (уникален в пределах
	// СПИНА, включая границы раундов — гарантия _next_block_id движка),
	// поэтому CSS-transition на top/left сам твинит падение выживших блоков;
	// genuinely-новые id получают drop-in.
	function setBoard(next: TetoBlock[], markEntering: boolean) {
		if (markEntering) {
			const prevIds = new Set(blocks.map((b) => b.block_id));
			enteringIds = new Set(next.filter((b) => !prevIds.has(b.block_id)).map((b) => b.block_id));
		} else {
			enteringIds = new Set();
		}
		blocks = next;
	}

	const LINE_COLORS = ['var(--accent-cyan)', 'var(--accent-pink)', 'var(--accent-yellow)'];

	// winning_lines.cells — [[row, col], ...] самодостаточной геометрией
	// (клиентской копии пейлайнов нет и не нужно). Центр клетки в viewBox
	// 600×600: (col·100+50, row·100+50).
	function linePoints(line: WinningLine): string {
		return line.cells.map((cell) => `${cell[1] * 100 + 50},${cell[0] * 100 + 50}`).join(' ');
	}

	// Финальное установившееся состояние — ЕДИНСТВЕННЫЙ выход из любого пути
	// (доигранный реплей, скип, reduced-motion, replay без анимации,
	// усечённый трейс): доска из outcome.final_blocks, лестница из
	// outcome.ladder_final_state, деньги из payout.
	function settle(res: TetoResult) {
		skipNow = true;
		for (const wake of [...pendingWakes]) wake();
		winningIds = new Set();
		removingIds = new Set();
		enteringIds = new Set();
		winLines = [];
		drillBurstId = null;
		boardDim = false;
		interstitial = null;
		roundRaw = 0;
		roundStamp = null;
		scatterMsg = null;
		fsActive = false;
		blocks = res.outcome.final_blocks;
		const lf = res.outcome.ladder_final_state;
		ladderScore = lf.score;
		ladderMultiplier = lf.multiplier;
		ladderCrossed = new Set(lf.crossed_thresholds);
		gaugeNote = null;
		spinCounter = res.payout;
		finalPayout = res.payout;
		outcomeTint = res.payout > 0 ? 'win' : 'lose';
		bankCapInfo = res.bank_capped
			? { paid: res.payout, total: res.outcome.total_payout }
			: null;
		playing = false;
		haptic(
			res.payout > 0 && res.payout >= lastBet * 10
				? 'big-win'
				: res.payout > 0
					? 'win'
					: 'lose'
		);
	}

	async function playOps(res: TetoResult, anim: TetoAnimation): Promise<void> {
		const paid = anim.payout_paid;
		let prefix = 0;
		let firstWinHapticDone = false;
		let bonusBaseCounter = 0;

		for (const op of anim.ops) {
			if (skipNow) return;

			if (op.op === 'fill') {
				winningIds = new Set();
				winLines = [];
				roundRaw = 0;
				roundStamp = null;
				drillBurstId = null;
				gaugeNote = null;
				if (op.phase === 'freespin') {
					fsActive = true;
					fsRound = op.round;
					scatterMsg = null;
					interstitial = `БОНУС-РАУНД ${op.round}`;
					haptic('spin');
					await wait(INTERSTITIAL_MS);
					interstitial = null;
					if (skipNow) return;
				}
				setBoard(op.blocks, true);
				await wait(FILL_MS);
			} else if (op.op === 'evaluate') {
				// Терминальный evaluate (has_win=false) — просто конец каскада,
				// визуального шага нет.
				if (!op.has_win) continue;
				winningIds = new Set(op.winning_block_ids);
				winLines = op.winning_lines;
				// Сырой счётчик РАУНДА (до множителя лестницы) — evaluate.payout
				// приходит сырым, множитель применяется РОВНО РАЗ в round_end.
				roundRaw += op.payout;
				if (!firstWinHapticDone) {
					haptic('win');
					firstWinHapticDone = true;
				}
				await wait(EVAL_MS);
			} else if (op.op === 'tumble') {
				winLines = [];
				winningIds = new Set();
				// Вспышка-осколки на удаляемых блоках ДО смены доски: старая
				// доска (с ними) остаётся в DOM на время exit-анимации.
				removingIds = new Set(op.removed_block_ids);
				await wait(SHATTER_MS);
				if (skipNow) return;
				removingIds = new Set();
				setBoard(op.blocks, true);
				haptic('reel-stop');
				await wait(TUMBLE_MS);
			} else if (op.op === 'drill_hunt') {
				if (!op.fired || op.wild_block_id === null) continue;
				// Денежный момент: притушить доску, посадить дрель.
				boardDim = true;
				await wait(DRILL_DIM_MS);
				if (skipNow) {
					boardDim = false;
					return;
				}
				// Доска op'а — ПОСЛЕ инъекции wild'а: смена доски и есть замена
				// символа на golden_drill; burst-класс делает её читаемой.
				setBoard(op.blocks, false);
				drillBurstId = op.wild_block_id;
				haptic('scatter');
				await wait(DRILL_BURST_MS);
				boardDim = false;
			} else if (op.op === 'round_end') {
				let extraWait = 0;
				if (op.phase === 'base' && (op.freespins_awarded ?? 0) > 0) {
					// Скаттеры объявляются на закрытии базового раунда; знаменатель
					// бэджа стартует с изначальной выдачи и растёт только на
					// реально показанных порогах лестницы (не спойлерим будущее);
					// min с freespins_played прикрывает FREESPINS_HARD_CAP.
					fsTotal = Math.min(op.freespins_awarded ?? 0, res.outcome.freespins_played);
					scatterMsg = `СКАТТЕР ×${op.scatter_count} → ${op.freespins_awarded} фриспинов`;
					haptic('scatter');
					extraWait += SCATTER_EXTRA_MS;
				}
				if (op.multiplier_applied > 1) {
					roundStamp = op.multiplier_applied;
				}
				if (op.ladder) {
					const l = op.ladder;
					ladderScore = l.score_after;
					ladderCrossed = new Set(l.crossed_thresholds);
					if (l.multiplier_after !== ladderMultiplier) {
						// Порог взят: штамп «клацает» новым множителем, но честно
						// подписан «со следующего раунда» (Вариант Y).
						ladderMultiplier = l.multiplier_after;
						gaugeNote = `×${l.multiplier_after} — со следующего раунда`;
						stampKey += 1;
						haptic('retrigger');
						extraWait += LADDER_CLUNK_EXTRA_MS;
					}
					if (l.extra_freespins_awarded > 0) {
						fsTotal = Math.min(
							fsTotal + l.extra_freespins_awarded,
							res.outcome.freespins_played
						);
					}
				}
				// ЕДИНСТВЕННАЯ формула бегущего счётчика спина (§5.1): min
				// применяется ВСЕГДА — счётчик никогда не превышает payout_paid
				// и заканчивается ровно на нём.
				prefix += op.final_round_payout;
				spinCounter = Math.min(prefix, paid);
				if (op.phase === 'freespin') {
					bonusWinShown = spinCounter - bonusBaseCounter;
				} else {
					bonusBaseCounter = spinCounter;
				}
				await wait(ROUND_END_MS + extraWait);
				roundStamp = null;
			}
		}
	}

	async function spin() {
		if (playing) return;
		error = null;
		throttleMsg = null;
		truncNote = null;
		replayMode = false;
		outcomeTint = null;
		finalPayout = null;
		bankCapInfo = null;
		spinCounter = null;
		roundRaw = 0;
		roundStamp = null;
		scatterMsg = null;
		bonusWinShown = 0;
		fsRound = 0;
		fsTotal = 0;
		lastBet = bet;
		skipNow = false;
		playing = true;
		haptic('spin');

		let res: TetoResult;
		try {
			res = await apiFetch<TetoResult>('/api/v1/games/teto_slots', {
				method: 'POST',
				body: JSON.stringify({ bet, idem_key: `teto:${crypto.randomUUID()}` })
			});
		} catch (err) {
			playing = false;
			if (err instanceof ApiError && err.status === 429) {
				// Троттлинг авто-спина — сообщение в тикере, не тост-ошибка.
				throttleMsg = 'не так быстро — подожди секунду и крути снова';
			} else {
				error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
			}
			haptic('error');
			return;
		}

		const anim = res.animation;
		if (anim) {
			// Статическая конфигурация лестницы — из конверта, один раз за спин.
			ladderThresholds = anim.ladder_thresholds;
			ladderMaxScore = anim.ladder_max_score;
		}

		const reducedMotion =
			typeof window !== 'undefined' &&
			window.matchMedia('(prefers-reduced-motion: reduce)').matches;

		// replay (animation:null) — НОРМАЛЬНЫЙ 200, не ошибка: финал без
		// реплеера. Неизвестная версия схемы — честная деградация туда же.
		if (anim === null || anim.version !== 1 || anim.ops.length === 0 || reducedMotion) {
			replayMode = anim === null;
			if (anim?.truncated) {
				truncNote = `…и ещё ${anim.rounds_total - anim.rounds_recorded} раундов`;
			}
			settle(res);
			return;
		}

		await playOps(res, anim);

		if (anim.truncated) {
			// Деградация по контракту: показанные раунды честные, дальше —
			// прыжок в финал из outcome с явным индикатором пропуска.
			truncNote = `…и ещё ${anim.rounds_total - anim.rounds_recorded} раундов`;
			if (!skipNow) {
				interstitial = truncNote;
				await wait(TRUNC_NOTE_MS);
				interstitial = null;
			}
		}
		settle(res);
	}

	// Авто-спин — та же дисциплина, что у Азуманги (runId против воскрешения
	// устаревшего цикла из паузы, реальный макротаск-гэп между спинами, стоп
	// на первой ошибке/троттле).
	let _autoSpinRunId = 0;

	async function runAutoSpin(count: number) {
		if (playing || autoSpinning) return;
		const myRunId = ++_autoSpinRunId;
		autoSpinsLeft = count;
		while (_autoSpinRunId === myRunId && autoSpinsLeft !== null && autoSpinsLeft > 0) {
			await spin();
			if (_autoSpinRunId !== myRunId || autoSpinsLeft === null || error || throttleMsg) break;
			autoSpinsLeft -= 1;
			if (autoSpinsLeft !== null && autoSpinsLeft > 0) {
				await wait(AUTO_SPIN_GAP_MS);
			}
		}
		if (_autoSpinRunId === myRunId) {
			autoSpinsLeft = null;
		}
	}

	function stopAutoSpin() {
		_autoSpinRunId += 1;
		autoSpinsLeft = null;
	}
</script>

<div class="teto-screen">
	<div class="menu-head">
		<h1 class="menu-title">Тето Брейнрот</h1>
		<div class="menu-sub">Дрель-Хант · 6×6 · 50 линий</div>
	</div>

	<!-- Шкала-вал дрели (§4 — фирменная деталь экрана): всегда видима, вне
	     фриспинов дремлет приглушённой; насечки — из ladder_thresholds
	     конверта (клиентской копии порогов нет). -->
	<div class="drill-gauge" class:gauge-live={fsActive}>
		<div class="gauge-head">
			<span class="gauge-title">Дрель-вал</span>
			{#key stampKey}
				<span class="gauge-stamp" class:stamp-idle={ladderMultiplier <= 1}
					>×{ladderMultiplier}</span
				>
			{/key}
		</div>
		<div class="gauge-track">
			<div
				class="gauge-fill"
				style:width={`${ladderMaxScore ? Math.min(100, (ladderScore / ladderMaxScore) * 100) : 0}%`}
			></div>
			{#if ladderThresholds && ladderMaxScore}
				{#each ladderThresholds as t (t.score)}
					<div
						class="gauge-notch"
						class:notch-crossed={ladderCrossed.has(t.score)}
						style:left={`${(t.score / ladderMaxScore) * 100}%`}
					>
						<span class="notch-label">×{t.multiplier}</span>
					</div>
				{/each}
			{/if}
		</div>
		<div class="gauge-note" class:gauge-note-hot={gaugeNote !== null}>
			{#if gaugeNote}
				{gaugeNote}
			{:else if fsActive}
				очки дрели: <span class="gauge-score">{ladderScore}</span>
			{:else}
				множитель растёт во фриспинах
			{/if}
		</div>
	</div>

	<!-- Доска 6×6. Тап по ней во время реплея — скип (обязателен: до 20
	     раундов проигрыша). Блоки keyed по block_id: transition top/left
	     твинит гравитацию выживших сам. -->
	<div
		class="teto-board"
		class:board-live={playing}
		class:board-dim={boardDim}
		class:board-win={outcomeTint === 'win'}
		class:board-lose={outcomeTint === 'lose'}
		role="button"
		tabindex="0"
		aria-label="Пропустить анимацию"
		onclick={requestSkip}
		onkeydown={(e) => {
			if (e.key === 'Enter' || e.key === ' ') requestSkip();
		}}
	>
		{#each blocks as b (b.block_id)}
			{@const sym = TETO_SYMBOLS[b.symbol_id]}
			<div
				class="teto-block"
				class:block-hit={winningIds.has(b.block_id)}
				class:block-removing={removingIds.has(b.block_id)}
				class:block-entering={enteringIds.has(b.block_id)}
				class:block-drill={drillBurstId === b.block_id}
				class:block-wild={sym?.tier === 'wild'}
				class:block-scatter={sym?.tier === 'scatter'}
				style={`left:${(b.col / GRID) * 100}%;top:${(b.row / GRID) * 100}%;width:${(b.width / GRID) * 100}%;height:${(b.height / GRID) * 100}%;--tint:${sym?.tint ?? '#333'};--drop-delay:${(b.row + b.col) * 20}ms`}
			>
				<div class="teto-cell">
					<img src={tetoSymbolSrc(b.symbol_id)} alt={sym?.name ?? b.symbol_id} draggable="false" />
					{#if sym?.tier === 'wild'}<span class="block-badge badge-wild">WILD</span>{/if}
					{#if sym?.tier === 'scatter'}<span class="block-badge badge-scatter">SCATTER</span>{/if}
				</div>
			</div>
		{/each}

		{#if winLines.length > 0}
			<svg class="teto-lines" viewBox="0 0 600 600" preserveAspectRatio="none" aria-hidden="true">
				{#each winLines as line (line.line_index)}
					<polyline
						points={linePoints(line)}
						style={`stroke:${LINE_COLORS[line.line_index % LINE_COLORS.length]}`}
					/>
				{/each}
			</svg>
		{/if}

		{#if fsActive}
			<!-- Бонус-бэдж — оверлей на доске (не сдвигает сетку): раунд N/M +
			     накопленный за бонус выигрыш (капнутый той же min-формулой). -->
			<div class="bonus-badge">
				<span class="bonus-badge-label">✦ БОНУС</span>
				{#key fsRound}
					<span class="bonus-badge-count">{fsRound} / {fsTotal}</span>
				{/key}
				{#if bonusWinShown > 0}
					<span class="bonus-badge-win">+{bonusWinShown}¥</span>
				{/if}
			</div>
		{/if}

		{#if interstitial}
			<div class="board-interstitial"><span>{interstitial}</span></div>
		{/if}
	</div>

	<div class="teto-ticker">
		{#if throttleMsg}
			<span class="ticker-throttle">{throttleMsg}</span>
		{:else if playing}
			{#if scatterMsg}
				<span class="ticker-scatter">{scatterMsg}</span>
			{:else if fsActive}
				<span class="ticker-scatter">бонус-раунд {fsRound} / {fsTotal}…</span>
			{:else}
				<span class="ticker-idle">каскады…</span>
			{/if}
			<button type="button" class="skip-btn" onclick={requestSkip}>пропустить →</button>
		{:else if replayMode}
			<span class="ticker-idle">повтор раунда</span>
		{:else if truncNote}
			<span class="ticker-idle">{truncNote}</span>
		{:else if finalPayout !== null && finalPayout > 0}
			<span class="ticker-win">выигрыш за спин</span>
		{:else if finalPayout !== null}
			<span class="ticker-idle">не в этот раз — крути ещё</span>
		{:else}
			<span class="ticker-idle">жми · крути мегаблоки</span>
		{/if}
	</div>

	<div class="win-area">
		{#if playing}
			{#if spinCounter !== null && spinCounter > 0}
				<div class="spin-counter" data-testid="spin-counter">
					{spinCounter}<small>¥</small>
				</div>
			{/if}
			{#if roundRaw > 0}
				<div class="round-raw">
					раунд: +{roundRaw}¥
					{#if roundStamp}<span class="round-mult">×{roundStamp}</span>{/if}
				</div>
			{/if}
		{:else if finalPayout !== null}
			<div
				class={`teto-result ${finalPayout > 0 ? 'res-win' : 'res-lose'} ${bigWin ? 'res-big' : ''}`}
				data-testid="final-result"
			>
				{#if bigWin}<span class="big-win-label">БОЛЬШОЙ ВЫИГРЫШ!</span>{/if}
				<span class="res-amount">{finalPayout > 0 ? `+${finalPayout}` : `−${lastBet}`}<small
						>¥</small
					></span
				>
			</div>
			{#if bankCapInfo}
				<!-- Честная подпись про кап банком — единственное применение
				     payout_engine_total (§5.1). -->
				<div class="bank-cap-note" data-testid="bank-cap-note">
					выплачено {bankCapInfo.paid} из {bankCapInfo.total}¥ — банк чата пуст
				</div>
			{/if}
		{/if}
	</div>

	<BetControl bind:bet disabled={playing || autoSpinning} step={BET_STEP} chips={BET_CHIPS} />

	<div class="autospin-row">
		<span class="autospin-label">авто-ставка</span>
		<div class="autospin-chips">
			{#each AUTO_SPIN_PRESETS as n (n)}
				<button
					type="button"
					class="chip autospin-chip"
					disabled={playing || autoSpinning}
					onclick={() => runAutoSpin(n)}
				>
					{n}
				</button>
			{/each}
			<button
				type="button"
				class="chip autospin-chip"
				disabled={playing || autoSpinning}
				onclick={() => runAutoSpin(Infinity)}
			>
				∞
			</button>
		</div>
	</div>

	<button
		type="button"
		class="teto-cta"
		disabled={!autoSpinning && playing}
		onclick={() => (autoSpinning ? stopAutoSpin() : spin())}
	>
		<span class="teto-cta-label">
			{#if autoSpinning}
				СТОП ({autoSpinsLeft === Infinity ? '∞' : autoSpinsLeft} ост.)
			{:else if playing}
				крутим…
			{:else}
				КРУТИТЬ
			{/if}
		</span>
		<span class="teto-cta-sub">
			{#if autoSpinning}
				ставка {bet}¥ · жми стоп, чтобы прервать
			{:else if !playing}
				ставка {bet}¥ · 50 линий
			{/if}
		</span>
	</button>

	{#if error}
		<div class="teto-error">{error}</div>
	{/if}
</div>

<style>
	.teto-screen {
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

	/* ── шкала-вал дрели ─────────────────────────────────────────────────── */
	.drill-gauge {
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
		padding: var(--space-sm) var(--space-md) var(--space-sm);
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 14px;
		margin: 0 2px;
		opacity: 0.68;
		transition:
			opacity 0.3s ease-out,
			border-color 0.3s ease-out,
			box-shadow 0.3s ease-out;
	}
	.gauge-live {
		opacity: 1;
		border-color: var(--accent-yellow);
		box-shadow: 0 0 18px rgba(255, 216, 74, 0.18);
	}
	.gauge-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}
	.gauge-title {
		font-family: var(--font-chrome);
		font-size: var(--font-label-size);
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	/* Штамп активного множителя — .chip-подпись (жёсткая рамка + смещённая
	   тень), «клацает» на взятом пороге ({#key stampKey} пересоздаёт узел,
	   анимация переигрывает). */
	.gauge-stamp {
		display: inline-block;
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		background: var(--accent-yellow);
		color: #1a0f12;
		border: 2px solid #111;
		border-radius: 8px;
		padding: 1px 10px;
		box-shadow: 2px 2px 0 #111;
		letter-spacing: 0.04em;
		animation: tetoStampClunk 0.3s cubic-bezier(0.2, 1.6, 0.4, 1);
	}
	.stamp-idle {
		background: #fff;
		opacity: 0.75;
		animation: none;
	}
	@keyframes tetoStampClunk {
		0% {
			transform: scale(1.55) rotate(-6deg);
		}
		100% {
			transform: scale(1) rotate(0deg);
		}
	}
	.gauge-track {
		position: relative;
		height: 18px;
		margin-top: 16px; /* место для насечек-подписей над валом */
		background: var(--bg-dominant);
		border: 1px solid var(--border-secondary);
		border-radius: 999px;
	}
	.gauge-fill {
		position: absolute;
		left: 0;
		top: 0;
		bottom: 0;
		background: var(--accent-yellow);
		border-radius: 999px;
		transition: width 0.45s ease-out;
	}
	.gauge-notch {
		position: absolute;
		top: -3px;
		bottom: -3px;
		width: 2px;
		background: var(--border-secondary);
		transform: translateX(-50%);
	}
	.notch-crossed {
		background: var(--accent-yellow);
	}
	.notch-label {
		position: absolute;
		top: -17px;
		left: 50%;
		transform: translateX(-50%);
		font-family: var(--font-shout);
		font-size: var(--font-label-size);
		color: var(--text-muted);
		letter-spacing: 0.04em;
		white-space: nowrap;
	}
	.notch-crossed .notch-label {
		color: var(--accent-yellow);
	}
	.gauge-note {
		min-height: 18px;
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		color: var(--text-muted);
	}
	/* Вариант Y: взятый порог — «со следующего раунда», подпись честная. */
	.gauge-note-hot {
		color: var(--accent-yellow);
		font-weight: 700;
	}
	.gauge-score {
		font-family: var(--font-numeric);
		color: var(--text-primary);
	}

	/* ── доска ───────────────────────────────────────────────────────────── */
	.teto-board {
		position: relative;
		aspect-ratio: 1;
		/* Pitfall 4 (как у Азуманги): анимированный контент не липнет к краю
		   экрана — не конфликтует с edge-swipe-close Телеграма. */
		margin: 0 2px;
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
		border-radius: 14px;
		overflow: hidden;
		padding: 2px;
		transition:
			border-color 0.25s ease-out,
			box-shadow 0.25s ease-out;
	}
	.board-win {
		border-color: var(--positive);
		box-shadow:
			0 0 0 2px var(--positive),
			0 0 26px rgba(46, 224, 106, 0.35);
	}
	.board-lose {
		border-color: var(--destructive);
		box-shadow:
			0 0 0 2px var(--destructive),
			0 0 20px rgba(255, 56, 56, 0.22);
	}

	.teto-block {
		position: absolute;
		padding: 2px;
	}
	/* Твин гравитации: transition на top/left только в live-режиме — settle()
	   и replay ставят финальную доску прыжком, без ложного «переезда». */
	.board-live .teto-block {
		transition:
			top 0.28s cubic-bezier(0.25, 0.7, 0.35, 1.12),
			left 0.28s cubic-bezier(0.25, 0.7, 0.35, 1.12),
			width 0.28s ease-out,
			height 0.28s ease-out;
	}
	.teto-cell {
		position: relative;
		width: 100%;
		height: 100%;
		border-radius: 8px;
		background: color-mix(in srgb, var(--tint) 25%, var(--bg-secondary-2));
		border: 1px solid var(--border-secondary);
		overflow: hidden;
		transition: filter 0.18s ease-out;
	}
	.teto-cell img {
		width: 100%;
		height: 100%;
		object-fit: contain;
		padding: 5%;
		display: block;
	}
	.block-wild .teto-cell {
		border-color: var(--accent-yellow);
		box-shadow: 0 0 10px rgba(255, 216, 74, 0.45);
	}
	.block-scatter .teto-cell {
		border-color: var(--accent-pink);
	}
	.block-badge {
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
	.badge-wild {
		background: var(--accent-yellow);
		color: #1a0f12;
	}
	.badge-scatter {
		background: var(--accent-pink);
		color: #1a0f12;
	}

	/* Выигравшие блоки — пульс/подсветка ДО удаления (чеклист §8). */
	.block-hit {
		z-index: 3;
	}
	.board-live .block-hit .teto-cell {
		border-color: var(--accent-yellow);
		box-shadow:
			0 0 0 2px var(--accent-yellow),
			0 0 14px rgba(255, 216, 74, 0.55);
		animation: tetoWinPulse 0.46s ease-in-out infinite;
	}
	@keyframes tetoWinPulse {
		0%,
		100% {
			transform: scale(1);
			filter: brightness(1.08);
		}
		50% {
			transform: scale(1.045);
			filter: brightness(1.4);
		}
	}

	/* Вспышка-осколки удаляемого блока (exit до смены доски). */
	.block-removing {
		z-index: 2;
	}
	.board-live .block-removing .teto-cell {
		animation: tetoShatter 0.25s ease-in forwards;
	}
	@keyframes tetoShatter {
		0% {
			transform: scale(1);
			opacity: 1;
			filter: brightness(1.7);
		}
		45% {
			transform: scale(1.06) rotate(-2deg);
			opacity: 1;
			filter: brightness(1.9);
		}
		100% {
			transform: scale(0.5) rotate(7deg);
			opacity: 0;
			filter: brightness(1.2);
		}
	}

	/* Drop-in новых блоков (fill целиком и добор после тумбла). */
	.board-live .block-entering .teto-cell {
		animation: tetoDrop 0.28s cubic-bezier(0.3, 0.7, 0.4, 1.15) both;
		animation-delay: var(--drop-delay, 0ms);
	}
	@keyframes tetoDrop {
		0% {
			transform: translateY(-130%);
			opacity: 0;
		}
		55% {
			opacity: 1;
		}
		82% {
			transform: translateY(4%);
		}
		100% {
			transform: translateY(0);
			opacity: 1;
		}
	}

	/* Дрель-Хант: доска тухнет, wild-блок в прожекторе с бёрстом. */
	.board-dim .teto-cell {
		filter: brightness(0.4) saturate(0.7);
	}
	.block-drill {
		z-index: 5;
	}
	.board-dim .block-drill .teto-cell,
	.block-drill .teto-cell {
		filter: none;
		border-color: var(--accent-yellow);
		box-shadow:
			0 0 0 2px var(--accent-yellow),
			0 0 26px rgba(255, 216, 74, 0.85);
	}
	.board-live .block-drill .teto-cell {
		animation: tetoDrillBurst 0.48s cubic-bezier(0.2, 1.4, 0.4, 1);
	}
	@keyframes tetoDrillBurst {
		0% {
			transform: scale(0.55) rotate(-160deg);
			filter: brightness(2);
		}
		70% {
			transform: scale(1.14) rotate(8deg);
		}
		100% {
			transform: scale(1) rotate(0deg);
			filter: brightness(1.15);
		}
	}

	/* Победные линии — самодостаточные cells из winning_lines, оверлей SVG. */
	.teto-lines {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		pointer-events: none;
		z-index: 4;
	}
	.teto-lines polyline {
		fill: none;
		stroke-width: 7;
		stroke-linecap: round;
		stroke-linejoin: round;
		opacity: 0.92;
		filter: drop-shadow(0 1px 3px rgba(0, 0, 0, 0.8));
	}

	/* Бонус-бэдж — оверлей, не сдвигает сетку (чеклист: размеры стабильны). */
	.bonus-badge {
		position: absolute;
		top: 8px;
		left: 8px;
		z-index: 6;
		display: flex;
		align-items: baseline;
		gap: 6px;
		background: var(--accent-yellow);
		color: #1a0f12;
		border: 2px solid #111;
		border-radius: 10px;
		padding: 4px 10px;
		box-shadow: 3px 3px 0 #111;
		font-family: var(--font-body);
	}
	.bonus-badge-label {
		font-size: 11px;
		font-weight: 900;
		letter-spacing: 0.06em;
	}
	.bonus-badge-count {
		display: inline-block;
		font-family: var(--font-numeric);
		font-size: var(--font-heading-size);
		font-weight: 900;
		line-height: 1;
		animation: tetoBadgeBump 0.3s ease-out;
	}
	.bonus-badge-win {
		font-size: 11px;
		font-weight: 900;
	}
	@keyframes tetoBadgeBump {
		0% {
			transform: scale(1.5);
		}
		100% {
			transform: scale(1);
		}
	}

	/* Интерстишл «БОНУС-РАУНД N» / индикатор усечения. */
	.board-interstitial {
		position: absolute;
		inset: 0;
		z-index: 7;
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgba(10, 6, 8, 0.55);
	}
	.board-interstitial span {
		font-family: var(--font-shout);
		font-size: var(--font-display-size);
		color: var(--accent-yellow);
		background: #1a0f12;
		border: 2px solid var(--accent-yellow);
		border-radius: 12px;
		padding: var(--space-sm) var(--space-md);
		box-shadow: 4px 4px 0 #111;
		letter-spacing: 0.04em;
		text-align: center;
		animation: tetoInterstitialPop 0.22s ease-out;
	}
	@keyframes tetoInterstitialPop {
		0% {
			transform: translateY(-8px) scale(0.88);
			opacity: 0;
		}
		100% {
			transform: translateY(0) scale(1);
			opacity: 1;
		}
	}

	/* ── тикер + скип ────────────────────────────────────────────────────── */
	.teto-ticker {
		position: relative;
		min-height: 44px;
		display: flex;
		align-items: center;
		justify-content: center;
		text-align: center;
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		padding: 0 112px; /* симметричный резерв под кнопку «пропустить →» */
	}
	.ticker-win {
		color: var(--positive-text);
		font-weight: 700;
	}
	.ticker-scatter {
		color: var(--accent-yellow);
		font-weight: 700;
	}
	.ticker-throttle {
		color: var(--accent-yellow);
	}
	.ticker-idle {
		color: var(--text-muted);
	}
	.skip-btn {
		position: absolute;
		right: 0;
		top: 50%;
		transform: translateY(-50%);
		background: none;
		border: 1px solid var(--border-secondary);
		color: var(--text-muted);
		border-radius: 999px;
		padding: 4px 12px;
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		cursor: pointer;
	}
	.skip-btn:active {
		color: var(--text-primary);
		border-color: var(--text-muted);
	}

	/* ── деньги ──────────────────────────────────────────────────────────── */
	.win-area {
		min-height: 64px;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: var(--space-xs);
	}
	.spin-counter {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		color: var(--positive-text);
		line-height: 1;
	}
	.spin-counter small {
		font-size: var(--font-body-size);
		margin-left: 2px;
	}
	.round-raw {
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		color: var(--text-muted);
	}
	.round-mult {
		display: inline-block;
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		color: #1a0f12;
		background: var(--accent-yellow);
		border: 2px solid #111;
		border-radius: 6px;
		padding: 0 6px;
		margin-left: 4px;
		box-shadow: 2px 2px 0 #111;
		animation: tetoStampClunk 0.3s cubic-bezier(0.2, 1.6, 0.4, 1);
	}
	.teto-result {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 2px;
		padding: 2px 16px;
		border-radius: 10px;
		text-align: center;
	}
	.res-amount {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		line-height: 1.1;
	}
	.res-amount small {
		font-size: var(--font-body-size);
	}
	/* Масштаб цифр — от payout, «большой выигрыш» = hero-кегль (шкала 4
	   размеров, hero = 64). */
	.res-big .res-amount {
		font-size: var(--font-hero-size);
	}
	.big-win-label {
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		color: var(--accent-yellow);
		letter-spacing: 0.06em;
	}
	.res-win {
		color: var(--positive-text);
		background: var(--positive-bg);
	}
	.res-lose {
		color: var(--destructive-text);
		background: var(--destructive-bg);
	}
	.bank-cap-note {
		font-family: var(--font-body);
		font-size: var(--font-body-size);
		color: var(--destructive-text);
		text-align: center;
	}

	/* ── управление ──────────────────────────────────────────────────────── */
	.autospin-row {
		display: flex;
		align-items: center;
		gap: var(--space-md);
	}
	.autospin-label {
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
		white-space: nowrap;
	}
	.autospin-chips {
		display: grid;
		grid-template-columns: repeat(5, 1fr);
		gap: var(--space-xs);
		flex: 1;
	}

	.teto-cta {
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
	.teto-cta:active:not(:disabled) {
		transform: translate(2px, 2px);
		box-shadow: 2px 2px 0 #111;
	}
	.teto-cta:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.teto-cta-label {
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		color: #1a0f12;
		letter-spacing: 0.04em;
	}
	.teto-cta-sub {
		font-size: var(--font-body-size);
		color: #3a1420;
		font-family: var(--font-body);
	}

	.teto-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	/* prefers-reduced-motion: чистое движение гасится в CSS; сам реплеер
	   при этом вообще не запускается (JS-гейт в spin() ведёт прямо в
	   settle()) — тот же двухуровневый подход, что у Азуманги. */
	@media (prefers-reduced-motion: reduce) {
		.board-live .teto-block {
			transition: none;
		}
		.board-live .block-entering .teto-cell,
		.board-live .block-removing .teto-cell,
		.board-live .block-hit .teto-cell,
		.board-live .block-drill .teto-cell {
			animation: none;
		}
		.gauge-stamp,
		.round-mult,
		.bonus-badge-count,
		.board-interstitial span {
			animation: none;
		}
		.gauge-fill {
			transition: none;
		}
	}
</style>
