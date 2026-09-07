"""Capture-measure-attest: machine-checked proof that a UI change was actually looked at.

Accessed via: ``dev shot`` (see ``cli._cmd_shot``). Consumed by the agent-tools
``visual-proof-gate`` pre-bash hook, which blocks a ``git commit`` that stages user-visible
files unless a valid attestation exists.

WHY this module exists: the gate's original contract was "any file in the marker dir with a
recent mtime". A bare ``touch`` satisfied it, so an agent that never opened a browser could
pass, and an agent that captured a blank page could pass just as easily. Both happened. This
module removes the agent from the attesting path entirely: the agent asks for a capture, and
only THIS code decides whether the result is evidence and writes the record.

Three properties make the record hard to forge:

1. **The tool captures.** The scroll sweep that triggers reveal-on-scroll content is ours, not
   the caller's. The real-world failure this prevents: a capture taken without scrolling left
   42% of the page blank because ``whileInView`` content never became visible, and the agent
   attested on it in good faith.
2. **The tool judges.** ``measure_png`` / ``verdict_for`` reject a degenerate capture before any
   record is written. Thresholds are grounded in two real captures (see ``THRESHOLDS``).
3. **The record is bound to the work.** It carries the sha256 of the capture bytes AND the
   sha256 of ``git diff --cached``. The gate recomputes the staged hash at commit time, so a
   record cannot be reused for a different change, and editing after capture invalidates it.

Assumptions:
    * Stdlib-only at import time (dev-cli hard rule). PNG decoding is hand-rolled on
      ``zlib`` + ``struct`` rather than Pillow: no third-party import, and it is the only
      image work this CLI needs.
    * The capture backend (``agent-browser``) is invoked as a subprocess and may be absent;
      that is an EXIT_MISSING_DEP condition, not a crash.
    * Attestations are advisory process discipline, not a security boundary. A determined
      agent with shell access can still write the file directly — see README "Residual trust".
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import time
import zlib
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator

ATTEST_VERSION = 2
"""Bumped from 1: what "featureless" means changed, and so did ``worktree_dirty``.

A version-1 record could have been written for a gradient-disguised blank page (the old
exact-equality measure scored one 0% featureless) or with untracked renderer inputs present
(``git diff`` never listed them). Those records are not equivalent to what this version
writes, and a consumer re-measuring the referenced capture would not reproduce their metrics.
Version 2 also records ``repo`` as the resolved repository root rather than the directory the
tool was invoked from, so a v1 consumer comparing that field needs to know which it is holding.
"""
DEFAULT_PROOF_DIR = "~/.cache/agent-tools/visual-proof"

# Grounded in two real captures of the same page, one taken without a scroll sweep and one
# with. Featureless-row COUNT separates them only ~1.4x (68.3% bad vs 48.5% good) because a
# landing page legitimately has wide bands of background. The largest CONTIGUOUS featureless
# band separates them ~6x (42.7% vs 6.9%) and is therefore the primary check.
#
# Known tradeoff: a page whose hero is a full-width pure gradient with no text over it counts
# as featureless there by design - that is the same signal a gradient-disguised blank gives,
# and it cannot be told apart from the pixels alone. Such a page must keep its gradient run
# under max_band_frac or pass --no-attest.
THRESHOLDS = {
    # The discriminator. One long featureless run means a section never rendered, and it
    # separates the real captures ~6x where the whole-page fraction separates them ~1.2x.
    "max_band_frac": 0.15,  # bad=0.427  good=0.069
    # A backstop for the page that is featureless ALL OVER without one long run - ten broken
    # image placeholders interleaved with text, say. Set BELOW the bad capture, so that one
    # fails both checks rather than only the band: every point of slack here is room for a
    # scattered-blank page to pass, and the good capture leaves 0.165 of headroom.
    "max_uniform_frac": 0.65,  # bad=0.683  good=0.485
    "min_height": 200,
    "min_width": 200,
}


class CaptureError(RuntimeError):
    """A capture or measurement failure with a user-actionable message."""


class MissingBackendError(CaptureError):
    """The capture backend is not installed. A distinct type so the CLI maps it to its own
    exit code by CLASS rather than by sniffing substrings out of an error message — a backend
    whose stderr happens to say "not installed" must not be mistaken for a missing backend."""


class UnboundProofError(CaptureError):
    """The staged change could not be identified, so no honest attestation can be written.

    Writing a record with a null binding would silently re-open the reuse hole this module
    exists to close: the gate could match it against any future change.
    """


# A capture larger than this is not a landing page; it is a decompression bomb or a runaway
# infinite-scroll page. Decoding is O(pixels) in interpreted bytecode, so refuse before
# allocating rather than after.
# Measured: ~1.5s for an 8.7M-pixel capture (1440x6036) of pure-Python unfiltering, so this
# ceiling is roughly 7s of CPU - slow but not indistinguishable from a hang. It still admits
# any realistic full-page capture (a 1440-wide page would have to exceed 27,000px tall).
MAX_PIXELS = 40_000_000
MAX_DIMENSION = 60_000


@dataclass(frozen=True)
class CaptureMetrics:
    """Geometry and blankness of a decoded PNG."""

    width: int
    height: int
    uniform_rows: int
    largest_band: int

    @property
    def uniform_frac(self) -> float:
        return self.uniform_rows / self.height if self.height else 1.0

    @property
    def band_frac(self) -> float:
        return self.largest_band / self.height if self.height else 1.0


@dataclass(frozen=True)
class Verdict:
    """Whether a capture is admissible as evidence, and why not when it isn't."""

    ok: bool
    reasons: tuple[str, ...]
    metrics: CaptureMetrics


