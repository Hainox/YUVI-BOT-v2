// ─────────────────────────────────────────────────────────────────────
// SlotScreen — wraps SlotMachine, talks to the server.
// SlotMachine doesn't know about the network; this component does.
// ─────────────────────────────────────────────────────────────────────

const { SlotMachine, getSkin } = window;

function SlotScreen({ user, balance, onBalanceChange }) {
  // Skin: Daioh sky with school stripes. Fixed for now — themed bg is
  // a tweak we can re-expose later, not part of v1 scope.
  const skin = React.useMemo(() => getSkin('daioh', 'school'), []);

  // Async spin: post to server, return whatever it gives us. The slot
  // engine uses this as its source of truth — grid, wins, new balance.
  const remoteSpin = React.useCallback(async (bet) => {
    const res = await window.slotApi.spin(bet);
    if (typeof res.balance === 'number') onBalanceChange(res.balance);
    return res;
  }, [onBalanceChange]);

  return (
    <SlotMachine
      skin={skin}
      spinSpeed="normal"
      absurdLevel="full"
      user={user}
      haptic={window.tgApp.haptic}
      balance={balance}
      remoteSpin={remoteSpin}
    />
  );
}

window.SlotScreen = SlotScreen;
