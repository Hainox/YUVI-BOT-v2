// ─────────────────────────────────────────────────────────────────────
// Slot machine UI bits: WinTicker, PayTable, BetControls, BigWinFX
// ─────────────────────────────────────────────────────────────────────

const { SYMBOLS, SYMBOL_LIST } = window.SLOT_DATA;

// Idle prompts — rotated before/between spins. Azumanga-flavoured nonsense.
const IDLE_LINES = [
  { jp: 'ぼーっと…',   en: 'ОСАКА ЗАВИСЛА…' },
  { jp: 'ガチャっと',  en: 'ДЁРГАЙ РЫЧАГ' },
  { jp: 'ドキドキ',    en: 'Hello, everynyan' },
  { jp: 'スロット!',   en: 'СЛОТЫ, БРАТ' },
  { jp: 'うにゅ?',     en: 'А?' },
  { jp: '昼休み',      en: 'ОБЕДЕННЫЕ СЛОТЫ' },
  { jp: 'いくぞ〜',    en: 'Окееееей, лес гоуу' },
  { jp: '猫だ!',       en: 'Крутани Осаку' },
  { jp: 'ちよちゃん',  en: 'ГДЕ ЧИЕ-ЧАН' },
  { jp: 'ぽえ〜',      en: 'ПОЭ~' },
  { jp: 'やる気ない',  en: 'НЕТ СИЛ' },
  { jp: 'むぎゅ',      en: 'МУГЮ' },
];

function WinTicker({ wins, active, spinning, hype, skin, totalWin }) {
  // Idle ticker: rotate through Azumanga-flavoured prompts every 3.5s.
  const [idleIdx, setIdleIdx] = React.useState(() => Math.floor(Math.random() * IDLE_LINES.length));
  const isIdle = !spinning && (!wins || wins.length === 0) && !hype;
  React.useEffect(() => {
    if (!isIdle) return;
    const t = setInterval(() => setIdleIdx((i) => (i + 1) % IDLE_LINES.length), 3500);
    return () => clearInterval(t);
  }, [isIdle]);

  if (spinning) {
    return (
      <div className="win-ticker">
        <div className="ticker-spinning">
          <span>クルクル</span> крутимся <span>クルクル</span>
        </div>
      </div>
    );
  }
  if (wins && wins.length > 0) {
    const w = wins[active];
    if (!w) return <div className="win-ticker" />;
    if (w.scatter) {
      return (
        <div className="win-ticker win-ticker-hit">
          <strong>СКАТТЕР ×{w.count}!</strong>
            <span className="ticker-line">→ {w.freespins} фриспинов</span>
        </div>
      );
    }
    const sym = SYMBOLS[w.symbolId];
    return (
      <div className="win-ticker win-ticker-hit">
        <div className="ticker-symbol">
          <img src={sym.src} alt="" />
        </div>
        <div className="ticker-text">
          <strong>{sym.name} ×{w.count}</strong>
          <span className="ticker-line">линия {w.lineIdx + 1} · +{w.payout}</span>
        </div>
        <div className="ticker-amount">+{w.payout}<small>¥</small></div>
      </div>
    );
  }
  if (hype) {
    return (
      <div className="win-ticker win-ticker-shrug">
        <span className="jp-bubble">{hype.jp}</span>
        <span className="en-shout">{hype.en}</span>
      </div>
    );
  }
  const idle = IDLE_LINES[idleIdx];
  return (
    <div className="win-ticker win-ticker-idle" key={idleIdx}>
      <span className="jp-bubble">{idle.jp}</span>
      <span className="en-shout">{idle.en}</span>
    </div>
  );
}

// ── PayTable: compact reference with all symbol payouts ──────────────
function PayTable() {
  const [open, setOpen] = React.useState(false);
  return (
    <div className={`paytable ${open ? 'paytable-open' : ''}`}>
      <button className="paytable-toggle" type="button" onClick={() => setOpen((o) => !o)}>
        <span className="pt-bullet">5 БАРАБАНОВ · 3 РЯДА · 10 ЛИНИЙ · 3+ СЛЕВА</span>
        <span className="pt-chev">{open ? '▴' : '▾'}</span>
      </button>
      {open && (
        <div className="paytable-grid">
          {SYMBOL_LIST.map((s) => (
            <div key={s.id} className={`pt-row pt-row-${s.role}`}>
              <div className="pt-thumb"><img src={s.src} alt="" /></div>
              <div className="pt-meta">
                <div className="pt-name">{s.name} <span className="pt-jp">{s.jp}</span></div>
                {s.role === 'wild' && <div className="pt-tag">ВАЙЛД · замена</div>}
                {s.role === 'scatter' && <div className="pt-tag">СКАТТЕР · фриспины</div>}
                {s.role !== 'wild' && s.role !== 'scatter' && (
                  <div className="pt-pays">3/{s.pay[3]} · 4/{s.pay[4]} · 5/{s.pay[5]}</div>
                )}
              </div>
            </div>
          ))}
          <div className="pt-note">
            ×ставка-на-линию (bet/10) за 3/4/5. 3+ ✦ → 4-7 фриспинов ×2.
          </div>
        </div>
      )}
    </div>
  );
}

// ── BetControls: chips, spin button, auto-spin row ───────────────────
const BET_CHIPS = [10, 50, 100, 500, 1000];