def _iter_chunks(data: bytes) -> Iterator[tuple[bytes, bytes]]:
    """Yield ``(type, payload)`` for each PNG chunk. Stops at IEND."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise CaptureError("not a PNG file (bad signature)")
    pos = 8
    end = len(data)
    while pos + 8 <= end:
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        yield ctype, payload
        if ctype == b"IEND":
            return
        pos += 12 + length


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> Iterator[bytes]:
    """Reverse the per-scanline PNG filters, yielding raw pixel rows.

    Each row's filter is relative to the row above, so this must run over every row in order
    even when the caller only samples some of them.
    """
    stride = width * bpp
    prev = bytearray(stride)
    pos = 0
    for _ in range(height):
        if pos >= len(raw):
            raise CaptureError("truncated PNG image data")
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos : pos + stride])
        if len(line) < stride:
            raise CaptureError("truncated PNG scanline")
        pos += stride
        if ftype == 1:
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 0xFF
        elif ftype == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif ftype == 3:
            for x in range(stride):
                left = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 0xFF
        elif ftype == 4:
            for x in range(stride):
                left = line[x - bpp] if x >= bpp else 0
                up = prev[x]
                upleft = prev[x - bpp] if x >= bpp else 0
                pa = abs(up - upleft)
                pb = abs(left - upleft)
                pc = abs(left + up - 2 * upleft)
                if pa <= pb and pa <= pc:
                    pred = left
                elif pb <= pc:
                    pred = up
                else:
                    pred = upleft
                line[x] = (line[x] + pred) & 0xFF
        elif ftype != 0:
            raise CaptureError(f"unsupported PNG filter type {ftype}")
        yield bytes(line)
        prev = line


FLAT_DELTA = 6
"""Largest adjacent-pixel jump a row may contain and still count as featureless.

Rendered content - text, borders, card edges - produces abrupt local jumps far above this.
A gradient moves by 0-1 per pixel across a 1440px span, so it stays under it.

Known limit: a border under this contrast - a 1px `#F9FAFB` rule on white is a delta of 6 -
is indistinguishable from a gradient by this measure, so a tall element outlined in a very
light grey can still read as one featureless band. Raising the delta to admit it would start
admitting real gradients, which is the failure this exists to catch; the band threshold is
the safety margin for it.

