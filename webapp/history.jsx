// ─────────────────────────────────────────────────────────────────────
// HistoryScreen — last 50 events for the current user.
// Same vocabulary as the reference: each row shows actor + kind +
// amount (red for spend, green for win) + relative time.
// ─────────────────────────────────────────────────────────────────────

function HistoryScreen() {
  const [items, setItems] = React.useState(null);
  const [error, setError] = React.useState(null);

  React.useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await window.slotApi.history(50);
        if (!alive) return;
        setItems(res.items || []);
      } catch (e) {
        if (!alive) return;
        setError(String(e.message || e));
      }
    })();
    return () => { alive = false; };
  }, []);

  if (error) return <div className="screen-error"><p>{error}</p></div>;
  if (items == null) return <div className="screen-loading"><span>загрузка…</span></div>;

  if (items.length === 0) {
    return (
      <div className="subscreen">
        <h1 className="subscreen-title">История</h1>
        <p className="subscreen-sub">лента всех операций по слоту</p>
        <div className="empty-state">
          <div className="empty-jp">からっぽ</div>
          <div className="empty-en">пусто. Крути слот — появится история.</div>
        </div>
      </div>
    );
  }

  return (
    <div className="subscreen">
      <h1 className="subscreen-title">История</h1>
      <p className="subscreen-sub">лента всех операций по слоту — для прозрачности</p>

      <div className="hist-list">
        {items.map((it) => (
          <div key={it.id} className="hist-row">
            <div className="hist-meta">
              <span className="hist-actor">@{it.username || it.first_name || `id${it.user_id}`}</span>
              <span className="hist-kind">{kindLabel(it)}</span>
            </div>
            <div className={`hist-amount ${it.amount >= 0 ? 'pos' : 'neg'}`}>
              {it.amount >= 0 ? `+${it.amount}` : it.amount}
            </div>
            <div className="hist-time">{formatTime(it.created_at)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function kindLabel(it) {
  if (it.kind === 'bet')      return 'Slots: ставка';
  if (it.kind === 'win')      return 'Slots: выигрыш';
  if (it.kind === 'big_win')  return 'Slots: BIG WIN';
  if (it.kind === 'free_win') return 'Slots: фриспин-выигрыш';
  if (it.kind === 'seed')     return 'Slots: пополнение';
  return `Slots: ${it.kind}`;
}

function formatTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) {
    return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }
  return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

window.HistoryScreen = HistoryScreen;
