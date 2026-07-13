// ─────────────────────────────────────────────────────────────────────
// Symbol catalogue + paylines for the Azumanga slot.
// 8 chibi images, each given an absurd persona + payout table.
// ─────────────────────────────────────────────────────────────────────

const SYMBOLS = {
  muscle: {
    id: 'muscle',
    src: 'symbols/muscle.jpg',
    name: 'КАЧОК-ОСАКА',
    jp: 'ムキムキ',
    role: 'wild',
    weight: 2,
    pay: { 3: 50, 4: 200, 5: 1000 },
    tint: '#ffd84a',
  },
  keffiyeh: {
    id: 'keffiyeh',
    src: 'symbols/keffiyeh.jpg',
    name: 'ШЕЙХ-АКА',
    jp: 'スキャッター',
    role: 'scatter',
    weight: 2,
    pay: { 3: 0, 4: 0, 5: 0 }, // pays via freespins
    tint: '#ff5b8d',
  },
  gasp: {
    id: 'gasp',
    src: 'symbols/gasp.jpg',
    name: 'НИХУЯ',
    jp: 'ガーン',
    role: 'high',
    weight: 4,
    pay: { 3: 10, 4: 27, 5: 72 },
    tint: '#7be6ff',
  },
  'lightning-eyes': {
    id: 'lightning-eyes',
    src: 'symbols/lightning-eyes.jpg',
    name: 'Osaka KYS',
    jp: 'ゴッド',
    role: 'high',
    weight: 5,
    pay: { 3: 9, 4: 20, 5: 50 },
    tint: '#c4a8ff',
  },
  dog: {
    id: 'dog',
    src: 'symbols/dog.jpg',
    name: 'Bruh….',
    jp: 'わんわん',
    role: 'mid',
    weight: 7,
    pay: { 3: 5, 4: 11, 5: 24 },
    tint: '#ffb1c8',
  },
  'osaka-stand': {
    id: 'osaka-stand',
    src: 'symbols/osaka-stand.jpg',
    name: 'Гроши заработал',
    jp: 'おさか',
    role: 'low',
    weight: 10,
    pay: { 3: 4, 4: 10, 5: 20 },
    tint: '#ffe27a',
  },
  'bath-chibi': {
    id: 'bath-chibi',
    src: 'symbols/bath-chibi.jpg',
    name: 'Да-да, выиграл хуйню',
    jp: 'おふろ',
    role: 'low',
    weight: 9,
    pay: { 3: 3, 4: 8, 5: 15 },
    tint: '#b8e7ff',
  },
  sakaki: {
    id: 'sakaki',
    src: 'symbols/sakaki.jpg',
    name: 'WTF OSAKA NIG……',
    jp: 'にっこり',
    role: 'low',
    weight: 8,
    pay: { 3: 3, 4: 6, 5: 12 },
    tint: '#d6c4a3',
  },
};

const SYMBOL_LIST = Object.values(SYMBOLS);

// Per-reel strip: weighted pool, lightly different per column so each barrel
// has its own rhythm and the eye doesn't see the same vertical pattern.
function buildReelStrip(seed) {
  const strip = [];
  SYMBOL_LIST.forEach((s) => {
    let w = s.weight;
    // mild per-reel variance — first/last reels see scatter+wild a bit more
    if (s.role === 'scatter' && (seed === 0 || seed === 4)) w += 1;
    if (s.role === 'wild' && seed === 2) w += 1;
    for (let i = 0; i < w; i++) strip.push(s.id);
  });
  // deterministic shuffle so each reel has its own arrangement
  for (let i = strip.length - 1; i > 0; i--) {
    const j = (i * 9301 + (seed + 1) * 49297) % (i + 1);
    [strip[i], strip[j]] = [strip[j], strip[i]];
  }
  return strip;
}

const REEL_STRIPS = [0, 1, 2, 3, 4].map(buildReelStrip);

function pickFromStrip(reelIdx) {
  const strip = REEL_STRIPS[reelIdx];
  return strip[Math.floor(Math.random() * strip.length)];
}

// 10 standard paylines on a 3×5 grid. Each is row index per column.
const PAYLINES = [
  [1, 1, 1, 1, 1], // 1 · middle
  [0, 0, 0, 0, 0], // 2 · top
  [2, 2, 2, 2, 2], // 3 · bottom
  [0, 1, 2, 1, 0], // 4 · V
  [2, 1, 0, 1, 2], // 5 · Λ
  [0, 0, 1, 2, 2], // 6 · descending
  [2, 2, 1, 0, 0], // 7 · ascending
  [1, 0, 0, 0, 1], // 8 · top U
  [1, 2, 2, 2, 1], // 9 · bottom U
  [0, 1, 0, 1, 0], // 10 · zigzag
];

// Detect winning lines on a 3×5 grid of symbol ids.
// Returns array of { line, symbolId, count, payout, positions }.
function evaluateGrid(grid, betPerLine) {
  const wins = [];
  PAYLINES.forEach((line, lineIdx) => {
    // collect symbols on this line
    const onLine = line.map((row, col) => grid[row][col]);
    // find the first non-wild leftmost — wilds count toward whatever
    // we're matching. If all leading are wild, the wild counts as itself.
    let target = null;
    for (let i = 0; i < onLine.length; i++) {
      if (onLine[i] !== 'muscle') { target = onLine[i]; break; }
    }
    if (target == null) target = 'muscle';
    if (target === 'keffiyeh') return; // scatter doesn't pay on lines

    // count from left
    let count = 0;
    for (let i = 0; i < onLine.length; i++) {
      if (onLine[i] === target || onLine[i] === 'muscle') count++;
      else break;
    }
    if (count < 3) return;
    const sym = SYMBOLS[target];
    const payout = (sym.pay[count] || 0) * betPerLine;
    if (payout <= 0) return;
    wins.push({
      lineIdx, line, symbolId: target, count, payout,
      positions: line.slice(0, count).map((r, c) => [r, c]),
    });
  });

  // scatter — count anywhere on grid, ≥3 awards freespins
  let scatterCount = 0;
  const scatterPos = [];
  for (let r = 0; r < 3; r++) for (let c = 0; c < 5; c++) {
    if (grid[r][c] === 'keffiyeh') { scatterCount++; scatterPos.push([r, c]); }
  }
  let freespins = 0;
  if (scatterCount >= 3) {
    freespins = scatterCount === 3 ? 4 : scatterCount === 4 ? 6 : 7;
    wins.push({
      lineIdx: -1, line: null, symbolId: 'keffiyeh', count: scatterCount,
      payout: 0, freespins, positions: scatterPos, scatter: true,
    });
  }
  return wins;
}

// Generate a fresh 3×5 grid by sampling each reel column independently.
function randomGrid() {
  const grid = [[], [], []];
  for (let c = 0; c < 5; c++) {
    for (let r = 0; r < 3; r++) grid[r].push(pickFromStrip(c));
  }
  return grid;
}

window.SLOT_DATA = {
  SYMBOLS, SYMBOL_LIST, REEL_STRIPS, PAYLINES,
  evaluateGrid, randomGrid, pickFromStrip,
};