Meaningful ONLY for 8-bit direct-colour channels, where a byte is a linear intensity. That
is not an assumption - ``measure_png_bytes`` raises on any other bit depth, and on palette
images (where adjacent indices can be white and black), before reaching here.
"""


def _row_is_flat(row: bytes, bpp: int) -> bool:
    """True when the row only ever varies gradually, so it carries no rendered content.

    Exact-equality uniformity misses the case this exists for: a blank page under a CSS
    gradient has no two identical pixels, yet shows nothing. Judging LOCAL change instead of
    global range separates the two - a gradient's neighbours differ by ~1, an edge's by tens.

    EVERY adjacent pair is examined, deliberately. Sampling a subset - even rotating which
    columns are sampled per row - aliases: a 1px rule running diagonally shifts one column per
    row in lockstep with the rotation and evades every probe, so a rendered page reads as one
    long blank band. Any fixed pattern has some content pattern that hides in it.

    Cost is kept off the common path by testing each channel's global range first, at C speed.
    A solid or near-solid row settles there without a per-pixel walk, and a content row exits
    on its first real edge; only a genuine full-width gradient walks far.
    """
    for c in range(bpp):
        chan = row[c::bpp]
        if max(chan) - min(chan) > FLAT_DELTA:
            break
    else:
        return True  # every channel varies less overall than one allowed step
    for c in range(bpp):
        chan = row[c::bpp]
        prev = chan[0]
        for cur in chan:
            if cur - prev > FLAT_DELTA or prev - cur > FLAT_DELTA:
                return False
            prev = cur
    return True


def measure_png(path: str | Path) -> CaptureMetrics:
    """Decode a PNG and report its geometry plus how much of it is featureless.

    A row counts as featureless when it is one pixel repeated, or when it varies only
    gradually (see :func:`_row_is_flat`). ``largest_band`` is the longest run of consecutive
    such rows - the signal that a whole section failed to render, as opposed to ordinary page
    background.
    """
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        # The backend can exit 0 and still leave nothing behind; that is a bad capture, not a
        # crash, and the caller's exit-code contract depends on it arriving as CaptureError.
        raise CaptureError(f"no capture file to measure at {path}: {exc}") from exc
    return measure_png_bytes(data, source=str(path))


def measure_png_bytes(data: bytes, source: str = "<bytes>") -> CaptureMetrics:
    """Measure an in-memory PNG.

    Split out from :func:`measure_png` so a caller can read the file ONCE and both measure and
    hash the same bytes. Re-reading between the two would let a concurrent writer swap the file
    so the record's digest covers bytes the verdict never judged.
    """
    path = source

    width = height = bit_depth = color_type = None
    idat = bytearray()
    try:
        for ctype, payload in _iter_chunks(data):
            if ctype == b"IHDR":
                width, height, bit_depth, color_type = struct.unpack(">IIBB", payload[:10])
                interlace = payload[12] if len(payload) > 12 else 0
                if interlace:
                    raise CaptureError("interlaced PNG is not supported")
            elif ctype == b"IDAT":
                idat += payload
    except struct.error as exc:
        raise CaptureError(f"corrupt PNG header in {path}: {exc}") from exc
    if width is None or height is None:
        raise CaptureError("PNG has no IHDR chunk")
    if not idat:
        raise CaptureError("PNG has no image data")
    if bit_depth != 8:
        raise CaptureError(f"unsupported PNG bit depth {bit_depth} (need 8)")
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        raise CaptureError(f"unsupported PNG colour type {color_type} (palette not supported)")
    if width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_PIXELS:
        raise CaptureError(
            f"capture claims to be {width}x{height}, beyond the {MAX_DIMENSION}px / "
            f"{MAX_PIXELS} pixel limit - refusing to decode it"
        )

    # Bound the INFLATED size, not just the declared dimensions: a harmless-looking 400x400
    # IHDR can carry an IDAT stream that expands to gigabytes, and that passes the dimension
    # check above. A valid image needs exactly height * (1 + width * channels) bytes.
    expected = height * (1 + width * channels)
    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(bytes(idat), expected + 1)
    except zlib.error as exc:
        raise CaptureError(f"corrupt PNG image data in {path}: {exc}") from exc
    if len(raw) > expected:
        raise CaptureError(
            f"PNG image data in {path} inflates beyond the {expected} bytes its "
            f"{width}x{height} header declares - refusing it as a decompression bomb"
        )
    bpp = channels
    uniform_rows = 0
    largest_band = 0
    current_band = 0
    for row in _unfilter(raw, width, height, bpp):
        # Exact repeat first: one C-level bytes compare, cheaper still than the range test,
        # and it settles the ordinary solid-background row immediately.
        if row == row[:bpp] * width or _row_is_flat(row, bpp):
            uniform_rows += 1
            current_band += 1
            if current_band > largest_band:
                largest_band = current_band
        else:
            current_band = 0
    return CaptureMetrics(
        width=width, height=height, uniform_rows=uniform_rows, largest_band=largest_band
    )


def _height_covers(actual: int, reported: int) -> bool:
    """True when the capture plausibly covers the whole page.

    Tolerant on purpose: the page can grow slightly during the sweep (lazy images, sticky
    headers). A device-pixel-ratio backend returns a TALLER image, which this already accepts,
    so no DPR branch is needed here (unlike ``_width_matches``). This is aimed at the gross
    failure — a viewport-height capture of a many-screen page — not at pixel accounting.
    """
    return actual >= reported * 0.9


def _width_matches(actual: int, expected: int) -> bool:
    """True when ``actual`` is the requested CSS width, or an integer device-pixel multiple.

    agent-browser returns CSS pixels here (verified: a 1440 viewport captures at 1440), but a
    backend configured with a device pixel ratio would return 2880 for the same page. Accepting
    small integer multiples keeps the check meaningful without failing closed on every capture
    from a retina-configured backend.
    """
    if actual == expected:
        return True
    return any(actual == expected * dpr for dpr in (2, 3))


def verdict_for(
    metrics: CaptureMetrics,
    expect_width: int | None = None,
    thresholds: dict | None = None,
    expect_height: int | None = None,
) -> Verdict:
    """Judge whether ``metrics`` describe a capture worth attesting on.

    ``expect_height`` is the page's reported ``scrollHeight``. Checking it closes the quietest
    false attestation available: a backend whose full-page capture degrades to viewport-only
    yields a perfectly clean 1440x900 image of a 6000px page — no blank band, no wrong width —
    that would otherwise be attested with most of the page never photographed.
    """
    t = {**THRESHOLDS, **(thresholds or {})}
    reasons: list[str] = []
    if expect_height is not None and not _height_covers(metrics.height, expect_height):
        reasons.append(
            f"capture is {metrics.height}px tall but the page reports {expect_height}px - "
            "the capture is not full-page, so most of it was never photographed"
        )
    if metrics.width < t["min_width"] or metrics.height < t["min_height"]:
        reasons.append(
            f"capture is {metrics.width}x{metrics.height}, smaller than the "
            f"{t['min_width']}x{t['min_height']} minimum - it did not render a page"
        )
    if expect_width is not None and not _width_matches(metrics.width, expect_width):
        reasons.append(
            f"capture is {metrics.width}px wide but {expect_width}px was requested - the "
            "viewport was not applied (a known agent-browser trap: `open --width` is "
            "silently ignored; set the viewport before opening)"
        )
    if metrics.band_frac > t["max_band_frac"]:
        reasons.append(
            f"largest blank band is {metrics.largest_band}px "
            f"({metrics.band_frac:.1%} of the page, limit {t['max_band_frac']:.0%}) - a whole "
            "section did not render, most likely reveal-on-scroll content that was never "
            "scrolled into view"
        )
    if metrics.uniform_frac > t["max_uniform_frac"]:
        reasons.append(
            f"{metrics.uniform_frac:.1%} of rows are featureless "
            f"(limit {t['max_uniform_frac']:.0%}) - the page is essentially blank"
        )
    return Verdict(ok=not reasons, reasons=tuple(reasons), metrics=metrics)


def staged_diff_hash(repo: str | Path, timeout_s: int = 10) -> str | None:
    """sha256 of ``git diff --cached`` for ``repo``; ``None`` when git cannot be queried.

    This is what binds an attestation to one specific change. The gate recomputes it at
    commit time: if the staged content moved on, the old record no longer matches.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached"],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    if not proc.stdout.strip():
        # An EMPTY staged diff hashes to a real, stable value - and "nothing staged" is a
        # repository's default state, so such a record would match at almost any later moment.
        # In particular `git commit -am ...` stages AFTER this gate runs, so the gate would
        # recompute the same empty-diff hash and pass a change nobody captured. Treat it as
        # unbound, exactly like a git failure.
        return None
    return hashlib.sha256(proc.stdout).hexdigest()


