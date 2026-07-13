// ─────────────────────────────────────────────────────────────────────
// Backend API client.
// All requests carry the Telegram initData string in `X-Init-Data` so
// the server can verify the caller is who they claim to be. Outside
// Telegram (local dev), we send a fake header — the server's dev mode
// accepts it; in production it 401s.
// ─────────────────────────────────────────────────────────────────────

(function () {
  // Where the API lives. In production this would be the same origin as
  // the Mini App, mounted under /api. For local dev you can override
  // via ?api=... query param.
  const u = new URL(window.location.href);
  const API_BASE = u.searchParams.get('api') || '/api';

  const initData = (window.tgApp && window.tgApp.initData) || '';

  async function call(path, body) {
    const res = await fetch(API_BASE + path, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Init-Data': initData,
      },
      body: JSON.stringify(body || {}),
    });
    if (!res.ok) {
      let detail = '';
      try { detail = (await res.json()).detail || ''; } catch {}
      throw new Error(`API ${path} ${res.status}: ${detail || res.statusText}`);
    }
    return res.json();
  }

  // Public surface — all endpoints return promises. Caller handles errors.
  window.slotApi = {
    base: API_BASE,
    me:        ()        => call('/me'),
    balance:   ()        => call('/balance'),
    spin:      (bet)     => call('/spin', { bet }),
    history:   (limit=30)=> call('/history', { limit }),
    stats:     ()        => call('/stats'),
    seedDev:   (amount)  => call('/dev/seed', { amount }),  // dev only
  };
})();
