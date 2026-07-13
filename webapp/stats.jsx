// ─────────────────────────────────────────────────────────────────────
// StatsScreen — leaderboards. Server returns ranked rows; current
// user is highlighted if they make the cut.
// ─────────────────────────────────────────────────────────────────────

function StatsScreen({ me }) {
  const [tab, setTab] = React.useState('balance');
  const [data, setData] = React.useState(null);
  const [error, setError] = React.useState(null);

  React.useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await window.slotApi.stats();
        if (!alive) return;
        setData(res);
      } catch (e) {
        if (!alive) return;
        setError(String(e.message || e));
      }
    })();
    return () => { alive = false; };
  }, []);

  if (error) return <div className="screen-error"><p>{error}</p></div>;
  if (data == null) return <div className="screen-loading"><span>загрузка…</span></div>;

  const rows = (tab === 'balance' ? data.balance
              : tab === 'wins'    ? data.wins
              : data.bigwins) || [];

  return (
    <div className="subscreen">
      <h1 className="subscreen-title">Статистика</h1>
      <p className="subscreen-sub">кто сильнее всех скамит</p>

      <div className="stats-tabs">
        {[
          { id: 'balance', label: 'Баланс' },
          { id: 'wins',    label: 'Выигрыши' },
          { id: 'bigwins', label: 'Биг-вины' },
        ].map((t) => (
          <button key={t.id} type="button"
                  className={`stats-tab ${tab === t.id ? 'active' : ''}`}
                  onClick={() => { window.tgApp.haptic('tap'); setTab(t.id); }}>
            {t.label}
          </button>
        ))}
      </div>

      {rows.length === 0 ? (
        <div className="empty-state">
          <div className="empty-en">данных пока нет</div>
        </div>
      ) : (
        <div className="stats-list">
          {rows.map((row, i) => {
            const isMe = me && row.user_id === me.id;
            return (
              <div key={row.user_id} className={`stats-row ${isMe ? 'is-me' : ''}`}>
                <span className={`stats-rank rank-${Math.min(i + 1, 4)}`}>{i + 1}</span>
                <span className="stats-name">
                  @{row.username || row.first_name || `id${row.user_id}`}
                  {isMe && <span className="stats-me-tag">ты</span>}
                </span>
                <span className="stats-val">{row.value.toLocaleString('ru-RU')}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

window.StatsScreen = StatsScreen;
