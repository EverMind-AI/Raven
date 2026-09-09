# Worked example: a structured SVG scene with bounded motion

This example demonstrates scene construction, paint order, shared geometry, and
motion controls. Its lighthouse subject is only a vehicle for those mechanisms;
the palette and composition are not a house style.

## Transferable mechanism

1. **Assign visual roles from the scene.** A dark ambient field separates the
   warm lamp from its surroundings. The exact colors are replaceable; the
   foreground, ambient, and light-source roles are the reusable decision.
2. **Build depth through paint order.** Sky and distant objects paint first,
   followed by the beam, sea, lighthouse, waves, and boat. Later layers occlude
   earlier ones without ad hoc cover patches.
3. **Keep related shapes derived.** Tower stripes are clipped by one tower
   silhouette. Repeated stars and wave segments use groups rather than unrelated
   copies with independent behavior.
4. **Bound motion before animating.** The rotating beam uses a radius smaller
   than every distance from its pivot to the viewBox edge. The boat's complete
   travel remains inside the viewBox. Repeating waves deliberately extend past
   the crop so translating one exact period creates a continuous loop.
5. **Make the static frame complete.** The lighthouse, beam, horizon, and boat
   communicate the scene before animation starts.
6. **Use a native control and honor motion preferences.** The HTML button has
   visible focus, state text, and `aria-pressed`. It is disabled when the system
   requests reduced motion, while the composed still frame remains available.

## Deliberately not reusable

The lighthouse silhouette, flat night colors, object positions, and animation
durations belong only to this demonstration. Another scene must derive its own
camera, light, depth, material, and motion from its subject and output contract.

## Code

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bounded motion in an SVG scene</title>
<style>
  :root {
    --surface: #ffffff;
    --text: #111827;
    --text-subtle: #374151;
    --rule: #64748b;
    --night: #102a43;
    --sea: #174a6e;
    --sea-near: #0f5d7a;
    --light: #ffd166;
    --tower: #f8fafc;
    --stripe: #b42318;
  }
  * { box-sizing: border-box; }
  html { background: var(--surface); color: var(--text); font-family: system-ui, sans-serif; }
  body { margin: 0; font-size: 1rem; line-height: 1.5; }
  main { width: min(100% - 2rem, 68rem); margin-inline: auto; padding-block: 3rem; }
  h1 { max-width: 24ch; margin: 0; font-size: 2rem; line-height: 1.12; }
  .lede { max-width: 70ch; color: var(--text-subtle); }
  figure { margin: 2rem 0 0; }
  svg { display: block; width: 100%; height: auto; background: var(--night); }
  figcaption { margin-top: 0.75rem; }
  .controls { display: flex; align-items: center; flex-wrap: wrap; gap: 1rem; margin-top: 1rem; }
  button { min-height: 2.75rem; padding: 0.625rem 1rem; border: 2px solid var(--text); border-radius: 0.25rem; background: var(--surface); color: var(--text); font: inherit; font-weight: 700; cursor: pointer; }
  button:focus-visible { outline: 3px solid var(--rule); outline-offset: 3px; }
  button:disabled { cursor: not-allowed; opacity: 0.72; }
  #beam { transform-origin: 300px 236px; animation: sweep 14s linear infinite; }
  #lamp { transform-origin: 300px 232px; animation: breathe 4s ease-in-out infinite; }
  #waves-far { animation: drift 16s linear infinite; }
  #waves-near { animation: drift 11s linear infinite reverse; }
  #boat-track { animation: cruise 24s ease-in-out infinite alternate; }
  #boat { transform-origin: 690px 452px; animation: bob 5s ease-in-out infinite; }
  .star-a { animation: twinkle 4s ease-in-out infinite; }
  .star-b { animation: twinkle 5.5s ease-in-out 1s infinite; }
  @keyframes sweep { to { transform: rotate(360deg); } }
  @keyframes breathe { 0%, 100% { opacity: 0.62; } 50% { opacity: 1; } }
  @keyframes drift { to { transform: translateX(-480px); } }
  @keyframes cruise { from { transform: translateX(0); } to { transform: translateX(150px); } }
  @keyframes bob { 0%, 100% { transform: rotate(-1.5deg); } 50% { transform: rotate(1.5deg); } }
  @keyframes twinkle { 0%, 100% { opacity: 0.35; } 50% { opacity: 0.95; } }
  .paused #beam, .paused #lamp, .paused #waves-far, .paused #waves-near,
  .paused #boat-track, .paused #boat, .paused .star-a, .paused .star-b {
    animation-play-state: paused;
  }
  @media (max-width: 44rem) {
    main { width: min(100% - 1.25rem, 68rem); padding-block: 1.5rem; }
    h1 { font-size: 1.625rem; }
    .controls { align-items: flex-start; flex-direction: column; }
  }
  @media (prefers-reduced-motion: reduce) {
    #beam, #lamp, #waves-far, #waves-near, #boat-track, #boat, .star-a, .star-b {
      animation: none;
    }
  }
