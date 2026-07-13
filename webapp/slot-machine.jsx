// ─────────────────────────────────────────────────────────────────────
// SlotMachine — server-driven version.
//
// All randomness, win detection, and balance changes happen on the
// server. This component owns: bet selection, spin animation, win
// celebration, auto-spin loop. It calls `remoteSpin(bet)` and trusts
// whatever comes back. Balance is a controlled prop from the parent.
// ─────────────────────────────────────────────────────────────────────

const { SYMBOLS, randomGrid } = window.SLOT_DATA;
const { Reel } = window;

// JP exclamations for win/lose moments
const HYPE_LINES = [
  { jp: 'すごい!', en: 'НИЧЕГО СЕБЕ!' },
  { jp: 'やった!', en: 'УРААА!' },
  { jp: 'やばい!', en: 'ЖЕСТЬ!' },
  { jp: '神!', en: 'БОГ!!' },
  { jp: 'マジ?!', en: 'СЕРЬЁЗНО??' },
  { jp: '勝った!', en: 'ПОБЕДА!' },
];
const SHRUG_LINES = [
  { jp: '残念', en: 'НУ ТАКОЕ' },
  { jp: 'う〜ん', en: 'МДА…' },
  { jp: 'まだまだ', en: 'НЕ СЕГОДНЯ' },
  { jp: 'もう一回', en: 'ЕЩЁ РАЗ' },
  { jp: '道はない', en: 'НЕТ ПУТИ…..' },
];

