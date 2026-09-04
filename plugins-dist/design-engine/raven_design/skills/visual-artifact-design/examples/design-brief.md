# Worked example: from request to output contract

This example demonstrates how an output contract becomes a small interactive
artifact. The subject is pour-over coffee, but the reusable part is the mapping
from requirements to state, visible behavior, and acceptance checks.

## Request

> Make an interactive page that shows curious beginners how pour-over coffee
> works.

The verbs matter: “shows how it works” calls for a visible sequence and a few
real inputs, not a static illustration surrounded by decorative controls.

## Transferable contract

- **Audience and task:** a beginner should recognize the equipment, inspect one
  phase, or play the whole sequence without first learning specialist terms.
- **Initial frame:** kettle, dripper, coffee bed, and carafe are visible before
  interaction. Controls extend the explanation rather than unlocking it.
- **State:** phase, phase progress, water temperature, and water volume live in
  one object. Outputs and SVG geometry read from that object.
- **Truth boundary:** temperature changes a labeled thermometer; volume changes
  the amount shown in the carafe. The example does not invent a flavor score or
  claim to be a brewing calculator.
- **Controls:** four phase buttons, two labeled ranges, and one sequence button
  all produce immediate visible and textual feedback.
- **Responsive behavior:** the scene and controls sit side by side when space
  permits and become one reading column at narrow widths.
- **Motion behavior:** the same final state remains available when reduced
  motion is requested; the sequence advances without animated interpolation.
- **Acceptance evidence:** inspect the initial frame, every phase, both slider
  extremes, the full sequence, keyboard focus, narrow layout, and reduced
  motion after the final edit.

## Deliberately not reusable

The coffee subject, flat fallback colors, wording, object shapes, and layout
proportions are not a template. Another explainer must derive its own model,
visual language, controls, and evidence from its content. Copy the contract-to-
state mapping, not this page's appearance.

## Code

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pour-over sequence</title>
<style>
  :root {
    --surface: #ffffff;
    --text: #111827;
    --text-subtle: #374151;
    --rule: #64748b;
    --water: #0b6e99;
    --coffee: #6f3f21;
    --focus: #1d4ed8;
  }
  * { box-sizing: border-box; }
  html { background: var(--surface); color: var(--text); font-family: system-ui, sans-serif; }
  body { margin: 0; font-size: 1rem; line-height: 1.5; }
  main { width: min(100% - 2rem, 70rem); margin-inline: auto; padding-block: 3rem; }
  h1 { max-width: 24ch; margin: 0; font-size: 2rem; line-height: 1.12; }
  h2 { margin: 0; font-size: 1.25rem; }
  .lede { max-width: 68ch; color: var(--text-subtle); }
  .workspace { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(18rem, 0.8fr); gap: 2rem; margin-top: 2rem; align-items: start; }
  figure { margin: 0; border-block: 1px solid var(--rule); }
  svg { display: block; width: 100%; height: auto; }
  figcaption { padding-block: 0.75rem; color: var(--text-subtle); }
  fieldset { min-width: 0; margin: 0; padding: 0; border: 0; }
  legend { padding: 0; font-size: 1.25rem; font-weight: 700; }
  .phase-controls { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.5rem; margin-top: 0.75rem; }
  button { min-height: 2.75rem; padding: 0.625rem 0.875rem; border: 2px solid var(--text); border-radius: 0.25rem; background: var(--surface); color: var(--text); font: inherit; font-weight: 700; cursor: pointer; }
  button[aria-pressed="true"] { background: var(--text); color: var(--surface); }
  button:focus-visible, input:focus-visible { outline: 3px solid var(--focus); outline-offset: 3px; }
  button:disabled { cursor: not-allowed; opacity: 0.72; }
  .caption { min-height: 5rem; margin-block: 1rem 1.5rem; padding-left: 1rem; border-left: 4px solid var(--coffee); }
  .field { margin-top: 1.25rem; }
  label { display: flex; justify-content: space-between; gap: 1rem; font-weight: 700; }
  output { font-variant-numeric: tabular-nums; }
  input[type="range"] { width: 100%; min-height: 2.75rem; accent-color: var(--water); }
  .play { width: 100%; margin-top: 1.5rem; background: var(--coffee); color: var(--surface); border-color: var(--coffee); }
  .status { color: var(--text-subtle); }
  @media (max-width: 48rem) {
    main { width: min(100% - 1.25rem, 70rem); padding-block: 1.5rem; }
    h1 { font-size: 1.625rem; }
    .workspace { grid-template-columns: 1fr; gap: 1.5rem; }
  }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { scroll-behavior: auto; transition: none; }
  }
