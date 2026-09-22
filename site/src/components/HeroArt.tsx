// Original illustration: survey contours at dusk, with a bat echolocation call
// rendered as a spectrogram sweep. Pure SVG, themes via CSS variables.
export function HeroArt() {
  const contours = Array.from({ length: 7 }, (_, i) => {
    const y = 150 + i * 26;
    const a = 18 + i * 4;
    return `M-10 ${y} C 90 ${y - a}, 170 ${y + a}, 260 ${y - a / 2} S 420 ${y + a}, 520 ${y - 6}`;
  });
  // frequency-modulated call: steep downward sweeps, like a pipistrelle pass
  const calls = [60, 150, 240, 330, 420].map((x, i) => {
    const top = 34 + (i % 2) * 6;
    return `M${x} ${top} C ${x + 4} ${top + 40}, ${x + 12} ${top + 62}, ${x + 30} ${top + 70}`;
  });
  return (
    <svg className="hero-art" viewBox="0 0 500 340" role="img"
      aria-label="Map contour lines under a bat call spectrogram">
      <defs>
        <linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="var(--night)" />
          <stop offset="1" stopColor="var(--forest)" />
        </linearGradient>
      </defs>
      <rect width="500" height="340" rx="20" fill="url(#sky)" />
      <g stroke="var(--sage)" strokeOpacity="0.14">
        {Array.from({ length: 9 }, (_, i) => (
          <line key={i} x1="0" x2="500" y1={24 + i * 12} y2={24 + i * 12} />
        ))}
      </g>
      <g fill="none" stroke="var(--gold)" strokeWidth="3" strokeLinecap="round">
        {calls.map((d, i) => <path key={i} d={d} opacity={0.55 + (i % 3) * 0.15} />)}
      </g>
      <g fill="none" stroke="var(--sage)" strokeWidth="1.4">
        {contours.map((d, i) => <path key={i} d={d} opacity={0.35 + i * 0.08} />)}
      </g>
      <g fill="var(--gold)">
        <circle cx="318" cy="228" r="5" />
        <circle cx="318" cy="228" r="12" fill="none" stroke="var(--gold)" strokeOpacity="0.5" />
      </g>
      <text x="24" y="318" fill="var(--sage)" fontSize="11" fontFamily="Inter, sans-serif" opacity="0.8">
        20–120 kHz · survey transect
      </text>
    </svg>
  );
}