function SlotMachine({
  skin,
  spinSpeed = 'normal',
  absurdLevel = 'full',
  user = null,
  haptic = () => {},
  balance = 0,
  remoteSpin,            // async (bet) → server response, see slot-screen.jsx
}) {
  const [grid, setGrid] = React.useState(() => randomGrid());
  const [spinning, setSpinning] = React.useState(false);
  const [bet, setBet] = React.useState(100);
  const [wins, setWins] = React.useState([]);
  const [highlightedWin, setHighlightedWin] = React.useState(0);
  const [bigWin, setBigWin] = React.useState(null);
  const [hype, setHype] = React.useState(null);
  const [shake, setShake] = React.useState(false);
  const [autoSpin, setAutoSpin] = React.useState(0);
  const [freespins, setFreespins] = React.useState(0);
  const [scatterFlash, setScatterFlash] = React.useState(false);
  const [errMsg, setErrMsg] = React.useState(null);

  const spinTimerRef = React.useRef(null);
  const cycleTimerRef = React.useRef(null);
  const autoTimerRef = React.useRef(null);

  const tileSize = 64;
  const gap = 6;

  const spinDurations = {
    slow:   { base: 1600, perReel: 220 },
    normal: { base: 1100, perReel: 160 },
    fast:   { base: 700,  perReel: 100 },
    turbo:  { base: 350,  perReel: 60  },
  };
  const { base: spinBase, perReel } = spinDurations[spinSpeed] || spinDurations.normal;

  async function doSpin() {
    if (spinning) return;
    if (freespins === 0 && balance < bet) return;
    setErrMsg(null);
    setWins([]);
    setHighlightedWin(0);
    setBigWin(null);
    setHype(null);
    setScatterFlash(false);
    haptic('spin');
    setSpinning(true);

    let result;
    try {
      result = await remoteSpin(bet);
    } catch (e) {
      // Server refused — abort the spin gracefully.
      setSpinning(false);
      setAutoSpin(0);
      haptic('error');
      setErrMsg(String(e.message || e).replace(/^API \/spin \d+: /, '') || 'не удалось');
      return;
    }

    // Server is the source of truth. It gives us:
    //   grid[3][5]            — final symbol ids
    //   wins[]                — { lineIdx, line, symbolId, count, payout,
    //                             positions, scatter?, freespins? }
    //   totalPayout, balance, freespinsRemaining, isBigWin, isFreeSpin
    setGrid(result.grid);
    setFreespins(result.freespinsRemaining || 0);

    const lastReelStop = spinBase + perReel * 4;
    spinTimerRef.current = window.setTimeout(() => {
      setSpinning(false);
      haptic('reel-stop');
      setWins(result.wins || []);

      const scatterWin = (result.wins || []).find((w) => w.scatter);
      if (scatterWin) {
        setScatterFlash(true);
        haptic('scatter');
      }
      if (result.isBigWin) {
        setBigWin({
          amount: result.totalPayout,
          win: (result.wins || []).find((w) => !w.scatter),
        });
        setShake(true);
        haptic('big-win');
        window.setTimeout(() => setShake(false), 700);
      } else if (result.totalPayout > 0) {
        haptic('win');
        setHype(HYPE_LINES[Math.floor(Math.random() * HYPE_LINES.length)]);
      } else {
        haptic('lose');
        setHype(SHRUG_LINES[Math.floor(Math.random() * SHRUG_LINES.length)]);
      }
    }, lastReelStop + 50);
  }

  // Auto-spin engine
  React.useEffect(() => {
    if (autoSpin > 0 && !spinning && !bigWin && !errMsg) {
      autoTimerRef.current = window.setTimeout(() => {
        if (balance >= bet || freespins > 0) {
          doSpin();
          setAutoSpin((n) => n - 1);
        } else {
          setAutoSpin(0);
        }
      }, 350);
      return () => window.clearTimeout(autoTimerRef.current);
    }
  }, [autoSpin, spinning, bigWin, errMsg]);

  // Cycle highlighted win every 1.2s
  React.useEffect(() => {
    if (!wins.length || spinning) return;
    if (wins.length === 1) { setHighlightedWin(0); return; }
    cycleTimerRef.current = window.setInterval(() => {
      setHighlightedWin((i) => (i + 1) % wins.length);
    }, 1200);
    return () => window.clearInterval(cycleTimerRef.current);
  }, [wins, spinning]);

  // Big-win banner timeout
  React.useEffect(() => {
    if (!bigWin) return;
    const t = window.setTimeout(() => setBigWin(null), 2600);
    return () => window.clearTimeout(t);
  }, [bigWin]);

  React.useEffect(() => () => {
    window.clearTimeout(spinTimerRef.current);
    window.clearInterval(cycleTimerRef.current);
    window.clearTimeout(autoTimerRef.current);
  }, []);

  const activeWin = wins[highlightedWin];
  const highlights = activeWin ? activeWin.positions : null;

  const skinClass = `slot slot-${skin.id} ${shake ? 'shake' : ''}`;

  return (
    <div className={skinClass} data-screen-label={skin.label}>
      <div className="slot-bg" aria-hidden="true">{skin.background}</div>
      {skin.speedLines && <div className="speed-lines" aria-hidden="true" />}

      <div className="slot-inner">
        <SlotHeader skin={skin} balance={balance} freespins={freespins} user={user} />

        <ReelFrame skin={skin}>
          <div className="reels" style={{ gap }}>
            {[0, 1, 2, 3, 4].map((c) => {
              const finalCol = [grid[0][c], grid[1][c], grid[2][c]];
              return (
                <Reel key={c} reelIdx={c} finalCol={finalCol}
                      spinning={spinning} delay={c * perReel}
                      spinDuration={spinBase} tileSize={tileSize} gap={gap}
                      highlights={highlights
                        ? highlights.filter(([, cc]) => cc === c).map(([rr]) => [rr, c])
                        : null} />
              );
            })}
            {activeWin && !activeWin.scatter && (
              <PaylineOverlay line={activeWin.line} tileSize={tileSize} gap={gap}
                              color={SYMBOLS[activeWin.symbolId].tint} />
            )}
          </div>
        </ReelFrame>

        <WinTicker wins={wins} active={highlightedWin} spinning={spinning}
                   hype={hype} skin={skin} totalWin={wins.reduce((s,w)=>s+(w.payout||0),0)} />

        {errMsg && (
          <div className="spin-error">⚠️ {errMsg}</div>
        )}

        <PayTable />

        <BetControls bet={bet} setBet={setBet} balance={balance}
                     canSpin={!spinning && (balance >= bet || freespins > 0)}
                     spinning={spinning} freespins={freespins}
                     onSpin={doSpin}
                     auto={autoSpin} setAuto={setAutoSpin}
                     skin={skin} />

        <HavdBanner />
      </div>

      {bigWin && absurdLevel !== 'low' && <BigWinFX bigWin={bigWin} skin={skin} />}
      {scatterFlash && !bigWin && <ScatterFX />}
    </div>
  );
}

