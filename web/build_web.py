"""Build the RBMK sim for the browser: pygbag -> WASM, with the pygame-web CDN
vendored so the deployed page makes no third-party requests.

    pip install pygbag
    py web/build_web.py [--mount /rbmk-sim] [--skip-download]

Output lands in web/dist/<mount>/ ready to copy into a static site. See
web/README.md for why each non-obvious step below exists -- every one of them
was a silent grey-screen failure with no traceback.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CDN = "https://pygame-web.github.io/cdn"
VER = "0.9.3"

# Python 3.13, NOT pygbag's 3.12 default. cp312's numpy is a pyodide-ABI wheel
# while pygame is wasm32_bi_emscripten; loading both wedges the VM inside
# pygame.display.set_mode(). cp313 ships numpy built for the matching ABI
# (and it is 4.7MB rather than 12.4MB).
PYB, TAG = "3.13", "cp313"

# Everything the runtime fetches, found by watching the request log of a
# working run. Wheel filenames come from index-<VER>-<TAG>.json, where the
# <abi>/<api> placeholders resolve to cp313 / wasm32_bi_emscripten.
CDN_FILES = [
    f"{VER}/pythons.js", f"{VER}/empty.html", f"{VER}/cpythonrc.py",
    f"{VER}/cpython313/main.js", f"{VER}/cpython313/main.data",
    f"{VER}/cpython313/main.wasm",
    "vtx.js", "vt/xterm.css", "vt/xterm.js", "vt/xterm-addon-image.js",
    f"index-{VER}-{TAG}.json",
    f"{TAG}/pygame_ce-2.5.7-{TAG}-{TAG}-wasm32_bi_emscripten.whl",
    f"{TAG}/numpy-2.3.0.dev0-{TAG}-{TAG}-wasm32_bi_emscripten.whl",
]


def stage(src: Path) -> None:
    """Copy the sim into a build dir, adapting the entrypoint for pygbag.

    The desktop sources are left untouched; the three edits below are exactly
    what pygbag requires and nothing more.
    """
    src.mkdir(parents=True, exist_ok=True)
    for name in ("config.py", "core.py"):
        shutil.copy(REPO / name, src / name)
    s = (REPO / "reactor_sim.py").read_text(encoding="utf-8")

    # 1. the frame loop must yield to the browser every frame
    s = s.replace("import textwrap\n", "import asyncio\nimport textwrap\n", 1)
    s = s.replace("    def run(self) -> None:", "    async def run(self) -> None:", 1)
    s = s.replace("            self.render(min(1.0, self._acc * self.cfg.physics_hz))\n",
                  "            self.render(min(1.0, self._acc * self.cfg.physics_hz))\n"
                  "            await asyncio.sleep(0)\n", 1)

    # 2. pygbag picks wheels to preload by scanning THIS file only. numpy is
    #    imported by core.py, so without naming it here the runtime hits an
    #    async fetch part-way through the import and deadlocks.
    s = s.replace("import pygame\n",
                  "import numpy  # noqa: F401  (pygbag preload hint, see web/README.md)\n"
                  "import pygame\n", 1)

    # 3. the display must be created inside the async main -- a module-level
    #    set_mode() hangs before the first paint. Any exception is surfaced in
    #    document.title because a WASM crash otherwise leaves only a grey canvas.
    s = s.replace('if __name__ == "__main__":\n    App(Config()).run()', """async def _web_main():
    app = App(Config())
    await app.run()


try:
    asyncio.run(_web_main())
except BaseException:
    import traceback
    err = traceback.format_exc()
    print(err)
    try:
        import platform
        platform.window.document.title = "PYERR " + err[-600:]
    except Exception:
        pass
    raise""")
    assert "async def run" in s and "asyncio.run(_web_main())" in s, "entry patch did not apply"
    (src / "main.py").write_text(s, encoding="utf-8")


def screen_size() -> tuple[int, int]:
    """Read the window size from the sim itself, so the canvas cannot drift."""
    sys.path.insert(0, str(REPO))
    from config import Config
    from reactor_sim import HUD_H, PANEL_H
    cfg = Config()
    return cfg.grid_w * cfg.cell_px, HUD_H + cfg.grid_h * cfg.cell_px + PANEL_H


def download(dest: Path) -> None:
    for rel in CDN_FILES:
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            continue
        print(f"  fetch {rel}")
        urllib.request.urlretrieve(f"{CDN}/{rel}", out)


def patch(site: Path, mount: str) -> None:
    idx = site / "index.html"
    s = idx.read_text(encoding="utf-8")
    # Root-absolute, never relative: these are ESM specifiers. A bare "cdn/..."
    # is read as a package name, and "./cdn/..." resolves against the importing
    # module (cdn/vtx.js) rather than the page.
    n = s.count(f"{CDN}/{VER}/")
    s = s.replace(f"{CDN}/{VER}/", f"{mount}/cdn/{VER}/")
    idx.write_text(s, encoding="utf-8")
    assert "pygame-web.github.io" not in s, "an external CDN url survived"

    # Wheels resolve through a SECOND, independent mechanism: pep0723's
    # PKG_INDEXES, which config.cdn does not touch. PYGPI overrides both the
    # package index and the wheel base.
    rc = site / "cdn" / VER / "cpythonrc.py"
    t = rc.read_text(encoding="utf-8")
    anchor = 'PYCONFIG_PKG_INDEXES_DEV = ["http://localhost:<port>/cdn/"]'
    assert anchor in t, "cpythonrc anchor missing -- did pygbag change version?"
    t = t.replace(anchor, "import os as _os  # vendored: keep wheels on this origin\n"
                          f'_os.environ.setdefault("PYGPI", "{mount}/cdn/")\n\n' + anchor, 1)
    rc.write_text(t, encoding="utf-8")
    print(f"  index.html: {n} urls -> {mount}/cdn/{VER}/;  PYGPI -> {mount}/cdn/")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mount", default="/rbmk-sim",
                    help="absolute site path the bundle will be served from; "
                         "it is baked into the build (default: %(default)s)")
    ap.add_argument("--skip-download", action="store_true",
                    help="reuse web/.cdn-cache instead of re-fetching")
    args = ap.parse_args()
    mount = "/" + args.mount.strip("/")

    # the staging dir name becomes the archive name and the app title
    src = HERE / "build" / mount.lstrip("/")
    cache = HERE / ".cdn-cache"
    site = HERE / "dist" / mount.lstrip("/")

    w, h = screen_size()
    print(f"== staging (canvas {w}x{h}) ==")
    shutil.rmtree(src, ignore_errors=True)
    stage(src)

    print("== pygbag build ==")
    subprocess.run([sys.executable, "-m", "pygbag", "--build", "--ume_block", "0",
                    "--PYBUILD", PYB, "--width", str(w), "--height", str(h), str(src)],
                   check=True)

    if not args.skip_download:
        print("== vendoring cdn ==")
        download(cache)

    print("== assembling ==")
    shutil.rmtree(site, ignore_errors=True)
    site.mkdir(parents=True)
    for f in (src / "build" / "web").iterdir():
        if f.is_file():
            shutil.copy(f, site / f.name)
    shutil.copytree(cache, site / "cdn")
    patch(site, mount)
    print(f"\nready: {site}")
    print(f"verify: py -m http.server 8080 --bind 127.0.0.1 --directory {site.parent}")
    print(f"        py web/probe.py http://127.0.0.1:8080{mount}/")


if __name__ == "__main__":
    main()
