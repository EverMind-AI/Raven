# HTML and canvas

Apply this card to HTML/CSS/JavaScript pages, web applications, browser-based
dashboards, canvas scenes, and browser animation.

## Structure and layout

- Deliver runnable HTML/CSS/JavaScript, not a screenshot of a page.
- Use semantic HTML and a clear DOM structure. Use CSS variables for the visual
  system and flex/grid for page layout. Reserve absolute positioning for
  genuinely layered scenes and local overlays.
- Prefer intrinsic sizing. Do not lock text-bearing containers to fragile
  heights. Prevent unintended horizontal scroll, overlap, clipped focus rings,
  and content hidden behind fixed chrome.
- Recompose deliberately at the required minimum, typical, and maximum widths;
  do not merely scale one layout. Keep the primary task and focal content
  apparent in the initial viewport.
- Keep remote scripts, fonts, images, and CDN dependencies out unless the
  output contract allows them. Verify every allowed dependency actually loads.

## Interaction and accessibility

- Give every control a real state transition and an immediate visible result.
  Provide applicable hover, focus, active, disabled, loading, empty, and error
  states.
- Keep labels, names, keyboard order, visible focus, and pointer targets
  coherent. Do not use color alone to communicate critical state.
- Preserve useful content before interaction. A tooltip, tab, modal, or start
  button may expose detail, but must not conceal the artifact's basic meaning.
- Exercise every critical path with `preview_file` actions. Treat an action
  reported as successful with `changed_pixel_ratio: 0` as suspect until a
  non-visual effect is intentionally proven.
- Inspect the resulting state after each action, not merely the action log.

## Runtime and canvas

- Deliver with zero page and console errors, broken resources, `NaN`,
  `undefined`, or `Infinity` in visible output.
- For canvas, establish logical dimensions, CSS dimensions, device-pixel
  scaling, and resize behavior before drawing. Keep hit-testing in the same
  coordinate system as rendering.
- Make the first frame complete. For animation, use
  `motion_mode: "dynamic"` and inspect representative start, middle,
  transition, loop, and end states.
- Keep motion purposeful and bounded. Confirm animated objects remain in frame
  and provide reduced-motion behavior when the target context requires it.

## Validation matrix

Render at every required viewport. At minimum, verify:

- the initial state;
- every primary control after use;
- keyboard focus on interactive elements;
- the densest and emptiest data/content states when applicable;
- the narrowest and widest supported layouts;
- static and dynamic animation previews when motion exists.
