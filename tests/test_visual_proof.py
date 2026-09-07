"""Tests for ``dev shot`` and the visual-proof attestation module.

Run from the repo root::

    python -m pytest tests/test_visual_proof.py -q

The headline cases are ``test_rejects_the_real_unswept_capture`` and
``test_accepts_the_real_swept_capture``: two REAL captures of the same page, one taken
without a scroll sweep (42% of it a single blank band) and one with. They are the reason the
thresholds are what they are, and they guard against a future "tidy-up" that loosens the
band check into uselessness. The fixtures are COMMITTED (600px slices that preserve the
metrics exactly) rather than read from a scratch directory: AGENTS.md makes re-running these
the precondition for touching THRESHOLDS, and a fixture that skipped on every machine but one
would make that rule unenforceable.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dev_cli import visual_proof  # noqa: E402
from dev_cli.visual_proof import (  # noqa: E402
    CaptureError,
    CaptureMetrics,
    capture_with_sweep,
    measure_png,
    verdict_for,
    write_attestation,
)

# Committed 600px-wide, every-4th-row slices of two REAL captures of the same page. The slice
# preserves the metrics that matter exactly (band 42.9% bad / 6.9% good, same as full-res) at
# ~100KB each. They live in the repo rather than a scratch directory ON PURPOSE: AGENTS.md
# makes "re-run the two real-capture tests" the precondition for touching THRESHOLDS, and a
# fixture that silently skips on every machine but one would make that rule unenforceable.
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_BAD_CAPTURE = _FIXTURES / "capture-unswept-bad.png"
_GOOD_CAPTURE = _FIXTURES / "capture-swept-good.png"


def _png(width: int, height: int, row_colour) -> bytes:
    """Build a minimal 8-bit RGB PNG. ``row_colour(y)`` returns an (r, g, b) tuple."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (None)
        raw.extend(bytes(row_colour(y)) * width)
    return _assemble(width, height, bytes(raw))


