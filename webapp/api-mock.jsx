// ─────────────────────────────────────────────────────────────────────
// Dev API mock — used when no real backend is reachable.
// Activated by appending `?mock=1` to the URL, OR auto-fallback when
// the real /api/me request fails. This lets the design preview work
// in isolation. In Telegram production this code path never runs.
// ─────────────────────────────────────────────────────────────────────

(function () {
  const u = new URL(window.location.href);
  const forceMock = u.searchParams.get('mock') === '1';

  // Same seedable RNG style as the server slot engine — but client-side
  // for the dev mock only. Server is authoritative in production.
  const SYMS = window.SLOT_DATA;
  const evaluate = SYMS.evaluateGrid;
  const randGrid = SYMS.randomGrid;

  // In-memory state for the mock
  const state = {
    balance: 2000,
    freespinsRemaining: 0,
    bank: 262793,
    history: [],
    nextId: 1,
  };

  function record(kind, amount) {
    state.history.unshift({
      id: state.nextId++,
      user_id: 99001,
      username: 'mock_user',
      first_name: 'Mock',
      kind,
      amount,
      created_at: new Date().toISOString(),
    });
    if (state.history.length > 200) state.history.length = 200;
  }

  const mockApi = {
    base: 'MOCK',
    me: async () => ({
      user: { id: 99001, username: 'mock_user', first_name: 'Mock' },
      balance: state.balance,
      bank: state.bank,
      freespinsRemaining: state.freespinsRemaining,
    }),
    balance: async () => ({ balance: state.balance }),
    spin: async ({ bet } = {}) => {
      bet = Number(bet) || 100;
      if (state.freespinsRemaining === 0 && state.balance < bet) {
        throw new Error('недостаточно средств');
      }
      const isFree = state.freespinsRemaining > 0;
      let betPlaced = 0;
      if (isFree) {
        state.freespinsRemaining--;
      } else {
        state.balance -= bet;
        betPlaced = bet;
        record('bet', -bet);
      }
      const grid = randGrid();
      const betPerLine = Math.max(1, Math.floor(bet / 10));
      const wins = evaluate(grid, betPerLine);
      const totalPayout = wins.reduce((s, w) => s + (w.payout || 0), 0);
      const scatter = wins.find((w) => w.scatter);
      if (totalPayout > 0) {
        state.balance += totalPayout;
        record(totalPayout >= bet * 3 ? 'big_win' : (isFree ? 'free_win' : 'win'), totalPayout);
      }
      if (scatter) state.freespinsRemaining += scatter.freespins;
      return {
        grid,
        wins,
        totalPayout,
        betPlaced,
        balance: state.balance,
        freespinsRemaining: state.freespinsRemaining,
        isBigWin: totalPayout >= bet * 3,
        isFreeSpin: isFree,
      };
    },
    history: async ({ limit = 30 } = {}) => ({ items: state.history.slice(0, limit) }),
    stats: async () => ({
      balance: [
        { user_id: 99001, username: 'mock_user', value: state.balance },
        { user_id: 1001, username: 'atonyan', value: 20710 },
        { user_id: 1002, username: 'konnko', value: 9999 },
        { user_id: 1003, username: 'Thesauros', value: 2970 },
      ],
      wins: [
        { user_id: 1002, username: 'konnko', value: 18500 },
        { user_id: 99001, username: 'mock_user', value: 5400 },
      ],
      bigwins: [
        { user_id: 1001, username: 'atonyan', value: 12 },
        { user_id: 99001, username: 'mock_user', value: 3 },
      ],
    }),
    seedDev: async () => ({ balance: state.balance }),
  };

  async function probeReal() {
    try {
      const r = await fetch((window.slotApi || {}).base + '/me', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Init-Data': '' },
      });
      return r.ok;
    } catch { return false; }
  }

  // Decide mode AFTER api.js sets window.slotApi.
  if (forceMock) {
    window.slotApi = mockApi;
    console.info('[slot] mock API active (forced via ?mock=1)');
  } else {
    // Auto-fallback: wrap real api, on first failure swap to mock.
    const real = window.slotApi;
    let swapped = false;
    const wrap = (fn) => async (...args) => {
      if (swapped) return mockApi[fn](...args);
      try {
        return await real[fn](...args);
      } catch (e) {
        if (!swapped) {
          console.warn('[slot] real API failed, falling back to mock:', e.message);
          swapped = true;
        }
        return mockApi[fn](...args);
      }
    };
    window.slotApi = {
      base: real.base,
      me:      wrap('me'),
      balance: wrap('balance'),
      spin:    wrap('spin'),
      history: wrap('history'),
      stats:   wrap('stats'),
      seedDev: wrap('seedDev'),
    };
  }
})();
