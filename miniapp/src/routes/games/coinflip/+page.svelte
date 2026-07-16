<script lang="ts">
	// Coinflip — first playable game (CASINO-01 vertical slice). Two large
	// tappable chip-style heads/tails toggles (04-UI-SPEC.md §Component
	// Inventory: "chip/chip-on pattern"), BET_CHIPS amount picker reused from
	// the slot screen's bet-controls, primary CTA copy locked verbatim
	// ("ПОДКИНУТЬ МОНЕТУ", 04-UI-SPEC.md Copywriting Contract).
	//
	// Server is the sole source of truth for the outcome (D-03/T-04.1-01) —
	// this screen only renders whatever POST /games/coinflip returns. Balance
	// updates arrive via lib/api.ts's balance-sniffing (instant, this tab) AND
	// the SSE stream seeded in +layout.svelte (other tabs/actions) — no local
	// balance mutation here.
	import { apiFetch, ApiError } from '$lib/api';
	import { haptic } from '$lib/tg';

	const BET_CHIPS = [10, 50, 100, 500, 1000];

	type CoinflipResult = {
		game: string;
		bet: number;
		payout: number;
		outcome: { result: 'heads' | 'tails'; won: boolean };
		// D-06: chat_bank couldn't cover the full bet*1.98 payout, so the win
		// was capped (in the worst case down to exactly `bet`, i.e. balance
		// doesn't change even though the round was won) — see api/routes/games.py.
		bank_capped?: boolean;
	};

	let bet = $state(BET_CHIPS[0]);
	let choice = $state<'heads' | 'tails'>('heads');
	let flipping = $state(false);
	let result = $state<CoinflipResult | null>(null);
	let error = $state<string | null>(null);

	async function flip() {
		if (flipping) return;
		flipping = true;
		error = null;
		result = null;
		try {
			const res = await apiFetch<CoinflipResult>('/api/v1/games/coinflip', {
				method: 'POST',
				body: JSON.stringify({
					bet,
					choice,
					idem_key: `coinflip:${crypto.randomUUID()}`
				})
			});
			result = res;
			haptic(res.outcome.won ? 'win' : 'lose');
		} catch (err) {
			error = err instanceof ApiError ? err.message : String(err ?? 'unknown_error');
			haptic('error');
		} finally {
			flipping = false;
		}
	}
</script>