function BetControls({ bet, setBet, balance, canSpin, spinning, freespins, onSpin, auto, setAuto, skin }) {
  return (
    <div className="bet-controls">
      <div className="bet-row">
        <div className="bet-display">
          <span className="bet-label">ставка</span>
          <div className="bet-amount">{bet}<small>¥</small></div>
        </div>
        <div className="bet-chips">
          {BET_CHIPS.map((v) => (
            <button key={v} type="button"
                    className={`chip ${bet === v ? 'chip-on' : ''}`}
                    onClick={() => setBet(v)} disabled={spinning}>
              {v}
            </button>
          ))}
          <button type="button"
                  className={`chip chip-all ${bet === balance && balance > 0 ? 'chip-on' : ''}`}
                  onClick={() => setBet(Math.max(10, balance))} disabled={spinning || balance <= 0}>
            all
          </button>
        </div>
      </div>

      <button type="button"
              className={`spin-btn spin-btn-${skin.id} ${spinning ? 'spinning' : ''}`}
              onClick={onSpin}
              disabled={!canSpin || auto > 0}>
        <span className="spin-shadow" />
        <span className="spin-label">
          {spinning ? 'крутим…'
            : auto > 0 ? `АВТО · ${auto}`
            : freespins > 0 ? `ФРИСПИН ✦ ${freespins}`
            : 'OH MA GAAAAWD'}
        </span>
        <span className="spin-sub">
          {spinning ? '' : auto > 0 ? 'НЕ ОСТАНОВИТЬСЯ' : `жми · ставка ${bet}¥`}
        </span>
      </button>

      <div className="auto-row">
        <span className="auto-lbl">авто</span>
        {[10, 25, 50].map((n) => (
          <button key={n} type="button"
                  className={`chip auto-chip ${auto === n ? 'chip-on' : ''}`}
                  onClick={() => setAuto(auto === n ? 0 : n)} disabled={spinning && auto === 0}>
            ×{n}
          </button>
        ))}
        <button type="button" className={`chip auto-chip ${auto === 9999 ? 'chip-on' : ''}`}
                onClick={() => setAuto(auto === 9999 ? 0 : 9999)} disabled={spinning && auto === 0}>
          ∞
        </button>
        {auto > 0 && (
          <button type="button" className="chip auto-chip auto-stop"
                  onClick={() => setAuto(0)}>
            СТОП
          </button>
        )}
      </div>
    </div>
  );
}

// ── BigWinFX: full-screen lightning + chibi reaction + JP scream ─────
function BigWinFX({ bigWin, skin }) {
  const sym = bigWin.win ? SYMBOLS[bigWin.win.symbolId] : null;
  const hype = window.HYPE_LINES[Math.floor(Math.random() * window.HYPE_LINES.length)];
  return (
    <div className="big-win-fx">
      <div className="bw-veil" />
      <LightningSVG />
      <div className="bw-stage">
        <div className="bw-jp">{hype.jp}</div>
        <div className="bw-en">{hype.en}</div>
        {sym && (
          <div className="bw-symbol">
            <img src={sym.src} alt={sym.name} />
            <div className="bw-symbol-ring" />
          </div>
        )}
        <div className="bw-amount">
          <small>+</small>{bigWin.amount.toLocaleString('ru-RU')}<small>¥</small>
        </div>
        <div className="bw-tag">КРУПНЫЙ ВЫИГРЫШ!! · ビッグ</div>
      </div>
      <div className="bw-rays" />
    </div>
  );
}

function ScatterFX() {
  return (
    <div className="scatter-fx">
      <div className="scatter-shout">
        <div className="ss-jp">スキャッター</div>
        <div className="ss-en">СКАТТЕР!</div>
        <div className="ss-sub">ФРИСПИНЫ ОТКРЫТЫ</div>
      </div>
    </div>
  );
}

// SVG lightning bolts — drawn at varied positions, flicker via CSS keyframes
function LightningSVG() {
  return (
    <svg className="lightning" viewBox="0 0 400 700" preserveAspectRatio="none"
         aria-hidden="true">
      <defs>
        <filter id="lightning-glow">
          <feGaussianBlur stdDeviation="2.4" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>
      <g filter="url(#lightning-glow)" stroke="#fff" fill="none"
         strokeLinecap="round" strokeLinejoin="round">
        <path className="bolt bolt1"
              d="M 60 0 L 80 110 L 50 150 L 110 240 L 70 290 L 130 410 L 90 460 L 150 600 L 120 700"
              strokeWidth="4" />
        <path className="bolt bolt2"
              d="M 340 0 L 310 90 L 350 170 L 290 280 L 340 360 L 280 480 L 330 560 L 270 700"
              strokeWidth="4" />
        <path className="bolt bolt3"
              d="M 200 0 L 180 130 L 230 210 L 170 320 L 240 410 L 170 540 L 220 700"
              strokeWidth="3" />
        <path className="bolt bolt4"
              d="M 30 200 L 90 270 L 50 360 L 110 470 L 70 580"
              strokeWidth="2.5" />
        <path className="bolt bolt5"
              d="M 380 220 L 320 290 L 370 380 L 310 500 L 360 600"
              strokeWidth="2.5" />
      </g>
    </svg>
  );
}

window.WinTicker = WinTicker;
window.PayTable = PayTable;
window.BetControls = BetControls;
window.BigWinFX = BigWinFX;
window.ScatterFX = ScatterFX;
