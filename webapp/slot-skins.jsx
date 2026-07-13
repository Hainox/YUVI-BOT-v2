// ─────────────────────────────────────────────────────────────────────
// Three visual variants of the slot. Each is a `skin` object that
// SlotMachine consumes, plus the wrapper styling lives in slot.css.
// ─────────────────────────────────────────────────────────────────────

// Halftone dot pattern — drawn with SVG and tiled. Used in Manga skin.
function HalftoneBg({ color = '#000', size = 6, opacity = 0.18 }) {
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' width='${size}' height='${size}'>
    <circle cx='${size/2}' cy='${size/2}' r='${size*0.32}' fill='${color}' opacity='${opacity}'/>
  </svg>`;
  const url = `url("data:image/svg+xml;utf8,${encodeURIComponent(svg)}")`;
  return <div className="halftone-bg" style={{ backgroundImage: url, backgroundSize: `${size}px ${size}px` }} />;
}

// Cloud field — fluffy Azumanga title-card sky
function CloudBg() {
  return (
    <div className="cloud-bg">
      <svg viewBox="0 0 480 900" preserveAspectRatio="xMidYMid slice"
           xmlns="http://www.w3.org/2000/svg">
        <defs>
          <radialGradient id="sky" cx="50%" cy="20%" r="100%">
            <stop offset="0%" stopColor="#d8f0ff" />
            <stop offset="100%" stopColor="#9cd3ff" />
          </radialGradient>
        </defs>
        <rect width="480" height="900" fill="url(#sky)" />
        {/* fluffy clouds — stacks of overlapping ellipses */}
        <g fill="#fff" opacity="0.95">
          <Cloud x={60} y={120} s={1.0} />
          <Cloud x={340} y={210} s={0.8} />
          <Cloud x={120} y={420} s={1.2} />
          <Cloud x={380} y={560} s={0.9} />
          <Cloud x={40} y={720} s={1.0} />
          <Cloud x={300} y={820} s={1.1} />
        </g>
      </svg>
    </div>
  );
}

function Cloud({ x, y, s }) {
  return (
    <g transform={`translate(${x},${y}) scale(${s})`}>
      <ellipse cx="0" cy="20" rx="60" ry="18" />
      <ellipse cx="-30" cy="10" rx="28" ry="22" />
      <ellipse cx="-5" cy="-5" rx="34" ry="28" />
      <ellipse cx="28" cy="0" rx="30" ry="24" />
      <ellipse cx="50" cy="15" rx="22" ry="18" />
    </g>
  );
}

// Storm sky — dark roiling clouds, flickering lightning in the bg
function StormBg() {
  return (
    <div className="storm-bg">
      <svg viewBox="0 0 480 900" preserveAspectRatio="xMidYMid slice">
        <defs>
          <radialGradient id="storm" cx="50%" cy="35%" r="100%">
            <stop offset="0%" stopColor="#3b2a55" />
            <stop offset="55%" stopColor="#1a1530" />
            <stop offset="100%" stopColor="#08060f" />
          </radialGradient>
        </defs>
        <rect width="480" height="900" fill="url(#storm)" />
        <g fill="#1c1530" opacity="0.85">
          <ellipse cx="100" cy="180" rx="180" ry="60" />
          <ellipse cx="380" cy="240" rx="160" ry="48" />
          <ellipse cx="240" cy="700" rx="220" ry="70" />
          <ellipse cx="60" cy="780" rx="160" ry="50" />
        </g>
      </svg>
      <div className="storm-flash" />
    </div>
  );
}

// ── Skin definitions consumed by <SlotMachine skin={...} /> ──────────

const SKINS = {
  manga: {
    id: 'manga',
    label: '01 · Манга-страница',
    title: 'Yuvi скам',
    titleJp: 'スロット',
    titleFont: '"Bangers", "Anton", system-ui, sans-serif',
    frameColor: '#111',
    background: null, // set per-instance via React below
    speedLines: false,
  },
  daioh: {
    id: 'daioh',
    label: '02 · Небо Даио',
    title: 'Yuvi скам',
    titleJp: 'あずまんが大王',
    titleFont: '"Bangers", "Anton", system-ui, sans-serif',
    frameColor: '#ff5b8d',
    background: null,
    speedLines: false,
  },
  storm: {
    id: 'storm',
    label: '03 · Молниевый хаос',
    title: 'Yuvi скам',
    titleJp: '雷モード',
    titleFont: '"Bangers", "Anton", system-ui, sans-serif',
    frameColor: '#7be6ff',
    background: null,
    speedLines: true,
  },
};

function getSkin(id, bgMode) {
  const base = SKINS[id];
  const bg = (() => {
    if (id === 'manga') {
      return (
        <>
          <div className="manga-paper" />
          <HalftoneBg color="#1a1a1a" size={6} opacity={0.2} />
          {bgMode === 'panels' && <MangaPanels />}
          {bgMode === 'sfx' && <MangaSFX />}
        </>
      );
    }
    if (id === 'daioh') {
      return (
        <>
          <CloudBg />
          {bgMode === 'pink' && <div className="pink-wash" />}
          {bgMode === 'school' && <SchoolStripes />}
        </>
      );
    }
    if (id === 'storm') {
      return (
        <>
          <StormBg />
          {bgMode === 'rain' && <RainDots />}
        </>
      );
    }
    return null;
  })();
  return { ...base, background: bg };
}

// Decorative bg variants toggled via Tweaks
function MangaPanels() {
  return (
    <svg className="manga-panels" viewBox="0 0 480 900" preserveAspectRatio="xMidYMid slice">
      <g fill="none" stroke="#111" strokeWidth="3">
        <path d="M 20 60 L 460 60 L 460 200 L 20 200 Z" />
        <path d="M 20 240 L 220 240 L 220 380 L 20 380 Z" />
        <path d="M 260 240 L 460 240 L 460 380 L 260 380 Z" />
        <path d="M 20 760 L 460 760 L 460 880 L 20 880 Z" />
      </g>
    </svg>
  );
}
function MangaSFX() {
  return (
    <div className="manga-sfx">
      <span style={{ top: '6%', left: '4%', transform: 'rotate(-12deg)' }}>БУХ-БУХ</span>
      <span style={{ top: '14%', right: '6%', transform: 'rotate(8deg)' }}>БАХ!!</span>
      <span style={{ bottom: '24%', left: '5%', transform: 'rotate(-6deg)' }}>ГРОМ</span>
      <span style={{ bottom: '8%', right: '4%', transform: 'rotate(10deg)' }}>ВЖУХ!</span>
    </div>
  );
}
function SchoolStripes() {
  return <div className="school-stripes" />;
}
function RainDots() {
  return <div className="rain-dots" />;
}

window.SKINS = SKINS;
window.getSkin = getSkin;