</style>
</head>
<body>
<main>
  <h1>A complete still frame, with motion added on top</h1>
  <p class="lede">The scene groups objects by depth and keeps every animated range explicit.</p>

  <figure>
    <svg viewBox="0 0 960 600" role="img" aria-labelledby="scene-title scene-description">
      <title id="scene-title">Lighthouse and boat at night</title>
      <desc id="scene-description">A lighthouse casts a short warm beam over a dark sea while a boat moves within the frame.</desc>
      <defs>
        <clipPath id="tower-clip">
          <path d="M274 442 L291 252 H309 L326 442 Z"></path>
        </clipPath>
      </defs>

      <rect width="960" height="600" fill="var(--night)"></rect>
      <g fill="var(--tower)">
        <circle class="star-a" cx="120" cy="112" r="3"></circle>
        <circle class="star-b" cx="205" cy="76" r="2.5"></circle>
        <circle class="star-a" cx="515" cy="92" r="2.5"></circle>
        <circle class="star-b" cx="620" cy="142" r="3"></circle>
        <circle class="star-a" cx="850" cy="86" r="2.5"></circle>
        <circle cx="790" cy="118" r="32"></circle>
      </g>
      <g fill="#cbd5e1" opacity="0.42">
        <path d="M92 164 Q126 132 160 164 Q194 138 226 170 Q170 184 108 177 Z"></path>
        <path d="M575 190 Q610 158 642 190 Q680 164 714 198 Q646 211 588 204 Z"></path>
      </g>

      <g id="beam" fill="var(--light)" opacity="0.42">
        <polygon points="300,236 516,210 516,262"></polygon>
        <polygon points="300,236 516,210 516,262" transform="rotate(180 300 236)"></polygon>
      </g>

      <rect y="360" width="960" height="240" fill="var(--sea)"></rect>
      <g id="waves-far" fill="none" stroke="#93c5d8" stroke-width="5" opacity="0.46">
        <path d="M0 406 Q120 380 240 406 T480 406 T720 406 T960 406 T1200 406 T1440 406"></path>
      </g>
      <g id="waves-near" fill="none" stroke="var(--sea-near)" stroke-width="18" opacity="0.9">
        <path d="M0 514 Q120 478 240 514 T480 514 T720 514 T960 514 T1200 514 T1440 514"></path>
      </g>

      <g id="lighthouse">
        <path d="M274 442 L291 252 H309 L326 442 Z" fill="var(--tower)"></path>
        <g clip-path="url(#tower-clip)" fill="var(--stripe)">
          <rect x="264" y="298" width="72" height="34" transform="rotate(-5 300 315)"></rect>
          <rect x="264" y="372" width="72" height="36" transform="rotate(-5 300 390)"></rect>
        </g>
        <rect x="278" y="240" width="44" height="12" fill="#cbd5e1"></rect>
        <rect x="284" y="212" width="32" height="28" fill="#172033"></rect>
        <path d="M279 212 Q300 188 321 212 Z" fill="var(--stripe)"></path>
        <circle id="lamp" cx="300" cy="226" r="11" fill="var(--light)"></circle>
        <path d="M248 470 L272 432 H328 L356 470 Z" fill="#172033"></path>
      </g>

      <g id="boat-track">
        <g id="boat">
          <path d="M646 452 H734 L716 472 H664 Z" fill="#172033"></path>
          <line x1="688" y1="452" x2="688" y2="414" stroke="#172033" stroke-width="4"></line>
          <path d="M692 416 L722 446 H692 Z" fill="#dbeafe"></path>
          <circle cx="688" cy="409" r="4" fill="var(--light)"></circle>
        </g>
      </g>
    </svg>
    <figcaption>The beam radius stays below 218 units; its pivot is at least 236 units from the nearest viewBox edge. A conservative boat envelope stays below x=890.</figcaption>
  </figure>

  <div class="controls">
    <button id="motion-toggle" type="button" aria-pressed="false">Pause motion</button>
    <span id="motion-status" aria-live="polite">Motion is running.</span>
  </div>
</main>

<script>
  const button = document.getElementById('motion-toggle');
  const status = document.getElementById('motion-status');
  const motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)');

  function syncMotionPreference() {
    document.body.classList.remove('paused');
    button.setAttribute('aria-pressed', 'false');
    button.disabled = motionPreference.matches;
    button.textContent = motionPreference.matches ? 'Motion disabled by system preference' : 'Pause motion';
    status.textContent = motionPreference.matches ? 'The static frame is shown.' : 'Motion is running.';
  }

  button.addEventListener('click', () => {
    const paused = document.body.classList.toggle('paused');
    button.setAttribute('aria-pressed', String(paused));
    button.textContent = paused ? 'Resume motion' : 'Pause motion';
    status.textContent = paused ? 'Motion is paused.' : 'Motion is running.';
  });
  motionPreference.addEventListener('change', syncMotionPreference);
  syncMotionPreference();
</script>
</body>
</html>
```