<div class="cf-screen">
	<div class="menu-head">
		<h1 class="menu-title">Монетка</h1>
		<div class="menu-sub">орёл/решка · 50/50</div>
	</div>

	<div class="cf-picker">
		<button
			type="button"
			class={`chip cf-side ${choice === 'heads' ? 'chip-on' : ''}`}
			disabled={flipping}
			onclick={() => (choice = 'heads')}
		>
			орёл
		</button>
		<button
			type="button"
			class={`chip cf-side ${choice === 'tails' ? 'chip-on' : ''}`}
			disabled={flipping}
			onclick={() => (choice = 'tails')}
		>
			решка
		</button>
	</div>

	{#if result}
		<div class={`cf-result ${result.outcome.won ? 'cf-win' : 'cf-lose'}`}>
			<div class="cf-result-flash">
				{result.outcome.result === 'heads' ? 'орёл' : 'решка'}
			</div>
			<div class="cf-result-text">
				{result.outcome.won ? `+${result.payout}¥` : `−${result.bet}¥`}
			</div>
			{#if result.bank_capped}
				<div class="cf-capped-note">
					банк чата почти пуст — выплата урезана до {result.payout}¥ (не полные ×1.98).
					Баланс наверху мог не измениться, если урезанная выплата = твоей ставке.
				</div>
			{/if}
		</div>
	{/if}

	{#if error}
		<div class="cf-error">{error}</div>
	{/if}

	<div class="bet-row">
		<div class="bet-display">
			<span class="bet-label">ставка</span>
			<div class="bet-amount">{bet}<small>¥</small></div>
		</div>
		<div class="bet-chips">
			{#each BET_CHIPS as v (v)}
				<button
					type="button"
					class={`chip ${bet === v ? 'chip-on' : ''}`}
					disabled={flipping}
					onclick={() => (bet = v)}
				>
					{v}
				</button>
			{/each}
		</div>
	</div>

	<button type="button" class="cf-cta" disabled={flipping} onclick={flip}>
		<span class="cf-cta-label">{flipping ? 'подкидываем…' : 'ПОДКИНУТЬ МОНЕТУ'}</span>
		<span class="cf-cta-sub">{flipping ? '' : `ставка ${bet}¥`}</span>
	</button>
</div>

<style>
	.cf-screen {
		padding: 24px 18px 32px;
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
	}
	.menu-head {
		margin-bottom: var(--space-xs);
	}
	.menu-title {
		font-family: var(--font-chrome);
		font-size: var(--font-display-size);
		font-weight: 700;
		margin: 0;
		color: var(--text-primary);
	}
	.menu-sub {
		font-size: var(--font-body-size);
		color: var(--text-muted);
		margin-top: var(--space-xs);
		letter-spacing: 0.04em;
		font-family: var(--font-body);
	}

	.cf-picker {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: var(--space-sm);
	}
	.cf-side {
		font-family: var(--font-chrome);
		font-size: var(--font-heading-size);
		text-transform: uppercase;
		padding: var(--space-md);
	}

	.cf-result {
		border-radius: 14px;
		padding: var(--space-lg);
		text-align: center;
		background: var(--bg-secondary-2);
		border: 2px solid var(--border-secondary);
	}
	.cf-result-flash {
		font-family: var(--font-numeric);
		font-size: var(--font-hero-size);
		font-weight: 900;
		text-transform: uppercase;
		line-height: 1;
		color: var(--text-primary);
	}
	.cf-result-text {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		margin-top: var(--space-sm);
	}
	.cf-win {
		border-color: var(--accent-pink);
	}
	.cf-win .cf-result-text {
		color: var(--accent-pink);
	}
	.cf-lose .cf-result-text {
		color: var(--destructive-text);
	}
	.cf-capped-note {
		margin-top: var(--space-sm);
		font-size: 12px;
		line-height: 1.4;
		color: var(--accent-yellow);
		font-family: var(--font-body);
	}

	.cf-error {
		background: var(--destructive-bg);
		color: var(--destructive-text);
		border-radius: 8px;
		padding: var(--space-sm) var(--space-md);
		font-size: var(--font-body-size);
		font-family: var(--font-body);
	}

	.bet-row {
		display: flex;
		align-items: center;
		gap: var(--space-md);
	}
	.bet-display {
		display: flex;
		flex-direction: column;
	}
	.bet-label {
		font-size: var(--font-label-size);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
	}
	.bet-amount {
		font-family: var(--font-numeric);
		font-size: var(--font-display-size);
		font-weight: 900;
		color: var(--text-primary);
	}
	.bet-amount small {
		font-size: 12px;
		color: var(--accent-pink);
		margin-left: 1px;
	}
	.bet-chips {
		display: grid;
		grid-template-columns: repeat(5, 1fr);
		gap: var(--space-xs);
		flex: 1;
	}

	.cf-cta {
		background: var(--accent-pink);
		border: none;
		border-radius: 14px;
		padding: var(--space-md);
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: var(--space-xs);
		cursor: pointer;
		box-shadow: 4px 4px 0 #111;
		transition: transform 0.08s;
	}
	.cf-cta:active:not(:disabled) {
		transform: translate(2px, 2px);
		box-shadow: 2px 2px 0 #111;
	}
	.cf-cta:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.cf-cta-label {
		font-family: var(--font-shout);
		font-size: var(--font-heading-size);
		color: #1a0f12;
		letter-spacing: 0.04em;
	}
	.cf-cta-sub {
		font-size: 12px;
		color: #3a1420;
		font-family: var(--font-body);
	}
</style>
