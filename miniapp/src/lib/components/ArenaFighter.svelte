<script lang="ts">
	type FighterId = 'tank' | 'assassin' | 'berserker' | 'tactician' | null;
	type Size = 'sm' | 'md' | 'lg';
	type Side = 'player' | 'opponent';
	type State = 'idle' | 'selected' | 'attack' | 'hit' | 'victory' | 'defeat';

	let {
		fighter = null,
		size = 'md',
		side = 'player',
		state = 'idle',
		action = ''
	} = $props<{
		fighter?: FighterId;
		size?: Size;
		side?: Side;
		state?: State;
		action?: string;
	}>();

	type Profile = { name: string; mark: string; className: string };
	type ProfileKey = NonNullable<FighterId> | 'unknown';
	const profiles: Record<ProfileKey, Profile> = {
		tank: { name: 'Танк', mark: 'T', className: 'tank' },
		assassin: { name: 'Ассасин', mark: 'A', className: 'assassin' },
		berserker: { name: 'Берсерк', mark: 'B', className: 'berserker' },
		tactician: { name: 'Тактик', mark: 'X', className: 'tactician' },
		unknown: { name: 'Соперник', mark: '?', className: 'unknown' }
	} as const;

	const profile = $derived(profiles[(fighter ?? 'unknown') as ProfileKey]);
</script>

<div
	class={`fighter-visual fighter-${profile.className} fighter-${size} fighter-${side} state-${state} action-${action}`}
	role="img"
	aria-label={profile.name}
>
	<div class="fighter-aura"></div>
	<div class="fighter-ring ring-back"></div>
	<div class="fighter-ring ring-front"></div>
	<div class="fighter-shadow"></div>
	<div class="fighter-silhouette">
		<div class="fighter-head"><span>{profile.mark}</span></div>
		<div class="fighter-torso"></div>
		<div class="fighter-arm arm-back"></div>
		<div class="fighter-arm arm-front"></div>
		<div class="fighter-weapon"></div>
	</div>
	<div class="fighter-scanline"></div>
	<div class="fighter-label">{profile.name}</div>
</div>