def _git_toplevel(repo: str | Path, timeout_s: int = 10) -> Path | None:
    """Resolved repository root for ``repo``, or None when git cannot answer."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    root = proc.stdout.decode("utf-8", "replace").strip()
    if not root:
        return None
    try:
        return Path(root).resolve()
    except OSError:
        return None


def _worktree_dirty(repo: str | Path, timeout_s: int = 10) -> bool | None:
    """True when unstaged changes exist, ``None`` when git cannot be asked.

    Counts UNTRACKED files too. ``git diff`` never lists them, so a page rendered from a
    brand-new asset or generated config - the common case when adding an image - would
    otherwise record ``worktree_dirty: false`` while the staged commit lacks the very file
    the capture shows.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=normal"],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    # Porcelain v1 is "XY path": X is the index status, Y the worktree's. A line is only
    # STAGED-clean when Y is a space, so anything else - modification or "??" - is dirt.
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        if len(line) >= 2 and line[1] != " ":
            return True
    return False


def proof_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get("VISUAL_PROOF_DIR", DEFAULT_PROOF_DIR)))


def write_attestation(
    capture: str | Path,
    verdict: Verdict,
    repo: str | Path,
    url: str,
    directory: Path | None = None,
    capture_sha256: str | None = None,
) -> Path:
    """Record that THIS tool captured and cleared ``capture``.

    Takes a :class:`Verdict`, not bare metrics, and refuses a failed one. The refusal is the
    point: if this function accepted metrics and trusted its caller to have checked, then any
    importer — a future subcommand, a batch re-attest script, a test helper promoted to
    production — could record proof for a blank page, which is the exact failure this module
    exists to prevent. Keeping the check here makes the invariant structural rather than a
    comment.

    The filename carries the capture digest for uniqueness only; the gate matches on the
    record BODY (``staged_sha256`` plus a re-hash of ``capture_path``), so renaming a record
    cannot make it attest to something else.

    Pass ``capture_sha256`` — the digest of the SAME bytes that were measured — so the record
    cannot end up describing a file that a concurrent writer swapped in after measurement.
    Omitting it re-reads the file, which is fine for tests but races in production.
    """
    if not verdict.ok:
        raise CaptureError(
            "refusing to attest a capture that failed measurement: " + "; ".join(verdict.reasons)
        )
    metrics = verdict.metrics
    target_dir = directory or proof_dir()
    capture_path = Path(capture).resolve()
    staged = staged_diff_hash(repo)
    if staged is None:
        # Refuse rather than write `"staged_sha256": null`: an unbound record would match any
        # future change, which is precisely the reuse the binding exists to prevent.
        raise UnboundProofError(
            f"cannot read `git diff --cached` in {repo} - the capture is fine, but proof "
            "cannot be bound to a staged change. Stage the change first, and run from inside "
            "the repository."
        )
    repo_root = _git_toplevel(repo)
    if repo_root is None:
        # Falling back to the raw invocation directory would silently reintroduce the very
        # mismatch this records the root to avoid - and intermittently, since git only fails
        # here on timeout or error. A record with the wrong repo is worthless; say so now.
        raise UnboundProofError(
            f"cannot resolve the repository root of {repo} - the capture is fine, but a proof "
            "record naming the wrong directory would be rejected later with no explanation."
        )
    target_dir.mkdir(parents=True, exist_ok=True)
    digest = capture_sha256 or hashlib.sha256(capture_path.read_bytes()).hexdigest()
    record = {
        "version": ATTEST_VERSION,
        "tool": "dev shot",
        "captured_at": int(time.time()),
        "capture_path": str(capture_path),
        "capture_sha256": digest,
        "staged_sha256": staged,
        # The binding is to the INDEX, but the browser rendered the WORKING TREE. Unstaged
        # edits present at capture time are invisible to the binding, so record whether any
        # existed and let the gate decide what to do about it.
        "worktree_dirty": _worktree_dirty(repo),
        # The REPOSITORY ROOT, not whatever directory this ran from. The gate resolves its own
        # side the same way, so a `git commit` issued from a subdirectory still matches;
        # recording the raw cwd would make an otherwise valid record unusable from anywhere
        # but the root.
        "repo": str(repo_root),
        "url": url,
        "metrics": asdict(metrics),
    }
    stamp = f"{record['captured_at']}-{digest[:12]}"
    out = target_dir / f"attest-{stamp}.json"
    out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def capture_with_sweep(
    url: str,
    out_path: str | Path,
    width: int,
    height: int,
    sweep_step: int = 400,
    settle_ms: int = 250,
    timeout_s: int = 180,
    runner=subprocess.run,
) -> CaptureContext:
    """Drive agent-browser: size the viewport, open, sweep the page, then capture full-page.

    The sweep is the point. Reveal-on-scroll sections stay at ``opacity: 0`` until they enter
    the viewport, and a naive full-page capture photographs them while still invisible. We
    scroll down in ``sweep_step`` increments to the bottom and back to the top, so every
    section has been in view before the shutter.

    ``runner`` is injected so tests exercise the command sequence without a browser.
    """
    out_path = Path(out_path)
    if width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_PIXELS:
        # Refuse BEFORE the backend allocates: a browser asked for a 10^6-square viewport can
        # hang or die long before we would have rejected the resulting PNG.
        raise CaptureError(
            f"viewport {width}x{height} exceeds the {MAX_DIMENSION}px / {MAX_PIXELS} pixel limit"
        )
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CaptureError(f"cannot create the output directory for {out_path}: {exc}") from exc

    def run(args: list[str], what: str) -> None:
        try:
            proc = runner(args, capture_output=True, timeout=timeout_s, check=False)
        except FileNotFoundError as exc:
            raise MissingBackendError(
                "agent-browser is not installed or not on PATH "
                "(install: npm i -g agent-browser && agent-browser install)"
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise CaptureError(f"{what} failed: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise CaptureError(f"{what} failed (exit {proc.returncode}): {detail}")

    # Viewport BEFORE open: `open --width/--height` is silently ignored by agent-browser, so
    # sizing afterwards would capture at the default width while reporting success.
    run(["agent-browser", "set", "viewport", str(width), str(height)], "setting the viewport")
    run(["agent-browser", "open", url], f"opening {url}")
    run(["agent-browser", "wait", "--load", "networkidle"], "waiting for network idle")

    page_height = _page_height(runner, timeout_s)
    steps = _sweep_count(page_height or _SWEEP_MAX, sweep_step)
    for _ in range(steps):
        run(["agent-browser", "scroll", "down", str(sweep_step)], "scrolling down")
    run(["agent-browser", "wait", str(settle_ms)], "settling after the sweep")
    run(["agent-browser", "scroll", "up", str(_SWEEP_MAX)], "scrolling back to the top")
    run(["agent-browser", "wait", str(settle_ms)], "settling at the top")
    # Read the location the browser is ACTUALLY on, immediately before the shutter. The
    # backend is a shared, stateful session: a redirect, an SPA navigation, or a concurrent
    # `dev shot` can move it, and recording the requested URL would then be a false claim.
    actual_url = _current_url(runner, timeout_s)
    run(["agent-browser", "screenshot", "--full", str(out_path)], "capturing the screenshot")
    return CaptureContext(page_height=page_height, url=actual_url)


@dataclass(frozen=True)
class CaptureContext:
    """What the browser reported around the shutter, for the verdict and the record."""

    page_height: int | None
    url: str | None  # None when the browser could not be asked - recorded as unknown, never
    # substituted with the requested URL, which would assert something we did not verify.


def _current_url(runner, timeout_s: int) -> str | None:
    try:
        proc = runner(
            ["agent-browser", "get", "url"], capture_output=True, timeout=timeout_s, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    text = (proc.stdout or b"").decode("utf-8", "replace").strip().strip('"')
    return text or None


def _page_height(runner, timeout_s: int) -> int | None:
    """Document height in CSS pixels, or ``None`` when it cannot be read.

    Without this the sweep issues its worst-case step count on every capture — 100 scroll
    subprocesses for a 2000px page, each costing process spawn plus IPC.
    """
    try:
        proc = runner(
            ["agent-browser", "eval", "document.documentElement.scrollHeight"],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    text = (proc.stdout or b"").decode("utf-8", "replace").strip().strip('"')
    try:
        value = int(float(text))
    except ValueError:
        return None
    return value if 0 < value <= _SWEEP_MAX else None


# A page taller than this is unusual for a landing page; the sweep is bounded so a runaway
# infinite-scroll page cannot spin forever.
_SWEEP_MAX = 40000


def _sweep_count(page_height: int, step: int) -> int:
    """Steps needed to reach the bottom, plus one so the last section is fully in view."""
    return max(1, min(page_height // max(step, 1) + 1, _SWEEP_MAX // max(step, 1)))