</style>
</head>
<body>
<main>
  <h1>See the brewing sequence, one phase at a time</h1>
  <p class="lede">The controls change the same state that drives the labels and drawing.</p>

  <div class="workspace">
    <figure>
      <svg viewBox="0 0 560 500" role="img" aria-labelledby="brew-title brew-description">
        <title id="brew-title">Pour-over brewing equipment and process</title>
        <desc id="brew-description">A kettle pours into a dripper above a carafe. A thermometer and the carafe level respond to the controls.</desc>
        <defs>
          <clipPath id="carafe-clip">
            <path d="M190 350 H370 L350 466 Q347 478 334 478 H226 Q213 478 210 466 Z"></path>
          </clipPath>
        </defs>

        <line x1="72" y1="478" x2="488" y2="478" stroke="var(--rule)" stroke-width="3"></line>

        <g id="kettle">
          <path d="M346 82 Q414 72 432 110 L424 176 Q422 190 406 190 H338 Q322 190 320 176 L312 112 Q320 88 346 82 Z" fill="#cbd5e1" stroke="var(--text)" stroke-width="3"></path>
          <path d="M316 112 Q270 116 240 150 L254 160 Q278 134 320 132 Z" fill="#cbd5e1" stroke="var(--text)" stroke-width="3"></path>
          <path d="M422 112 Q466 124 468 162 Q468 198 432 206" fill="none" stroke="var(--text)" stroke-width="12" stroke-linecap="round"></path>
        </g>

        <path id="stream" d="M248 154 Q270 214 280 250" fill="none" stroke="var(--water)" stroke-width="10" stroke-linecap="round" opacity="0"></path>

        <g id="dripper">
          <path d="M210 236 H350 L326 326 Q322 340 308 340 H252 Q238 340 234 326 Z" fill="#f8fafc" stroke="var(--text)" stroke-width="3"></path>
          <ellipse id="bed" cx="280" cy="270" rx="54" ry="14" fill="var(--coffee)"></ellipse>
          <g id="bubbles" fill="var(--surface)" opacity="0">
            <circle cx="258" cy="264" r="5"></circle>
            <circle cx="282" cy="259" r="4"></circle>
            <circle cx="305" cy="267" r="5"></circle>
          </g>
        </g>

        <g id="carafe">
          <path d="M190 350 H370 L350 466 Q347 478 334 478 H226 Q213 478 210 466 Z" fill="#f8fafc" stroke="var(--text)" stroke-width="3"></path>
          <g clip-path="url(#carafe-clip)">
            <rect id="coffee-fill" x="190" y="478" width="180" height="0" fill="var(--coffee)"></rect>
            <ellipse id="coffee-surface" cx="280" cy="478" rx="70" ry="6" fill="var(--coffee)"></ellipse>
            <ellipse id="ripple" cx="280" cy="430" rx="24" ry="5" fill="none" stroke="var(--surface)" stroke-width="3" opacity="0"></ellipse>
          </g>
        </g>

        <g id="thermometer">
          <rect x="94" y="128" width="22" height="150" rx="11" fill="#f8fafc" stroke="var(--text)" stroke-width="3"></rect>
          <rect id="temperature-fill" x="100" y="230" width="10" height="42" rx="5" fill="var(--water)"></rect>
          <circle cx="105" cy="280" r="16" fill="var(--water)" stroke="var(--text)" stroke-width="3"></circle>
        </g>
      </svg>
      <figcaption>Temperature is always printed as a number; water volume controls the computed carafe level.</figcaption>
    </figure>

    <section aria-labelledby="controls-title">
      <h2 id="controls-title">Explore the process</h2>
      <fieldset>
        <legend>Choose a phase</legend>
        <div class="phase-controls">
          <button type="button" data-phase="0" aria-pressed="true">Wet the grounds</button>
          <button type="button" data-phase="1" aria-pressed="false">Let gas escape</button>
          <button type="button" data-phase="2" aria-pressed="false">Continue pouring</button>
          <button type="button" data-phase="3" aria-pressed="false">Finish</button>
        </div>
      </fieldset>

      <p class="caption" id="caption" aria-live="polite">The equipment and full sequence are visible before playback.</p>

      <div class="field">
        <label for="temperature">Water temperature <output id="temperature-output" for="temperature">92°C</output></label>
        <input id="temperature" type="range" min="88" max="96" value="92" step="1">
      </div>
      <div class="field">
        <label for="volume">Water volume <output id="volume-output" for="volume">300 mL</output></label>
        <input id="volume" type="range" min="200" max="400" value="300" step="25">
      </div>

      <button class="play" id="play-sequence" type="button">Play the four phases</button>
      <p class="status" id="sequence-status" aria-live="polite">Ready.</p>
    </section>
  </div>
