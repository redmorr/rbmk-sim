# Running the RBMK sim in a browser

The desktop sim runs unmodified in a browser via [pygbag](https://pypi.org/project/pygbag/),
which packages pygame-ce apps to WebAssembly. The build is fully self-hosted:
the deployed page makes **zero** third-party requests.

```
pip install pygbag
py web/build_web.py                 # -> web/dist/rbmk-sim/  (~26 MB)
```

Verify it (needs `pip install playwright`; uses your installed Chrome):

```
py -m http.server 8080 --bind 127.0.0.1 --directory web/dist
py web/probe.py http://127.0.0.1:8080/rbmk-sim/
```

`probe.py` exits 0 only if the app actually started. Expect the title to become
`RBMK reactor simulation` within ~5 s, and a screenshot showing the lattice,
neutrons and live graphs.

## Performance

Measured in headless Chrome, self-driving build (target 1500, then rods frozen
and pumps to 10% to force the void runaway), 960x856 canvas:

| neutrons | browser fps | what is happening |
|---|---|---|
| < 1800 | 47-60 | normal operation, controller holding target |
| 2500 | 43 | voids forming, power climbing |
| 3200 | 28 | runaway under way |
| 4563 | **17** | worst point |
| 6000 | — | explosion threshold: stepping halts |
| after | ~20 | static explosion screen (render-bound, no physics) |

**The dip is short**: about 4 seconds of wall time between 3200 neutrons and
the explosion. Everything up to the climax runs at 45-60 fps, so the deployed
build is comfortable to use.

The cost is the sim, not WebAssembly. With the explosion threshold lifted so
the population pins at the 8000 cap, native desktop manages only 4-20 fps and
the browser 3 fps — roughly a 3x WASM penalty on an already-heavy workload.
`_update_neutrons` is a per-neutron Python loop at 120 steps/s; at 8000
neutrons that is ~1M neutron-updates/s in either runtime.

If the frame rate ever needs to improve, lower `max_neutrons`/
`explosion_threshold` for the web build rather than optimising — but note
those constants shape the scenario, so `headless_test.py` has to stay 5/5.

## Read this before changing anything

Every failure mode below produces **the same grey canvas, with no traceback,
no console error, and no exception**. They are indistinguishable from outside,
and stack — fixing one just reveals the next. That is what makes this fragile.

### 1. Python 3.13, not pygbag's 3.12 default

pygbag's wheel index lists numpy differently per Python target:

| target | numpy wheel | ABI |
|---|---|---|
| `cp312` (default) | `numpy-2.0.2-<abi>-<abi>-pyodide_2024_0_wasm32.whl` | **pyodide** |
| `cp313` | `numpy-2.3.0.dev0-<abi>-<abi>-<api>.whl` | `wasm32_bi_emscripten` |

pygame is always `wasm32_bi_emscripten`. On cp312 the two ABIs are mixed, and
the VM wedges inside `pygame.display.set_mode()` — numpy itself imports and
computes fine, which makes it look like a display bug. Hence `--PYBUILD 3.13`.
The cp313 wheel is also 4.7 MB rather than 12.4 MB.

**If you bump pygbag**, re-check `index-<VER>-cp313.json` for the numpy
filename and ABI tag, and update `CDN_FILES` in `build_web.py`.

### 2. numpy must be named in the entry script

pygbag decides which wheels to preload by **statically scanning the main
script only**. `core.py` imports numpy, `main.py` did not — so numpy was never
preloaded, and the runtime hit an async fetch part-way through a synchronous
import and deadlocked. The build inserts a bare `import numpy` into the entry
file for this reason. Import *position* is irrelevant: preloading happens
before any of the module body runs.

### 3. The display must be created inside the async main

`pygame.display.set_mode()` at module level hangs. `App(Config())` therefore
has to be constructed *inside* the coroutine, not passed as an argument to
`asyncio.run(App(Config()).run())` — that evaluates the constructor first, at
module level, and hangs.

### 4. CDN paths must be root-absolute

The vendored URLs are ESM specifiers. `cdn/0.9.3/` is read as a bare package
name and rejected; `./cdn/0.9.3/` resolves against the *importing module*
(`cdn/vtx.js`), not the page. Only `/rbmk-sim/cdn/0.9.3/` works — which is why
the mount path is baked in at build time and `--mount` must match where the
bundle is actually served.

### 5. There are two independent CDN mechanisms

Vendoring the URLs in `index.html` is only half of it.

- **Runtime assets** (`pythons.js`, `cpython313/main.*`, xterm) come from
  `config.cdn` in `index.html`.
- **Wheels** come from `pep0723.py`'s `PKG_INDEXES`, which `config.cdn` does
  not touch. It defaults to `https://pygame-web.github.io/cdn/`. The override
  is the `PYGPI` environment variable, which the build sets inside the
  vendored `cpythonrc.py`.

Miss the second and the page still silently pulls ~6 MB of wheels from the
public CDN. Confirm with the browser's own resource list:

```js
performance.getEntriesByType('resource').map(r => r.name)
    .filter(n => !n.startsWith(location.origin))   // must be []
```

### 6. Serve on the pygbag default port when using its dev server

`pep0723.py` hardcodes `http://localhost:8000/cdn/` as the dev wheel mirror:

```python
elif platform.window.location.href.startswith("http://localhost:8"):
    rewritecdn = "http://localhost:8000/cdn/"
```

`pygbag --port 8011` therefore serves the page on 8011 while the runtime
fetches wheels from 8000 and fails. Use the default port, or a plain static
server on `127.0.0.1` (which is not `localhost`, so it takes the production
path — the honest way to test a deployment).

## Debugging technique

Ordinary tools do not work here: `print()` goes to pygbag's xterm terminal
(drawn on a canvas, unreadable from JS), and a wedged VM produces no console
error. Two things that do work:

- **`document.title` as a probe.** The build writes `PYERR <traceback>` there
  on any exception. For bisecting a hang, inject marks and watch the title:
  ```python
  def _mark(s):
      import platform
      platform.window.document.title = "MARK " + s
  ```
  Distinguishing a hang from a crash is the whole game — they look identical.
- **Append `#debug`** to the URL to open pygbag's terminal overlay.

Do not trust `canvas.offsetParent` or `getBoundingClientRect()` to decide
whether the app is rendering — pygbag's canvas reports `0x0` and a null
`offsetParent` while painting perfectly fine. Take a screenshot instead.

## What is not in the repo

The three edits in `stage()` are applied to a staged **copy**; `reactor_sim.py`
itself is unchanged, so the desktop entry point stays plain sync code. The
alternative is making the async loop real in `reactor_sim.py` and having the
desktop path call `asyncio.run()` too — one entry path instead of a patched
copy, at the cost of async in code that does not otherwise need it.

`web/build/`, `web/dist/` and `web/.cdn-cache/` are build artifacts and are
gitignored.
