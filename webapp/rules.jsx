// ─────────────────────────────────────────────────────────────────────
// RulesScreen — how the slot works, written like a manga panel.
// ─────────────────────────────────────────────────────────────────────

function RulesScreen() {
  return (
    <div className="subscreen">
      <h1 className="subscreen-title">Правила</h1>
      <p className="subscreen-sub">как работает Yuvi скам</p>

      <RuleBlock title="Старт">
        <p>Новый игрок получает <strong>2000 ¥</strong>. Этот баланс — общий
        для всех чатов, привязан к твоему Telegram-аккаунту.</p>
      </RuleBlock>

      <RuleBlock title="Слот">
        <ul>
          <li><strong>3×5, 10 линий</strong> — выплата при 3+ одинаковых символов слева.</li>
          <li><strong>Ставка</strong> делится между 10 линий: bet/10 на линию.</li>
          <li><strong>Wild</strong> (КАЧОК-ОСАКА) — заменяет любой символ кроме скаттера.</li>
          <li><strong>Scatter</strong> (ШЕЙХ-АКА) — 3+ в любом месте → 4-7 фриспинов.</li>
          <li>Big Win — выигрыш ≥ 3× ставки. Молнии, тряска, чибик-крик.</li>
        </ul>
      </RuleBlock>

      <RuleBlock title="Выплаты (×bet/10)">
        <div className="pay-grid-rules">
          <div><span className="pg-name">КАЧОК-ОСАКА</span><span>50 / 200 / 1000</span></div>
          <div><span className="pg-name">НИХУЯ</span><span>10 / 27 / 72</span></div>
          <div><span className="pg-name">Osaka KYS</span><span>9 / 20 / 50</span></div>
          <div><span className="pg-name">Bruh….</span><span>5 / 11 / 24</span></div>
          <div><span className="pg-name">Гроши заработал</span><span>4 / 10 / 20</span></div>
          <div><span className="pg-name">Да-да, выиграл хуйню</span><span>3 / 8 / 15</span></div>
          <div><span className="pg-name">WTF OSAKA NIG……</span><span>3 / 6 / 12</span></div>
        </div>
      </RuleBlock>

      <RuleBlock title="Важно">
        <ul>
          <li>Минимальная ставка: <strong>10 ¥</strong>.</li>
          <li>Если ушёл в ноль — попроси в чате <code>/yuvi</code>, кто-нибудь пожалеет.</li>
          <li>Все спины и выигрыши пишутся в журнал — прозрачно, как в банке.</li>
        </ul>
      </RuleBlock>
    </div>
  );
}

function RuleBlock({ title, children }) {
  return (
    <div className="rule-block">
      <h2 className="rule-title">{title}</h2>
      <div className="rule-body">{children}</div>
    </div>
  );
}

window.RulesScreen = RulesScreen;
