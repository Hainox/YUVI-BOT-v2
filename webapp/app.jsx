// ─────────────────────────────────────────────────────────────────────
// App shell — screen router + balance context.
//
// Five screens, navigated by setting `screen` state. The Telegram
// BackButton is wired to return to the menu from any sub-screen.
// All screens read balance from the shared API; <SlotScreen /> updates
// it through window.slotApi which is the same source of truth.
// ─────────────────────────────────────────────────────────────────────

const { Menu, SlotScreen, HistoryScreen, StatsScreen, RulesScreen } = window;

window.tgApp.init();
window.tgApp.setHeader('#0d0a18');

function App() {
  const [screen, setScreen] = React.useState('menu');
  const [balance, setBalance] = React.useState(null);
  const [bank, setBank] = React.useState(null);
  const [user, setUser] = React.useState(window.tgApp.user);
  const [error, setError] = React.useState(null);

  // Initial /me — fetches balance, bank, signed-in user
  React.useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const me = await window.slotApi.me();
        if (!alive) return;
        setBalance(me.balance);
        setBank(me.bank);
        if (me.user) setUser((u) => u || me.user);
      } catch (e) {
        if (!alive) return;
        setError(String(e.message || e));
      }
    })();
    return () => { alive = false; };
  }, []);

  // BackButton: visible everywhere except menu, returns to menu.
  React.useEffect(() => {
    if (screen === 'menu') {
      window.tgApp.hideBack();
    } else {
      window.tgApp.showBack(() => {
        window.tgApp.haptic('tap');
        setScreen('menu');
      });
    }
  }, [screen]);

  function navigate(next) {
    window.tgApp.haptic('tap');
    setScreen(next);
  }

  // Called by SlotScreen after each spin — keeps menu balance fresh.
  function applyBalance(newBalance) {
    if (typeof newBalance === 'number') setBalance(newBalance);
  }

  if (error) {
    return (
      <div className="screen-error">
        <h2>Connection error</h2>
        <p className="err-msg">{error}</p>
        <p className="err-hint">
          Сервер слота недоступен. Если ты открыл это в браузере вне Telegram —
          так и должно быть; запусти через бота командой <code>/casino</code>.
        </p>
        <button type="button" onClick={() => window.location.reload()}>Повторить</button>
      </div>
    );
  }

  if (balance == null) {
    return <div className="screen-loading"><span>загрузка…</span></div>;
  }

  return (
    <div className="webapp-root" data-screen={screen}>
      {screen === 'menu' && (
        <Menu user={user} balance={balance} bank={bank} onNavigate={navigate} />
      )}
      {screen === 'slot' && (
        <SlotScreen user={user} balance={balance} onBalanceChange={applyBalance} />
      )}
      {screen === 'history' && <HistoryScreen />}
      {screen === 'stats'   && <StatsScreen me={user} />}
      {screen === 'rules'   && <RulesScreen />}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
