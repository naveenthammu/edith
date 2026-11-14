# EDITH Galactic Voyage

`edith-sun-galaxy.html` is a standalone, interactive Canvas visualization of the
Sun and planets travelling through a stylized Milky Way. It is separate from the
Python voice assistant: it does not import or call `main.py`, and it has no
JavaScript package dependencies.

## What it shows

- A barred spiral galaxy with a bright central bulge, four spiral arms (two
  major and two less populated arms), diffuse stars, colored gas, and a
  twinkling distant star field.
- The Sun moving around the galactic center, with the planets orbiting the Sun.
  Their trails show the resulting helix-like paths through the galaxy.
- A perspective camera that slowly drifts around the scene. Dragging rotates
  the camera so the galaxy and orbital paths can be viewed from different
  directions.
- A dark background, EDITH label, controls hint, and explanatory caption.

This is a real-time procedural illustration, not a photograph or a precise
astronomical simulator. Distances, planet sizes, orbital periods, and trail
length are intentionally adjusted so the motion is visible. The `~230 km/s`
caption is explanatory; the displayed simulation is not calibrated to convert
its animation into real time or physical distances.

## Run it

### Open the file directly

Open `edith-sun-galaxy.html` in a current desktop browser. It uses no external
scripts, fonts, image files, server, or build step.

### Run it from a local web server

From the repository root, run:

```powershell
py -m http.server 8000
```

Then open `http://localhost:8000/edith-sun-galaxy.html`. Stop the server with
Ctrl+C in the terminal.

### Start the animation at a later simulation time

Add a non-zero `t` query parameter to the URL:

```text
http://localhost:8000/edith-sun-galaxy.html?t=20
```

The value initializes the simulation clock in seconds. It starts the Sun and
planet positions at that time; it does not pre-fill the trails.

## Controls

- **Drag on the canvas** to look around. The camera continues its slow drift
  while you drag.
- **Space** pauses or resumes time and trail recording. The camera and star
  field stop their time-driven motion; you can still drag to adjust the camera.
- Resize the browser to resize the canvas. Rendering uses the display pixel
  ratio, capped at 2, to balance sharpness and performance.

## How it works

Everything is in the HTML file: document markup, CSS, and a classic inline
JavaScript script.

1. **Canvas setup:** The script gets the `#c` canvas and its 2D context. The
   resize handler updates its backing resolution and scale. `W`, `H`, and `F`
   are the viewport width, height, and perspective focal length.
2. **Repeatable galaxy generation:** A seeded pseudo-random number generator
   creates the same galaxy on each page load. Gaussian samples distribute
   points around the central bulge/bar, four logarithmic spiral arms, and a
   diffuse disk. Additional arrays hold arm gas, bright young stars, and 1,800
   distant background stars.
3. **Reusable glow sprites:** `sprite()` draws each kind of soft glow into a
   small off-screen canvas once. The renderer reuses these canvases instead of
   creating a new gradient for every star on every frame.
4. **Motion:** `bodies(t)` calculates the Sun's galactic orbit and each
   planet's position relative to the moving Sun. `OMEGA`, `PATTERN`, `TILT`,
   `PLANETS`, and `TRAIL` define the visualization's speeds, orientation,
   exaggerated planet orbits, and recorded trail length.
5. **Camera and projection:** `setCamera()` builds camera basis vectors.
   `project()` transforms a 3D point into screen coordinates with perspective
   projection and skips points behind the camera.
6. **Frame rendering:** `requestAnimationFrame(frame)` advances the simulation
   clock, records recent body positions, clears the canvas, then draws the
   background sky, galaxy gas and stars, orbit, trails, planets, and Sun. The
   `lighter` composite mode blends the luminous scene elements.
7. **Input:** Pointer events update camera yaw and pitch. The Space key toggles
   `paused`; while paused, simulation time and recorded paths stop advancing.

The animation clock is measured in simulation seconds, not Earth seconds. The
frame delta is capped at 50 ms to reduce large jumps after a blocked frame.
`?t=...` provides a convenient initial clock value for demonstrations.

## Use it in another project

### Recommended: embed the standalone page

Copy `edith-sun-galaxy.html` into the other project's static/public files,
preserving the whole file, and embed it in an iframe:

```html
<iframe
  src="/edith-sun-galaxy.html"
  title="The Sun's voyage through the Milky Way"
  style="display:block; width:100%; height:600px; border:0"
></iframe>
```

Change the `src` to match that project's static-file URL. Add
`?t=20` (or another starting time) to the URL if desired. The iframe keeps the
visualizer's CSS and JavaScript isolated from the host application's styles,
variables, and event handlers. For an embedded view, give the iframe a
non-zero height.

For a React, Vue, or similar application, put the HTML in its public/static
directory and render the iframe from a component. This does not require adding
a JavaScript dependency.

### Integrate the Canvas into an existing page

If an iframe is not appropriate, copy the pieces from the HTML into the host
page:

1. Add `<canvas id="c"></canvas>` to the component's markup.
2. Move the visualizer's CSS rules into the host stylesheet. Scope selectors
   such as `canvas`, `html`, `body`, and the overlay IDs to the visualizer's
   container so they do not restyle the rest of the application.
3. Move the inline script into a JavaScript file or the host framework's
   component. Keep the rendering functions and their associated constants and
   data together; the script uses many module-level values.
4. Start the renderer after the canvas has mounted. In a component-based app,
   retain the `requestAnimationFrame` ID and remove event listeners and cancel
   the frame when the component unmounts. The original standalone page starts
   immediately and does not include that component cleanup lifecycle.
5. Scope pointer and keyboard events to the component as appropriate. The
   standalone Space shortcut is registered on `window`, and the resize listener
   is global; avoid duplicate global listeners if multiple instances can mount.

Copying only the canvas element is not enough: the renderer also needs its
styles, JavaScript, and the EDITH/caption/hint markup if those overlays are
wanted. There are no external image assets or packages to copy.

## Customization

The implementation is intentionally kept in one file for easy reuse.

- **Branding and text:** Edit `#brand`, `#hint`, and `#caption` in the body.
- **Colors and page appearance:** Change the CSS custom properties in `:root`
  and the page/canvas rules.
- **Galaxy population:** Adjust the 42,000-star loop, 1,600-gas-cloud loop,
  520-bright-star loop, and 1,800-star `sky` array.
- **Spiral shape:** Change `ARMS`, `ARM_W`, the radius and angle calculations in
  `armPoint()`, and the Gaussian spreads passed to it.
- **Planet motion:** Edit the `PLANETS` entries, `R_SUN`, `OMEGA`, `PATTERN`,
  `TILT`, or `TRAIL`. These are artistic display values, not scale-model
  astronomy data.
- **Camera:** Change the yaw, pitch, and distance passed to `setCamera()` in
  `frame()`, or adjust the pointer sensitivity in the pointer-move handler.
- **Performance:** Reduce the star/cloud counts first if the demo is slow. The
  renderer redraws the procedural scene on every animation frame.

## Troubleshooting

- **Blank page:** Use a current browser with Canvas 2D and JavaScript enabled;
  check the browser developer console for errors.
- **Slow or hot-running device:** Lower the population counts listed above.
  High display pixel ratios and the large number of per-frame star projections
  increase rendering work.
- **Space scrolls the page instead of pausing:** The standalone page prevents
  the default Space action when its key handler receives the event. If embedded
  in an application, check whether another component or keyboard handler
  intercepts it.
- **Can't drag to look around:** Start the drag on the canvas. The pointer
  capture is attached to the canvas so a drag can continue beyond its bounds.