def _assemble(width: int, height: int, raw: bytes) -> bytes:
    def chunk(ctype: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + ctype
            + payload
            + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


# --- measurement ---------------------------------------------------------------------


def test_all_blank_page_is_fully_uniform(tmp_path: Path) -> None:
    p = tmp_path / "blank.png"
    p.write_bytes(_png(400, 400, lambda y: (255, 255, 255)))
    m = measure_png(p)
    assert m.width == 400 and m.height == 400
    assert m.uniform_rows == 400
    assert m.largest_band == 400


def test_textured_page_has_no_uniform_rows(tmp_path: Path) -> None:
    p = tmp_path / "noise.png"
    p.write_bytes(_png(400, 400, lambda y: (y % 251, (y * 7) % 251, (y * 13) % 251)))
    # every row is a solid colour -> uniform. Use a per-pixel varying row instead. The stride
    # must be LARGE: featureless is judged by adjacent-pixel change, so a +1-per-pixel ramp is
    # a gradient (correctly flat), not the content this test needs.
    raw = bytearray()
    for y in range(400):
        raw.append(0)
        for x in range(400):
            raw.extend(bytes(((x * 127) % 256, (y * 131) % 256, (x * 89 + y) % 256)))
    p.write_bytes(_assemble(400, 400, bytes(raw)))
    m = measure_png(p)
    assert m.uniform_rows == 0
    assert m.largest_band == 0


def test_largest_band_measures_the_longest_run_not_the_total(tmp_path: Path) -> None:
    """Two short blank runs must not read as one long one - that distinction IS the check."""
    raw = bytearray()
    for y in range(300):
        raw.append(0)
        blank = (0 <= y < 60) or (200 <= y < 260)  # two 60px runs, 120px uniform total
        if blank:
            raw.extend(bytes((255, 255, 255)) * 300)
        else:
            for x in range(300):
                raw.extend(bytes((x % 251, y % 251, (x * y) % 251)))
    p = tmp_path / "bands.png"
    p.write_bytes(_assemble(300, 300, bytes(raw)))
    m = measure_png(p)
    assert m.uniform_rows == 120
    assert m.largest_band == 60


def _encode_with_filter(width: int, height: int, pixels, ftype: int) -> bytes:
    """Encode ``pixels`` (a (y, x) -> (r,g,b) callable) using ONE PNG filter type throughout."""
    stride = width * 3
    raw = bytearray()
    prev = bytearray(stride)
    for y in range(height):
        line = bytearray()
        for x in range(width):
            line.extend(bytes(pixels(y, x)))
        enc = bytearray(stride)
        for i in range(stride):
            left = line[i - 3] if i >= 3 else 0
            up = prev[i]
            upleft = prev[i - 3] if i >= 3 else 0
            if ftype == 0:
                pred = 0
            elif ftype == 1:
                pred = left
            elif ftype == 2:
                pred = up
            elif ftype == 3:
                pred = (left + up) >> 1
            else:
                pa, pb, pc = abs(up - upleft), abs(left - upleft), abs(left + up - 2 * upleft)
                pred = left if (pa <= pb and pa <= pc) else (up if pb <= pc else upleft)
            enc[i] = (line[i] - pred) & 0xFF
        raw.append(ftype)
        raw.extend(enc)
        prev = line
    return _assemble(width, height, bytes(raw))


@pytest.mark.parametrize("ftype", [0, 1, 2, 3, 4])
def test_every_png_filter_type_reconstructs_identically(tmp_path: Path, ftype: int) -> None:
    """A wrong unfilter turns uniform rows into textured ones - it fails OPEN, passing a
    blank capture. Every filter must reconstruct the same image, or that guard is hollow."""

    def pixels(y: int, x: int):
        # Stride by ~half the channel range so ADJACENT pixels jump hard. Featureless is judged
        # by LOCAL change now, so a gentle ramp is correctly flat and cannot stand in for
        # content here.
        return (255, 255, 255) if 40 <= y < 90 else ((x * 127) % 256, (y * 131) % 256, 0)

    p = tmp_path / f"f{ftype}.png"
    p.write_bytes(_encode_with_filter(60, 120, pixels, ftype))
    m = measure_png(p)
    assert (m.width, m.height) == (60, 120)
    assert m.uniform_rows == 50, f"filter {ftype} reconstructed the blank band wrong"
    assert m.largest_band == 50, f"filter {ftype} reconstructed the blank band wrong"


def test_rejects_a_non_png(tmp_path: Path) -> None:
    p = tmp_path / "not.png"
    p.write_bytes(b"definitely not a png")
    with pytest.raises(CaptureError, match="bad signature"):
        measure_png(p)


# --- verdict -------------------------------------------------------------------------


def _metrics(width=1440, height=1000, uniform=0, band=0) -> CaptureMetrics:
    return CaptureMetrics(width=width, height=height, uniform_rows=uniform, largest_band=band)


def _ctx(page_height=None, url="http://localhost/") -> visual_proof.CaptureContext:
    return visual_proof.CaptureContext(page_height=page_height, url=url)


def _ok_verdict(**kw) -> visual_proof.Verdict:
    return visual_proof.Verdict(ok=True, reasons=(), metrics=_metrics(**kw))


def test_wrong_viewport_width_is_rejected() -> None:
    v = verdict_for(_metrics(width=1280), expect_width=1440)
    assert not v.ok
    assert any("1280px wide but 1440px was requested" in r for r in v.reasons)


def test_large_blank_band_is_rejected() -> None:
    v = verdict_for(_metrics(height=1000, uniform=500, band=400), expect_width=1440)
    assert not v.ok
    assert any("largest blank band" in r for r in v.reasons)


def test_ordinary_page_whitespace_is_accepted() -> None:
    """~20% featureless rows in short runs is a normal landing page, not a failure."""
    v = verdict_for(_metrics(height=6377, uniform=1241, band=264), expect_width=1440)
    assert v.ok, v.reasons


def test_tiny_capture_is_rejected() -> None:
    v = verdict_for(_metrics(width=100, height=100))
    assert not v.ok
    assert any("did not render a page" in r for r in v.reasons)


def test_viewport_only_capture_of_a_tall_page_is_rejected() -> None:
    """The quietest false attestation: a clean 900px shot of a 6000px page, nothing blank."""
    v = verdict_for(_metrics(width=1440, height=900), expect_width=1440, expect_height=6000)
    assert not v.ok
    assert any("not full-page" in r for r in v.reasons)


def test_full_page_capture_satisfies_the_height_check() -> None:
    v = verdict_for(_metrics(width=1440, height=6036), expect_width=1440, expect_height=6000)
    assert v.ok, v.reasons


def test_height_check_is_skipped_when_the_page_height_is_unknown() -> None:
    """`_page_height` returning None must not fail the capture closed."""
    v = verdict_for(_metrics(width=1440, height=900), expect_width=1440, expect_height=None)
    assert v.ok, v.reasons


def test_unknown_url_is_recorded_as_unknown_not_as_the_requested_one() -> None:
    """Substituting the requested URL would assert a page we never confirmed was loaded."""
    import tempfile

    def runner(args, capture_output, timeout, check):  # noqa: ARG001
        if args[1:3] == ["get", "url"]:
            return subprocess.CompletedProcess(args, 1, b"", b"unsupported")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    with tempfile.TemporaryDirectory() as d:
        ctx = capture_with_sweep(
            "https://example.com/", Path(d) / "o.png", 1440, 900, sweep_step=20000, runner=runner
        )
    assert ctx.url is None


def test_capture_records_the_url_the_browser_is_actually_on() -> None:
    """A redirect or a concurrent shot can move a shared session; record what is real."""
    import tempfile

    def runner(args, capture_output, timeout, check):  # noqa: ARG001
        if args[1:3] == ["get", "url"]:
            return subprocess.CompletedProcess(args, 0, b"https://example.com/redirected", b"")
        if args[1] == "eval":
            return subprocess.CompletedProcess(args, 0, b"2000", b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    with tempfile.TemporaryDirectory() as d:
        ctx = capture_with_sweep(
            "https://example.com/", Path(d) / "o.png", 1440, 900, sweep_step=1000, runner=runner
        )
    assert ctx.url == "https://example.com/redirected"
    assert ctx.page_height == 2000


# --- the real captures (the whole point) ----------------------------------------------


def test_write_attestation_refuses_a_failed_verdict(tmp_path: Path, monkeypatch) -> None:
    """The invariant must be structural: no importer may record proof for a bad capture."""
    capture = tmp_path / "shot.png"
    capture.write_bytes(_png(400, 400, lambda y: (1, 2, 3)))
    monkeypatch.setattr(visual_proof, "staged_diff_hash", lambda *a, **k: "abc")
    monkeypatch.setattr(visual_proof, "_git_toplevel", lambda *a, **k: tmp_path)
    bad = visual_proof.Verdict(
        ok=False, reasons=("largest blank band is 4000px",), metrics=_metrics()
    )
    with pytest.raises(CaptureError, match="refusing to attest"):
        write_attestation(capture, bad, tmp_path, "http://localhost/", directory=tmp_path / "proof")
    assert not (tmp_path / "proof").exists()


def test_the_real_fixtures_are_committed() -> None:
    """If these ever vanish, the threshold guard below silently stops guarding anything."""
    assert _BAD_CAPTURE.is_file(), f"missing committed fixture {_BAD_CAPTURE}"
    assert _GOOD_CAPTURE.is_file(), f"missing committed fixture {_GOOD_CAPTURE}"


def test_rejects_the_real_unswept_capture() -> None:
    m = measure_png(_BAD_CAPTURE)
    v = verdict_for(m, expect_width=m.width)
    assert not v.ok, "the unswept capture must never be admissible as proof"
    assert any("largest blank band" in r for r in v.reasons)


def test_accepts_the_real_swept_capture() -> None:
    m = measure_png(_GOOD_CAPTURE)
    v = verdict_for(m, expect_width=m.width)
    assert v.ok, f"the swept capture must be admissible; got {v.reasons}"


def test_the_two_real_captures_are_separated_with_margin() -> None:
    """Guards the threshold itself: a future tweak must keep real headroom on both sides."""
    bad = measure_png(_BAD_CAPTURE)
    good = measure_png(_GOOD_CAPTURE)
    limit = visual_proof.THRESHOLDS["max_band_frac"]
    assert good.band_frac < limit / 2, "good capture should clear the limit by 2x"
    assert bad.band_frac > limit * 2, "bad capture should fail the limit by 2x"


# --- attestation ---------------------------------------------------------------------


def test_attestation_binds_capture_and_staged_hashes(tmp_path: Path, monkeypatch) -> None:
    capture = tmp_path / "shot.png"
    capture.write_bytes(_png(400, 400, lambda y: (1, 2, 3)))
    monkeypatch.setattr(visual_proof, "staged_diff_hash", lambda *a, **k: "deadbeef")
    monkeypatch.setattr(visual_proof, "_git_toplevel", lambda *a, **k: tmp_path)
    out = write_attestation(
        capture, _ok_verdict(), tmp_path, "http://localhost/", directory=tmp_path / "proof"
    )
    record = json.loads(out.read_text())
    assert record["staged_sha256"] == "deadbeef"
    assert (
        record["capture_sha256"] == __import__("hashlib").sha256(capture.read_bytes()).hexdigest()
    )
    assert record["tool"] == "dev shot"
    assert record["version"] == visual_proof.ATTEST_VERSION


def test_staged_hash_changes_when_staged_content_changes(tmp_path: Path, monkeypatch) -> None:
    """Editing after capture must invalidate the proof - that is the anti-reuse property."""
    calls = {"n": 0}

    def fake_run(cmd, capture_output, timeout, check):  # noqa: ARG001
        calls["n"] += 1
        payload = b"diff one" if calls["n"] == 1 else b"diff two"
        return subprocess.CompletedProcess(cmd, 0, payload, b"")

    monkeypatch.setattr(visual_proof.subprocess, "run", fake_run)
    first = visual_proof.staged_diff_hash(tmp_path)
    second = visual_proof.staged_diff_hash(tmp_path)
    assert first != second


def test_staged_hash_is_none_when_git_fails(tmp_path: Path, monkeypatch) -> None:
    def fake_run(cmd, capture_output, timeout, check):  # noqa: ARG001
        return subprocess.CompletedProcess(cmd, 128, b"", b"not a repo")

    monkeypatch.setattr(visual_proof.subprocess, "run", fake_run)
    assert visual_proof.staged_diff_hash(tmp_path) is None


# --- capture orchestration ------------------------------------------------------------


def test_capture_sets_viewport_before_opening(tmp_path: Path) -> None:
    """`open --width` is silently ignored by agent-browser, so ordering is load-bearing."""
    seen: list[list[str]] = []

    def runner(args, capture_output, timeout, check):  # noqa: ARG001
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    capture_with_sweep(
        "http://localhost/", tmp_path / "o.png", 1440, 900, sweep_step=20000, runner=runner
    )
    joined = [" ".join(a) for a in seen]
    viewport_at = next(i for i, c in enumerate(joined) if "set viewport" in c)
    open_at = next(i for i, c in enumerate(joined) if c.startswith("agent-browser open"))
    assert viewport_at < open_at
    assert "agent-browser set viewport 1440 900" == joined[viewport_at]


def test_capture_sweeps_and_returns_to_top(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def runner(args, capture_output, timeout, check):  # noqa: ARG001
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    capture_with_sweep(
        "http://localhost/", tmp_path / "o.png", 1440, 900, sweep_step=20000, runner=runner
    )
    joined = [" ".join(a) for a in seen]
    assert any("scroll down" in c for c in joined), "must scroll to trigger reveal content"
    assert any("scroll up" in c for c in joined), "must return to the top before capturing"
    assert joined[-1].startswith("agent-browser screenshot --full")


def test_missing_agent_browser_is_a_clear_error(tmp_path: Path) -> None:
    def runner(args, capture_output, timeout, check):  # noqa: ARG001
        raise FileNotFoundError("agent-browser")

    with pytest.raises(CaptureError, match="not installed or not on PATH"):
        capture_with_sweep("http://localhost/", tmp_path / "o.png", 1440, 900, runner=runner)


def test_capture_failure_surfaces_stderr(tmp_path: Path) -> None:
    def runner(args, capture_output, timeout, check):  # noqa: ARG001
        return subprocess.CompletedProcess(args, 1, b"", b"connection refused")

    with pytest.raises(CaptureError, match="connection refused"):
        capture_with_sweep("http://localhost/", tmp_path / "o.png", 1440, 900, runner=runner)


def test_backend_stderr_mentioning_not_installed_is_not_a_missing_backend(tmp_path: Path) -> None:
    """Exit-code routing keys off the exception TYPE, never a substring of the message."""

    def runner(args, capture_output, timeout, check):  # noqa: ARG001
        if args[1] == "screenshot":
            return subprocess.CompletedProcess(args, 1, b"", b"chromium is not installed")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    with pytest.raises(CaptureError) as caught:
        capture_with_sweep("http://localhost/", tmp_path / "o.png", 1440, 900, runner=runner)
    assert not isinstance(caught.value, visual_proof.MissingBackendError)


def test_sweep_scales_to_the_page_height(tmp_path: Path) -> None:
    """A short page must not pay for a full-height sweep."""
    scrolls: list[int] = []

    def runner(args, capture_output, timeout, check):  # noqa: ARG001
        if args[1] == "eval":
            return subprocess.CompletedProcess(args, 0, b"2000", b"")
        if args[1:3] == ["scroll", "down"]:
            scrolls.append(1)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    capture_with_sweep(
        "http://localhost/", tmp_path / "o.png", 1440, 900, sweep_step=400, runner=runner
    )
    assert len(scrolls) == 6, f"2000px / 400px + 1 == 6, got {len(scrolls)}"


def test_sweep_falls_back_to_full_range_when_height_unreadable(tmp_path: Path) -> None:
    scrolls: list[int] = []

    def runner(args, capture_output, timeout, check):  # noqa: ARG001
        if args[1] == "eval":
            return subprocess.CompletedProcess(args, 1, b"", b"nope")
        if args[1:3] == ["scroll", "down"]:
            scrolls.append(1)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    capture_with_sweep(
        "http://localhost/", tmp_path / "o.png", 1440, 900, sweep_step=20000, runner=runner
    )
    assert len(scrolls) >= 2, "an unreadable height must sweep the full bounded range"


# --- malformed captures must be CaptureError, not tracebacks --------------------------


def test_missing_capture_file_is_a_capture_error(tmp_path: Path) -> None:
    with pytest.raises(CaptureError, match="no capture file"):
        measure_png(tmp_path / "never-written.png")


def test_corrupt_image_data_is_a_capture_error(tmp_path: Path) -> None:
    """A backend killed mid-write leaves a valid signature over a broken IDAT."""
    good = _png(64, 64, lambda y: (1, 2, 3))
    idat_at = good.index(b"IDAT")
    broken = bytearray(good)
    broken[idat_at + 8 : idat_at + 16] = b"\x00" * 8  # scramble the deflate stream
    p = tmp_path / "corrupt.png"
    p.write_bytes(bytes(broken))
    with pytest.raises(CaptureError, match="corrupt PNG"):
        measure_png(p)


def test_oversized_capture_is_refused_before_decoding(tmp_path: Path) -> None:
    """A decompression bomb must be rejected on its declared size, not after allocation."""
    raw = bytearray()
    for _ in range(4):
        raw.append(0)
        raw.extend(b"\x00" * 12)
    payload = zlib.compress(bytes(raw))

    def chunk(ctype: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + ctype
            + data
            + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 200_000, 200_000, 8, 2, 0, 0, 0)
    p = tmp_path / "bomb.png"
    p.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", payload) + chunk(b"IEND", b"")
    )
    with pytest.raises(CaptureError, match="refusing to decode"):
        measure_png(p)


def test_attestation_is_refused_when_the_staged_change_is_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    """An unbound record would match any future change - refuse rather than write null."""
    capture = tmp_path / "shot.png"
    capture.write_bytes(_png(400, 400, lambda y: (1, 2, 3)))
    monkeypatch.setattr(visual_proof, "staged_diff_hash", lambda *a, **k: None)
    with pytest.raises(visual_proof.UnboundProofError, match="cannot be bound"):
        write_attestation(
            capture, _ok_verdict(), tmp_path, "http://localhost/", directory=tmp_path / "proof"
        )
    assert not (tmp_path / "proof").exists(), "no proof directory should be created on refusal"


def test_empty_staged_diff_is_treated_as_unbound(tmp_path: Path, monkeypatch) -> None:
    """`git commit -am` stages AFTER the gate runs, so an empty-diff hash would match then."""

    def fake_run(cmd, capture_output, timeout, check):  # noqa: ARG001
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(visual_proof.subprocess, "run", fake_run)
    assert visual_proof.staged_diff_hash(tmp_path) is None


def test_inflation_bomb_with_small_header_is_refused(tmp_path: Path) -> None:
    """A 400x400 header can carry an IDAT that expands far beyond it; dimensions alone miss it."""
    width = height = 400
    honest = height * (1 + width * 3)
    payload = zlib.compress(b"\x00" * (honest * 50))

    def chunk(ctype: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + ctype
            + data
            + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        )

    p = tmp_path / "bomb.png"
    p.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", payload)
        + chunk(b"IEND", b"")
    )
    with pytest.raises(CaptureError, match="decompression bomb"):
        measure_png(p)


def test_oversized_viewport_is_refused_before_the_backend_runs(tmp_path: Path) -> None:
    called: list[str] = []

    def runner(args, capture_output, timeout, check):  # noqa: ARG001
        called.append(args[0])
        return subprocess.CompletedProcess(args, 0, b"", b"")

    with pytest.raises(CaptureError, match="exceeds the"):
        capture_with_sweep(
            "http://localhost/", tmp_path / "o.png", 1_000_000, 1_000_000, runner=runner
        )
    assert not called, "the backend must never be invoked for an absurd viewport"


def test_device_pixel_ratio_widths_are_accepted() -> None:
    """A retina-configured backend returns 2880 for a 1440 viewport - not a failure."""
    assert verdict_for(_metrics(width=2880, height=6000), expect_width=1440).ok
    assert verdict_for(_metrics(width=1440, height=6000), expect_width=1440).ok
    assert not verdict_for(_metrics(width=1280, height=6000), expect_width=1440).ok


def test_attestation_uses_the_digest_it_was_given(tmp_path: Path, monkeypatch) -> None:
    """The record must describe the bytes that were MEASURED, not a later re-read."""
    capture = tmp_path / "shot.png"
    capture.write_bytes(_png(400, 400, lambda y: (1, 2, 3)))
    monkeypatch.setattr(visual_proof, "staged_diff_hash", lambda *a, **k: "abc123")
    monkeypatch.setattr(visual_proof, "_git_toplevel", lambda *a, **k: tmp_path)
    out = write_attestation(
        capture,
        _ok_verdict(),
        tmp_path,
        "http://localhost/",
        directory=tmp_path / "proof",
        capture_sha256="measured-bytes-digest",
    )
    assert json.loads(out.read_text())["capture_sha256"] == "measured-bytes-digest"


def test_unwritable_output_parent_is_a_capture_error(tmp_path: Path) -> None:
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory")
    with pytest.raises(CaptureError, match="cannot create the output directory"):
        capture_with_sweep("http://localhost/", blocker / "sub" / "o.png", 1440, 900)


# --- CLI layer ------------------------------------------------------------------------


def test_shot_passes_the_measured_digest_into_the_record(tmp_path: Path, monkeypatch) -> None:
    """Guards the read-once race fix: a regression that drops the digest must fail here."""
    from dev_cli import cli

    out = tmp_path / "o.png"
    payload = b"the-exact-bytes"
    seen: dict = {}
    monkeypatch.setattr(
        visual_proof,
        "capture_with_sweep",
        lambda url, o, *a, **k: (Path(o).write_bytes(payload), _ctx())[1],
    )
    monkeypatch.setattr(
        visual_proof, "measure_png_bytes", lambda b, source="": CaptureMetrics(1440, 6000, 10, 5)
    )
    monkeypatch.setattr(
        visual_proof,
        "write_attestation",
        lambda *a, **k: seen.update(k) or (tmp_path / "a.json"),
    )
    rc = cli.main(["shot", "http://x/", "--out", str(out), "--repo", str(tmp_path)])
    assert rc == 0
    assert seen["capture_sha256"] == __import__("hashlib").sha256(payload).hexdigest()


def test_shot_maps_unbound_proof_to_exit_four(tmp_path: Path, monkeypatch) -> None:
    """Exit 4 is published in the installed skill text; a retake cannot fix this class."""
    from dev_cli import cli

    out = tmp_path / "o.png"
    monkeypatch.setattr(
        visual_proof,
        "capture_with_sweep",
        lambda url, o, *a, **k: (Path(o).write_bytes(b"x"), _ctx())[1],
    )
    monkeypatch.setattr(
        visual_proof, "measure_png_bytes", lambda b, source="": CaptureMetrics(1440, 6000, 10, 5)
    )

    def refuse(*a, **k):
        raise visual_proof.UnboundProofError("nothing staged")

    monkeypatch.setattr(visual_proof, "write_attestation", refuse)
    rc = cli.main(["shot", "http://x/", "--out", str(out), "--repo", str(tmp_path)])
    assert rc == cli.EXIT_UNBOUND_PROOF


def test_parse_viewport_accepts_and_rejects() -> None:
    from dev_cli import cli

    assert cli._parse_viewport("1440x900") == (1440, 900)
    assert cli._parse_viewport("393X852") == (393, 852)
    for bad in ("1440", "1440x900x2", "0x900", "-1440x900", "axb", ""):
        with pytest.raises(cli.DevError):
            cli._parse_viewport(bad)


def test_shot_maps_missing_backend_to_its_own_exit_code(tmp_path: Path, monkeypatch) -> None:
    from dev_cli import cli

    def boom(*a, **k):
        raise visual_proof.MissingBackendError("agent-browser is not installed or not on PATH")

    monkeypatch.setattr(visual_proof, "capture_with_sweep", boom)
    rc = cli.main(
        ["shot", "http://localhost/", "--out", str(tmp_path / "o.png"), "--repo", str(tmp_path)]
    )
    assert rc == cli.EXIT_MISSING_DEP


def test_shot_maps_bad_capture_to_exit_three(tmp_path: Path, monkeypatch, capsys) -> None:
    from dev_cli import cli

    monkeypatch.setattr(
        visual_proof,
        "capture_with_sweep",
        lambda url, out, *a, **k: (Path(out).write_bytes(b"x"), _ctx())[1],
    )
    monkeypatch.setattr(
        visual_proof,
        "measure_png_bytes",
        lambda b, source="": CaptureMetrics(1440, 6000, 5000, 4000),
    )
    rc = cli.main(
        ["shot", "http://localhost/", "--out", str(tmp_path / "o.png"), "--repo", str(tmp_path)]
    )
    assert rc == cli.EXIT_BAD_CAPTURE
    assert "capture rejected" in capsys.readouterr().err


def test_shot_no_attest_skips_the_record(tmp_path: Path, monkeypatch, capsys) -> None:
    from dev_cli import cli

    wrote: list[str] = []
    monkeypatch.setattr(
        visual_proof,
        "capture_with_sweep",
        lambda url, out, *a, **k: (Path(out).write_bytes(b"x"), _ctx())[1],
    )
    monkeypatch.setattr(
        visual_proof, "measure_png_bytes", lambda b, source="": CaptureMetrics(1440, 6000, 100, 50)
    )
    monkeypatch.setattr(visual_proof, "write_attestation", lambda *a, **k: wrote.append("x"))
    rc = cli.main(
        [
            "shot",
            "http://localhost/",
            "--out",
            str(tmp_path / "o.png"),
            "--repo",
            str(tmp_path),
            "--no-attest",
        ]
    )
    assert rc == 0
    assert not wrote
    assert "--no-attest" in capsys.readouterr().out


def test_shot_json_output_shape(tmp_path: Path, monkeypatch, capsys) -> None:
    from dev_cli import cli

    monkeypatch.setattr(
        visual_proof,
        "capture_with_sweep",
        lambda url, out, *a, **k: (Path(out).write_bytes(b"x"), _ctx())[1],
    )
    monkeypatch.setattr(
        visual_proof, "measure_png_bytes", lambda b, source="": CaptureMetrics(1440, 6000, 100, 50)
    )
    monkeypatch.setattr(visual_proof, "write_attestation", lambda *a, **k: tmp_path / "a.json")
    rc = cli.main(
        [
            "shot",
            "http://localhost/",
            "--out",
            str(tmp_path / "o.png"),
            "--repo",
            str(tmp_path),
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["width"] == 1440
    assert payload["largest_band_px"] == 50
    assert payload["attestation"].endswith("a.json")


def test_content_free_horizontal_gradient_is_flat(tmp_path: Path) -> None:
    """A blank page under a CSS gradient has no two identical pixels, so exact-equality
    uniformity scored it 0% featureless and handed it an attestation - the precise outcome
    this feature exists to withhold. Judge local change, and it reads as blank."""
    raw = bytearray()
    for _y in range(400):
        raw.append(0)
        for x in range(400):
            v = (x * 255) // 400
            raw.extend(bytes((v, v, v)))
    p = tmp_path / "gradient.png"
    p.write_bytes(_assemble(400, 400, bytes(raw)))
    m = measure_png(p)
    assert m.uniform_rows == 400, "a content-free gradient must count as featureless"
    assert m.largest_band == 400


def test_untracked_file_makes_the_worktree_dirty(tmp_path: Path) -> None:
    """`git diff` never lists untracked files, so a page rendered from a brand-new asset
    recorded worktree_dirty=false while the staged commit lacked the very file on screen."""
    import subprocess

    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(tmp_path), *a], capture_output=True, check=True
    )
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (tmp_path / "a.txt").write_text("a")
    run("add", "a.txt")
    run("commit", "-qm", "init")
    assert visual_proof._worktree_dirty(tmp_path) is False

    (tmp_path / "new-asset.webp").write_text("brand new, never tracked")
    assert visual_proof._worktree_dirty(tmp_path) is True, "untracked files are worktree dirt"


@pytest.mark.parametrize("col", [1, 2, 3, 5, 7, 101, 203, 398])
def test_a_one_pixel_vertical_rule_is_content_at_every_column(tmp_path: Path, col: int) -> None:
    """Probe columns rotate per row for this reason. Held fixed, a 1px border falls between
    samples in EVERY row or in none - so a tall empty bordered element (a textarea, an empty
    card) became one long featureless band and a correctly rendered page was refused."""
    raw = bytearray()
    for _y in range(400):
        raw.append(0)
        for x in range(400):
            raw.extend(bytes((0, 0, 0) if x == col else (255, 255, 255)))
    p = tmp_path / f"rule{col}.png"
    p.write_bytes(_assemble(400, 400, bytes(raw)))
    m = measure_png(p)
    assert m.band_frac <= visual_proof.THRESHOLDS["max_band_frac"], (
        f"a 1px rule at x={col} must not read as a blank band"
    )


def test_staged_only_change_is_not_worktree_dirty(tmp_path: Path) -> None:
    """Pins that column Y of porcelain is what's read, not column X. Checking X would call a
    staged-and-clean tree dirty - and stage-then-capture-then-attest is the NORMAL flow, so
    every honest attestation would carry worktree_dirty=true and the field would mean nothing.
    """
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(tmp_path), *a], capture_output=True, check=True
    )
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one")
    run("add", "a.txt")
    run("commit", "-qm", "init")

    (tmp_path / "a.txt").write_text("two")
    run("add", "a.txt")  # staged, worktree matches the index
    assert visual_proof._worktree_dirty(tmp_path) is False, "staged-and-clean is not dirty"

    (tmp_path / "a.txt").write_text("three")  # now the tree diverges from the index
    assert visual_proof._worktree_dirty(tmp_path) is True