<style>
	.fighter-visual {
		--fighter-main: var(--accent-cyan);
		--fighter-hot: #b8f6ff;
		--fighter-dark: #123342;
		position: relative;
		width: 128px;
		height: 132px;
		flex: 0 0 auto;
		isolation: isolate;
		filter: drop-shadow(0 12px 16px rgba(0, 0, 0, 0.38));
	}
	.fighter-sm { width: 76px; height: 82px; }
	.fighter-lg { width: 170px; height: 178px; }
	.fighter-assassin { --fighter-main: var(--accent-pink); --fighter-hot: #ffc1d4; --fighter-dark: #42192f; }
	.fighter-berserker { --fighter-main: var(--accent-yellow); --fighter-hot: #fff3ad; --fighter-dark: #493711; }
	.fighter-tactician { --fighter-main: #a58bff; --fighter-hot: #ddd5ff; --fighter-dark: #2c2355; }
	.fighter-unknown { --fighter-main: #6c6881; --fighter-hot: #aaa5bd; --fighter-dark: #292638; }
	.fighter-opponent { transform: scaleX(-1); }
	.fighter-opponent .fighter-label { transform: scaleX(-1); }
	.fighter-aura {
		position: absolute;
		inset: 18% 8% 12%;
		z-index: -2;
		border-radius: 50%;
		background: radial-gradient(ellipse, color-mix(in srgb, var(--fighter-main) 28%, transparent), transparent 68%);
		animation: aura-pulse 2.8s ease-in-out infinite;
	}
	.fighter-ring {
		position: absolute;
		left: 50%;
		border: 1px solid color-mix(in srgb, var(--fighter-main) 65%, transparent);
		border-radius: 50%;
		transform: translateX(-50%) rotate(-12deg);
	}
	.ring-back { width: 86%; height: 38%; top: 26%; opacity: .28; border-style: dashed; animation: ring-drift 8s linear infinite; }
	.ring-front { width: 72%; height: 28%; top: 42%; opacity: .58; transform: translateX(-50%) rotate(18deg); }
	.fighter-shadow {
		position: absolute;
		left: 12%;
		right: 12%;
		bottom: 12%;
		height: 13%;
		border-radius: 50%;
		background: #05040a;
		box-shadow: 0 0 18px color-mix(in srgb, var(--fighter-main) 30%, transparent);
	}
	.fighter-silhouette { position: absolute; inset: 6% 20% 12%; }
	.fighter-head {
		position: absolute;
		left: 25%;
		top: 0;
		width: 50%;
		aspect-ratio: 1;
		z-index: 2;
		border: 2px solid var(--fighter-main);
		border-radius: 42% 48% 44% 45%;
		background: radial-gradient(circle at 60% 25%, var(--fighter-hot), var(--fighter-main) 12%, var(--fighter-dark) 56%, #080812 58%);
		box-shadow: inset -8px -8px 0 rgba(0, 0, 0, .2), 0 0 14px color-mix(in srgb, var(--fighter-main) 58%, transparent);
		transform: rotate(-5deg);
	}
	.fighter-head::after {
		content: '';
		position: absolute;
		left: -12%;
		top: 44%;
		width: 124%;
		height: 16%;
		border: 1px solid var(--fighter-hot);
		background: color-mix(in srgb, var(--fighter-main) 55%, #070710);
		box-shadow: 0 0 8px var(--fighter-main);
	}
	.fighter-head span {
		position: absolute;
		inset: 18% 0 auto;
		z-index: 3;
		color: var(--fighter-hot);
		font: 700 18px var(--font-numeric);
		text-align: center;
		text-shadow: 0 0 8px var(--fighter-main);
	}
	.fighter-torso {
		position: absolute;
		left: 10%;
		right: 10%;
		top: 31%;
		bottom: 0;
		border: 2px solid var(--fighter-main);
		border-radius: 42% 42% 22% 22%;
		background: linear-gradient(110deg, #090912 0 23%, var(--fighter-dark) 24% 52%, #090912 53% 70%, color-mix(in srgb, var(--fighter-main) 35%, #090912) 71%);
		clip-path: polygon(22% 0, 78% 0, 100% 31%, 84% 100%, 16% 100%, 0 31%);
		box-shadow: inset 0 0 0 2px rgba(255, 255, 255, .04), 0 0 12px color-mix(in srgb, var(--fighter-main) 32%, transparent);
	}
	.fighter-torso::after {
		content: '';
		position: absolute;
		left: 27%;
		right: 27%;
		top: 26%;
		height: 3px;
		background: var(--fighter-hot);
		box-shadow: 0 9px 0 color-mix(in srgb, var(--fighter-main) 60%, transparent), 0 18px 0 color-mix(in srgb, var(--fighter-main) 35%, transparent);
	}
	.fighter-arm { position: absolute; top: 38%; width: 23%; height: 51%; border: 2px solid var(--fighter-main); border-radius: 50% 50% 35% 35%; background: linear-gradient(var(--fighter-dark), #080812); transform-origin: top center; }
	.arm-back { left: -2%; transform: rotate(24deg); opacity: .82; }
	.arm-front { right: -2%; transform: rotate(-28deg); }
	.fighter-weapon { position: absolute; right: -12%; top: 22%; width: 5px; height: 63%; border-radius: 5px; background: linear-gradient(var(--fighter-hot), var(--fighter-main)); box-shadow: 0 0 10px var(--fighter-main); transform: rotate(-32deg); }
	.fighter-berserker .fighter-weapon { width: 12px; height: 22%; top: 35%; border-radius: 50%; transform: rotate(-25deg); }
	.fighter-tank .fighter-torso { border-radius: 28%; }
	.fighter-tactician .fighter-weapon { height: 34%; top: 28%; border-radius: 2px; }
	.fighter-unknown .fighter-head { border-style: dashed; }
	.fighter-unknown .fighter-weapon { display: none; }
	.fighter-scanline { position: absolute; inset: 0; opacity: .16; background: repeating-linear-gradient(0deg, transparent 0 4px, var(--fighter-main) 5px, transparent 6px); mix-blend-mode: screen; pointer-events: none; }
	.fighter-label { position: absolute; left: 0; right: 0; bottom: 0; color: var(--fighter-hot); font: 10px var(--font-body); letter-spacing: .12em; text-align: center; text-transform: uppercase; text-shadow: 0 2px 4px #05040a; }
	.state-selected { filter: drop-shadow(0 0 16px color-mix(in srgb, var(--fighter-main) 70%, transparent)); }
	.state-attack .fighter-silhouette { animation: fighter-lunge .42s ease-out; }
	.state-hit .fighter-silhouette { animation: fighter-hit .34s ease-out; }
	.state-victory .fighter-aura { animation: victory-aura .7s ease-in-out infinite alternate; }
	.state-defeat { opacity: .55; filter: grayscale(.35) drop-shadow(0 8px 12px rgba(0, 0, 0, .5)); }
	.action-heavy_attack .fighter-weapon { box-shadow: 0 0 18px 4px var(--fighter-main); }
	.action-special .fighter-aura { background: radial-gradient(ellipse, color-mix(in srgb, var(--fighter-hot) 45%, transparent), transparent 68%); }
	@keyframes aura-pulse { 0%, 100% { transform: scale(.92); opacity: .65; } 50% { transform: scale(1.08); opacity: 1; } }
	@keyframes ring-drift { to { transform: translateX(-50%) rotate(348deg); } }
	@keyframes fighter-lunge { 45% { transform: translateX(12%) scale(1.04); } }
	@keyframes fighter-hit { 25% { transform: translateX(-7%) rotate(-5deg); filter: brightness(2); } 60% { transform: translateX(5%) rotate(4deg); } }
	@keyframes victory-aura { to { transform: scale(1.18); opacity: 1; } }
	@media (prefers-reduced-motion: reduce) {
		.fighter-aura, .fighter-ring, .fighter-silhouette { animation: none; }
	}
</style>