// ─── HAVD telegram banner ──────────────────────────────────────────
function HavdBanner() {
  return (
    <a className="havd-banner"
       href="https://t.me/havdaily" target="_blank" rel="noopener noreferrer">
      <span className="havd-stamp" aria-hidden="true">
        <img src="havd-avatar.jpg" alt="" draggable={false} />
      </span>
      <span className="havd-text">
        <span className="havd-brand">HAVD.</span>
        <span className="havd-cta">Подписать контракт</span>
      </span>
      <span className="havd-arrow" aria-hidden="true">→</span>
      <span className="havd-shimmer" aria-hidden="true" />
    </a>
  );
}

// ─── header: user greeting + title + balance ──────────────────────
function SlotHeader({ skin, balance, freespins, user }) {
  const display = user
    ? (user.first_name || user.username || `id${user.id}`).slice(0, 18)
    : null;
  return (
    <div className="slot-header">
      {display ? (
        <div className="user-row">
          <span className="user-greet">こんにちは,</span>
          <span className="user-name">{display}</span>
          <span className="user-suffix">-сан</span>
        </div>
      ) : null}
      <h1 className="slot-title" style={{ fontFamily: skin.titleFont }}>
        {skin.title}
        <span className="title-jp">{skin.titleJp}</span>
      </h1>
      <div className="header-row">
        <div className="balance">
          <span className="balance-label">баланс</span>
          <span className="balance-val">{balance.toLocaleString('ru-RU')}</span>
          <span className="balance-coin">¥</span>
        </div>
        {freespins > 0 && (
          <div className="freespins-pill">
            <span>✦</span> {freespins} ФРИСПИНОВ
          </div>
        )}
      </div>
    </div>
  );
}

// ─── frame ──────────────────────────────────────────────────────────
function ReelFrame({ skin, children }) {
  return (
    <div className="reel-frame" style={{ '--frame-color': skin.frameColor }}>
      <div className="reel-frame-inner">
        {children}
        <span className="corner tl" /><span className="corner tr" />
        <span className="corner bl" /><span className="corner br" />
      </div>
    </div>
  );
}

// ─── payline overlay (SVG over reels) ───────────────────────────────
function PaylineOverlay({ line, tileSize, gap, color }) {
  const w = tileSize * 5 + gap * 4;
  const h = tileSize * 3 + gap * 2;
  const xOf = (c) => c * (tileSize + gap) + tileSize / 2;
  const yOf = (r) => r * (tileSize + gap) + tileSize / 2;
  const d = line.map((r, c) => `${c === 0 ? 'M' : 'L'}${xOf(c)} ${yOf(r)}`).join(' ');
  return (
    <svg className="payline-svg" viewBox={`0 0 ${w} ${h}`}
         style={{ position: 'absolute', inset: 0, width: '100%', height: '100%',
                  pointerEvents: 'none' }}>
      <path d={d} stroke="#111" strokeWidth="7" fill="none"
            strokeLinecap="round" strokeLinejoin="round" />
      <path d={d} stroke={color} strokeWidth="4" fill="none"
            strokeLinecap="round" strokeLinejoin="round"
            style={{ filter: 'drop-shadow(0 0 4px rgba(255,255,255,.7))' }} />
    </svg>
  );
}

window.SlotMachine = SlotMachine;
window.HYPE_LINES = HYPE_LINES;
window.SHRUG_LINES = SHRUG_LINES;