</main>

<script>
  const PHASES = [
    { label: 'Wet the grounds', duration: 1400, caption: 'A controlled first pour wets the coffee bed.' },
    { label: 'Let gas escape', duration: 1400, caption: 'The wet bed expands while trapped gas escapes.' },
    { label: 'Continue pouring', duration: 1800, caption: 'More water passes through the bed and collects below.' },
    { label: 'Finish', duration: 1000, caption: 'The stream stops and the brewed volume remains visible.' },
  ];
  const state = { phase: 0, progress: 0, temperature: 92, volume: 300 };
  const phaseButtons = [...document.querySelectorAll('[data-phase]')];
  const temperature = document.getElementById('temperature');
  const volume = document.getElementById('volume');
  const playButton = document.getElementById('play-sequence');
  const status = document.getElementById('sequence-status');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  function setAttributes(id, attributes) {
    const element = document.getElementById(id);
    Object.entries(attributes).forEach(([name, value]) => element.setAttribute(name, value));
  }

  function render() {
    const { phase, progress, temperature: temp, volume: waterVolume } = state;
    const phaseProgress = Math.max(0, Math.min(1, progress));
    const temperatureHeight = 42 + ((temp - 88) / 8) * 92;
    const maximumCoffeeHeight = 62 + ((waterVolume - 200) / 200) * 56;
    const fillFraction = phase === 0
      ? 0.12 * phaseProgress
      : phase === 1
        ? 0.18
        : phase === 2
          ? 0.18 + 0.82 * phaseProgress
          : 1;
    const coffeeHeight = maximumCoffeeHeight * fillFraction;
    const coffeeY = 478 - coffeeHeight;
    const bedScale = phase === 1 ? 1 + Math.sin(phaseProgress * Math.PI) * 0.12 : 1;
    const streamVisible = phase === 0 || phase === 2;
    const kettleAngle = streamVisible ? -8 * phaseProgress : 0;

    setAttributes('temperature-fill', { y: 272 - temperatureHeight, height: temperatureHeight });
    setAttributes('coffee-fill', { y: coffeeY, height: coffeeHeight });
    setAttributes('coffee-surface', { cy: coffeeY, opacity: coffeeHeight > 0 ? 1 : 0 });
    setAttributes('ripple', { cy: coffeeY, opacity: phase === 3 ? 0.72 : 0 });
    setAttributes('stream', { opacity: streamVisible ? Math.max(0.25, phaseProgress) : 0 });
    setAttributes('kettle', { transform: `rotate(${kettleAngle} 378 138)` });
    setAttributes('bed', { transform: `translate(280 270) scale(1 ${bedScale}) translate(-280 -270)` });
    setAttributes('bubbles', { opacity: phase === 1 ? phaseProgress : 0 });

    document.getElementById('temperature-output').textContent = `${temp}°C`;
    document.getElementById('volume-output').textContent = `${waterVolume} mL`;
    document.getElementById('caption').textContent = PHASES[phase].caption;
    phaseButtons.forEach((button, index) => {
      button.setAttribute('aria-pressed', String(index === phase));
    });
  }

  function showPhase(index) {
    state.phase = index;
    state.progress = 1;
    render();
    status.textContent = `${PHASES[index].label} is shown.`;
  }

  function animatePhase(index) {
    state.phase = index;
    state.progress = 0;
    render();
    if (reducedMotion.matches) {
      state.progress = 1;
      render();
      return Promise.resolve();
    }
    return new Promise(resolve => {
      const started = performance.now();
      function frame(now) {
        state.progress = Math.min(1, (now - started) / PHASES[index].duration);
        render();
        if (state.progress < 1) requestAnimationFrame(frame);
        else resolve();
      }
      requestAnimationFrame(frame);
    });
  }

  phaseButtons.forEach((button, index) => {
    button.addEventListener('click', () => showPhase(index));
  });
  temperature.addEventListener('input', () => {
    state.temperature = Number(temperature.value);
    render();
  });
  volume.addEventListener('input', () => {
    state.volume = Number(volume.value);
    render();
  });
  playButton.addEventListener('click', async () => {
    playButton.disabled = true;
    phaseButtons.forEach(button => { button.disabled = true; });
    status.textContent = reducedMotion.matches ? 'Showing the final state without animation.' : 'Sequence is playing.';
    for (let index = 0; index < PHASES.length; index += 1) await animatePhase(index);
    playButton.disabled = false;
    phaseButtons.forEach(button => { button.disabled = false; });
    status.textContent = 'Sequence complete.';
  });

  render();
</script>
</body>
</html>
```
