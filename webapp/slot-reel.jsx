// ─────────────────────────────────────────────────────────────────────
// SlotMachine — the interactive 3×5 reel widget.
// Skin-agnostic: takes `skin` props that control color/frame/button.
// Shared mechanics: spin, weighted random, win detection, big-win FX.
// ─────────────────────────────────────────────────────────────────────

const { SYMBOLS, SYMBOL_LIST, REEL_STRIPS, PAYLINES,
        evaluateGrid, randomGrid, pickFromStrip } = window.SLOT_DATA;

// SymbolTile — one cell on a reel. The card has a manga corner-cut frame.
function SymbolTile({ symId, highlight, tileSize, cornerCut = 10 }) {
  const sym = SYMBOLS[symId];
  if (!sym) return <div style={{ width: tileSize, height: tileSize }} />;
  const clip = `polygon(
    ${cornerCut}px 0, calc(100% - ${cornerCut}px) 0, 100% ${cornerCut}px,
    100% calc(100% - ${cornerCut}px), calc(100% - ${cornerCut}px) 100%,
    ${cornerCut}px 100%, 0 calc(100% - ${cornerCut}px), 0 ${cornerCut}px
  )`;
  return (
    <div className={`tile ${highlight ? 'tile-hit' : ''}`}
         style={{
           width: tileSize, height: tileSize,
           '--tint': sym.tint,
           clipPath: clip,
         }}>
      <div className="tile-bg" style={{ background: sym.tint }} />
      <img src={sym.src} alt={sym.name} draggable={false} />
      {sym.role === 'wild' && <span className="tile-badge tile-badge-wild">WILD</span>}
      {sym.role === 'scatter' && <span className="tile-badge tile-badge-scatter">SCATTER</span>}
      {highlight && <div className="tile-flash" />}
    </div>
  );
}

// Reel — single column. During a spin, it animates a long random
// strip translating upward, then snaps to its final 3 visible symbols.
function Reel({ reelIdx, finalCol, spinning, delay, spinDuration, tileSize, gap, highlights }) {
  // Build the rolling strip synchronously each time spin starts. Using useMemo
  // (not useState+useEffect) so the strip is ready on the same render where
  // spinning flips true — otherwise the first render maps a null array.
  const rollSyms = React.useMemo(() => {
    if (!spinning) return null;
    const buffer = [];
    const total = 18 + reelIdx * 3; // longer strip for later reels = stagger
    for (let i = 0; i < total; i++) buffer.push(pickFromStrip(reelIdx));
    buffer.push(finalCol[0], finalCol[1], finalCol[2]);
    return buffer;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spinning, reelIdx, finalCol.join('|')]);

  const colKey = finalCol.join('|');
  const stripCount = rollSyms ? rollSyms.length : 0;
  // how far to translate during the spin animation
  const travel = stripCount > 0 ? (stripCount - 3) * (tileSize + gap) : 0;

  if (!spinning || !rollSyms) {
    // static state: just show the final 3
    return (
      <div className="reel" style={{ width: tileSize }}>
        <div className="reel-strip" style={{ gap }}>
          {finalCol.map((sid, r) => {
            const hit = highlights && highlights.some(([rr, cc]) => rr === r && cc === reelIdx);
            return <SymbolTile key={`${colKey}-${r}`} symId={sid} highlight={hit} tileSize={tileSize} />;
          })}
        </div>
      </div>
    );
  }

  return (
    <div className="reel" style={{ width: tileSize }}>
      <div className="reel-strip reel-strip-spinning"
           key={`${colKey}-spin`}
           style={{
             gap,
             animation: `reelSpin ${spinDuration}ms cubic-bezier(.2,.6,.2,1) ${delay}ms forwards`,
             '--travel': `-${travel}px`,
           }}>
        {rollSyms.map((sid, i) => (
          <SymbolTile key={i} symId={sid} tileSize={tileSize} />
        ))}
      </div>
    </div>
  );
}

window.SymbolTile = SymbolTile;
window.Reel = Reel;
