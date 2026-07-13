// ─────────────────────────────────────────────────────────────────────
// Menu screen — bot's casino home.
// Layout follows the reference (dark theme, balance card at top, 2-col
// grid of feature cards) but keeps the Azumanga accent for personality.
// Only cards for features that actually exist are clickable; the rest
// show as locked placeholders so the menu still feels populated.
// ─────────────────────────────────────────────────────────────────────

function Menu({ user, balance, bank, onNavigate }) {
  const name = user
    ? `@${user.username || user.first_name || `id${user.id}`}`
    : 'гость';

  return (
    <div className="menu">
      <div className="menu-head">
        <h1 className="menu-title">Yuvi скам</h1>
        <div className="menu-sub">казино · ставки · абсурд</div>
      </div>

      <BalanceCard name={name} balance={balance} bank={bank} />

      <div className="menu-grid">
        <FeatureCard
          title="Слот"
          desc="3×5, 10 линий, до 1000×"
          accent="pink"
          onClick={() => onNavigate('slot')}
        />
        <FeatureCard
          title="История"
          desc="мои ставки и выигрыши"
          accent="cyan"
          onClick={() => onNavigate('history')}
        />
        <FeatureCard
          title="Статистика"
          desc="топ игроков чата"
          accent="yellow"
          onClick={() => onNavigate('stats')}
        />
        <FeatureCard
          title="Правила"
          desc="как это работает"
          accent="plain"
          onClick={() => onNavigate('rules')}
        />
        <FeatureCard
          title="Лотерея /yuvi"
          desc="ежедневный розыгрыш"
          locked="доступна в чате бота"
        />
        <FeatureCard
          title="Скоро"
          desc="дайс · рулетка · blackjack"
          locked="в разработке"
        />
      </div>

      <a className="havd-banner havd-banner-menu"
         href="https://t.me/havdaily" target="_blank" rel="noopener noreferrer">
        <span className="havd-stamp">
          <img src="havd-avatar.jpg" alt="" draggable={false} />
        </span>
        <span className="havd-text">
          <span className="havd-brand">HAVD.</span>
          <span className="havd-cta">Подписать контракт</span>
        </span>
        <span className="havd-arrow">→</span>
        <span className="havd-shimmer" aria-hidden="true" />
      </a>
    </div>
  );
}

function BalanceCard({ name, balance, bank }) {
  return (
    <div className="balance-card">
      <div className="bc-handle">{name}</div>
      <div className="bc-amount">
        <span className="bc-val">{balance.toLocaleString('ru-RU')}</span>
        <span className="bc-unit">¥ юви</span>
      </div>
      {bank != null && (
        <div className="bc-bank">Банк чата: <strong>{bank.toLocaleString('ru-RU')}</strong></div>
      )}
    </div>
  );
}

function FeatureCard({ title, desc, accent, locked, onClick }) {
  const cls = `feature-card ${accent ? `fc-${accent}` : ''} ${locked ? 'fc-locked' : ''}`;
  return (
    <button type="button" className={cls} onClick={locked ? undefined : onClick} disabled={!!locked}>
      <span className="fc-title">{title}</span>
      <span className="fc-desc">{locked || desc}</span>
      {!locked && <span className="fc-chev" aria-hidden="true">›</span>}
    </button>
  );
}

window.Menu = Menu;