@pytest.mark.parametrize(
    "name,pixel",
    [
        # A 45-degree rule shifts one column per row. Under any per-row rotation of sampled
        # columns it moves in LOCKSTEP with the rotation and is never sampled, so a rendered
        # page reads as one long blank band. This is why every adjacent pair is examined.
        ("diagonal", lambda y, x: (0, 0, 0) if x == y + 2 else (255, 255, 255)),
        # Regular thin dividers alias with any fixed stride - a table's column rules, say.
        ("stripes", lambda y, x: (0, 0, 0) if x % 4 == 0 else (255, 255, 255)),
    ],
)
def test_thin_content_is_never_aliased_away(tmp_path: Path, name: str, pixel) -> None:
    raw = bytearray()
    for y in range(400):
        raw.append(0)
        for x in range(400):
            raw.extend(bytes(pixel(y, x)))
    p = tmp_path / f"{name}.png"
    p.write_bytes(_assemble(400, 400, bytes(raw)))
    m = measure_png(p)
    assert m.band_frac <= visual_proof.THRESHOLDS["max_band_frac"], (
        f"{name}: thin content must not read as a blank band"
    )


def test_attestation_records_the_repo_root_not_the_subdirectory(tmp_path: Path) -> None:
    """The gate resolves ITS side to the git top-level, so the writer must agree. Recording
    the invocation directory instead makes every attestation written from a subdirectory
    unusable - the failure this pins is silent, and the claim is otherwise untested."""
    repo = tmp_path / "r"
    (repo / "src").mkdir(parents=True)
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(repo), *a], capture_output=True, check=True
    )
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "a.txt").write_text("one")
    run("add", "a.txt")

    capture = tmp_path / "shot.png"
    capture.write_bytes(_png(400, 400, lambda y: (1, 2, 3)))
    out = write_attestation(
        capture, _ok_verdict(), repo / "src", "http://localhost/", directory=tmp_path / "proof"
    )
    record = json.loads(out.read_text())
    assert record["repo"] == str(repo.resolve()), "the record must name the repository root"


def test_attestation_refuses_when_the_repo_root_cannot_be_resolved(
    tmp_path: Path, monkeypatch
) -> None:
    """Falling back to the raw directory would reintroduce the mismatch intermittently, since
    git only fails here on timeout or error. Refuse loudly instead."""
    monkeypatch.setattr(visual_proof, "staged_diff_hash", lambda *a, **k: "abc")
    monkeypatch.setattr(visual_proof, "_git_toplevel", lambda *a, **k: None)
    capture = tmp_path / "shot.png"
    capture.write_bytes(_png(400, 400, lambda y: (1, 2, 3)))
    with pytest.raises(visual_proof.UnboundProofError, match="repository root"):
        write_attestation(
            capture, _ok_verdict(), tmp_path, "http://localhost/", directory=tmp_path / "proof"
        )
