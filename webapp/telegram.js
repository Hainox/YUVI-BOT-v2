// ─────────────────────────────────────────────────────────────────────
// Telegram WebApp SDK shim
// Outside Telegram (local dev) every method is a safe no-op.
// ─────────────────────────────────────────────────────────────────────

(function () {
  const raw = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  const inTelegram = !!(raw && raw.initData);

  const user = inTelegram && raw.initDataUnsafe && raw.initDataUnsafe.user
    ? raw.initDataUnsafe.user
    : null;

  function haptic(kind) {
    if (!inTelegram || !raw.HapticFeedback) return;
    try {
      switch (kind) {
        case 'tap':        raw.HapticFeedback.impactOccurred('light'); break;
        case 'spin':       raw.HapticFeedback.impactOccurred('light'); break;
        case 'reel-stop':  raw.HapticFeedback.impactOccurred('rigid'); break;
        case 'win':        raw.HapticFeedback.notificationOccurred('success'); break;
        case 'big-win':    raw.HapticFeedback.notificationOccurred('success'); break;
        case 'lose':       raw.HapticFeedback.impactOccurred('soft'); break;
        case 'scatter':    raw.HapticFeedback.notificationOccurred('warning'); break;
        case 'error':      raw.HapticFeedback.notificationOccurred('error'); break;
      }
    } catch (e) {}
  }

  function setHeader(color) {
    if (!inTelegram) return;
    try {
      raw.setHeaderColor(color);
      raw.setBackgroundColor(color);
    } catch (e) {}
  }

  // BackButton — wire to a single onBack callback set by the router.
  // We register the handler once and route presses through the closure.
  let backHandler = null;
  function showBack(onBack) {
    backHandler = onBack;
    if (!inTelegram || !raw.BackButton) return;
    try { raw.BackButton.show(); } catch (e) {}
  }
  function hideBack() {
    backHandler = null;
    if (!inTelegram || !raw.BackButton) return;
    try { raw.BackButton.hide(); } catch (e) {}
  }
  if (inTelegram && raw.BackButton) {
    try {
      raw.BackButton.onClick(() => { if (backHandler) backHandler(); });
    } catch (e) {}
  }

  function init() {
    if (!inTelegram) return;
    try {
      raw.ready();
      raw.expand();
      if (raw.disableVerticalSwipes) raw.disableVerticalSwipes();
    } catch (e) {}
  }

  window.tgApp = {
    inTelegram,
    user,
    initData: inTelegram ? raw.initData : '',
    haptic,
    setHeader,
    showBack,
    hideBack,
    init,
    raw,
  };
})();
