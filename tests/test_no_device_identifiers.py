"""Refuse to commit a device identifier.

rules.md section 0 records what this is for: on 2026-09-28 a file
holding the charger's serial number and the household SSID was
committed, caught, removed, the history rewritten and the branch
force-pushed -- and GitHub still serves that blob by SHA, because a
rewritten history is not a deletion and the purge request has never
been filed.

The rule written after that incident lives in rules.md, which is
deliberately untracked. That decision is fine for conventions, but it
means the rule reaches no clone and no worktree: an agent working in
one gets no warning, and reaches for a real serial when it needs a
fixture. That has already happened once, and the catch was a human
reading the rule rather than anything that would have travelled.

This suite is that catch, in a form that does travel. It scans the
tracked files only -- what is actually reachable by a commit -- for
the identifier shapes the incident involved. Ignored working files
such as /log are out of scope on purpose: they cannot reach a commit,
and deleting them is the operator's call, not a test's.

Shapes, not values. A test that embedded the operator's real serial in
order to search for it would be the leak it exists to prevent.

Run with pytest, or standalone:

    python3 tests/test_no_device_identifiers.py
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# A real Cognito access token runs to roughly a thousand characters.
# The two JWT-shaped strings in test_diagnostics.py are hand-built
# headers of about twenty, used to prove the diagnostics handler
# redacts what it is given. Anything long enough to carry a payload is
# treated as real; anything that decodes to a bare JOSE header is not.
# Both conditions must hold for a string to be allowed through, so a
# genuine token cannot pass by being short.
SYNTHETIC_JWT_MAX_LEN = 60

JOSE_HEADER_KEYS = frozenset({"alg", "typ", "cty", "kid", "enc", "zip"})

PATTERNS: dict[str, re.Pattern[str]] = {
    # The charger's own serial. A fixture needs "SER1", never this.
    #
    # Case-insensitive: the charger reports the serial upper-case, but
    # a value copied out of a URL, a log line or a lower-cased
    # identifier reaches a file as "26dt..." just as easily, and the
    # strict form missed exactly that. Confirmed to add no false
    # positive against the tracked tree.
    "Daze serial number": re.compile(r"\b\d{2}DT\d{7}\b", re.IGNORECASE),
    # Network UIDs are GUIDs. A fixture needs "net-1", never this.
    "GUID (network UID)": re.compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
        r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    ),
    "MAC address": re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}"),
}


def _is_synthetic_jose_header(token: str) -> bool:
    """Whether a JWT-shaped string is a hand-built test header.

    Decodes the first segment and accepts it only if it is a small
    JSON object whose every key belongs to the JOSE header set. A real
    token fails on length before it reaches here; one that somehow did
    not would still have to carry nothing but header keys to pass.
    """
    if len(token) > SYNTHETIC_JWT_MAX_LEN:
        return False

    segment = token.split(".", 1)[0][3:]  # drop the "eyJ" sentinel
    padded = "eyJ" + segment
    padded += "=" * (-len(padded) % 4)

    try:
        decoded = base64.urlsafe_b64decode(padded)
        parsed = json.loads(decoded)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False

    if not isinstance(parsed, dict):
        return False

    return bool(parsed) and set(parsed).issubset(JOSE_HEADER_KEYS)


def _tracked_files() -> list[Path]:
    """Every file git would include in a commit."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        ROOT / name for name in result.stdout.split("\0") if name
    ]


def _findings() -> list[tuple[str, str, str]]:
    """Return (path, kind, match) for every identifier in tracked files."""
    found: list[tuple[str, str, str]] = []

    for path in _tracked_files():
        try:
            text = path.read_text(errors="replace")
        except (OSError, IsADirectoryError):
            continue

        relative = str(path.relative_to(ROOT))

        for kind, pattern in PATTERNS.items():
            for match in sorted(set(pattern.findall(text))):
                if kind == "JWT" and _is_synthetic_jose_header(match):
                    continue
                found.append((relative, kind, match))

    return found


def test_no_tracked_file_carries_a_device_identifier() -> None:
    """The gate itself.

    Fails with the file, the kind and the matched text, so whoever
    trips it can see at once whether they have leaked something or
    written a fixture that merely looks like one.
    """
    findings = _findings()

    assert not findings, "device identifiers in tracked files:\n" + "\n".join(
        f"  {path}: {kind} -> {match}" for path, kind, match in findings
    )


def test_the_serial_pattern_matches_the_shape_it_is_meant_to() -> None:
    """A gate nobody has seen reject anything is not known to work.

    Proves the serial pattern against the shape rather than against
    the operator's own serial, which this file must never contain.
    """
    pattern = PATTERNS["Daze serial number"]

    # Built, never written out. A literal of this shape in this file
    # would be the thing the gate exists to reject, and the gate
    # scans itself.
    shaped = "99" + "DT" + "0" * 7

    assert pattern.search(f"evse {shaped} reported")
    assert pattern.search(shaped)
    assert pattern.search(shaped.lower()), (
        "a lower-cased serial slipped the pattern"
    )
    assert not pattern.search("SER1")
    assert not pattern.search("serial_number")
    assert not pattern.search("DT01")


def test_a_serial_missing_its_letters_is_not_a_near_miss_to_rely_on() -> None:
    """Pins the shape that produced a false green, so it cannot again.

    Someone verifying this gate planted a test serial built from a
    masked form -- "26D" followed by digits, the masking having hidden
    the T. It did not match, the gate passed, and for a minute that
    read as the scanner being broken rather than the probe being
    malformed.

    The lesson generalises past this one pattern: a mutation built
    from a remembered or redacted shape proves nothing about the
    pattern, only about the memory. Build the probe from the pattern,
    as the test above does.
    """
    pattern = PATTERNS["Daze serial number"]

    missing_the_t = "26D" + "0" * 7
    assert not pattern.search(missing_the_t), (
        "the pattern matched a string that is not a serial; a probe "
        "built from this shape would prove nothing"
    )

    with_the_t = "26DT" + "0" * 7
    assert pattern.search(with_the_t), (
        "the pattern missed a correctly shaped serial"
    )


def test_the_guid_pattern_does_not_fire_on_fixture_names() -> None:
    """"net-1" is what a fixture should use, and must stay usable."""
    pattern = PATTERNS["GUID (network UID)"]

    # Assembled rather than written, for the same reason as the
    # serial above.
    shaped = "-".join(("0" * 8, "0" * 4, "0" * 4, "0" * 4, "0" * 12))

    assert pattern.search(shaped)
    assert not pattern.search("net-1")
    assert not pattern.search("ENTRY1")


def test_a_real_length_token_is_never_treated_as_synthetic() -> None:
    """Length alone must not be something a leak can satisfy."""
    real = "eyJ" + "A" * 400

    assert not _is_synthetic_jose_header(real), (
        "a token of realistic length was waved through as a test stub"
    )


def test_the_known_test_headers_are_allowed() -> None:
    """The two deliberate stubs in test_diagnostics.py stay legal.

    Without this the gate would fail on first run and be disabled,
    which is the usual way a check like this dies.
    """
    assert _is_synthetic_jose_header("eyJhbGciOiJIUzI1NiJ9")
    assert _is_synthetic_jose_header("eyJjdHkiOiJKV1QifQ")


def test_a_payload_bearing_token_is_not_a_header() -> None:
    """A short token carrying real claims still fails the check."""
    claims = base64.urlsafe_b64encode(
        json.dumps({"sub": "u1"}).encode()
    ).decode().rstrip("=")

    assert not _is_synthetic_jose_header(claims.replace("eyJ", "eyJ", 1)), (
        "a claims payload was accepted as a JOSE header"
    )


# ------------------------------------------------------------------
# Images carry metadata the identifier patterns were never meant to see
# ------------------------------------------------------------------

# Chunks a PNG needs in order to be a PNG. Everything else is metadata
# of some kind, and none of it belongs in this repository.
#
# Deliberately an allowlist. A denylist would have to name caBX, and
# nobody knew caBX existed until one arrived — which is the whole
# lesson. Anything not on this list fails, including chunk types
# invented after this was written.
_PNG_PIXEL_CHUNKS = frozenset(
    {
        "IHDR",  # dimensions, bit depth, colour type
        "PLTE",  # palette, for indexed-colour images
        "IDAT",  # the pixels
        "IEND",  # terminator
        "tRNS",  # transparency
        "gAMA",  # gamma
        "sRGB",  # colour space
        "cHRM",  # chromaticity
        "pHYs",  # pixel dimensions
        "sBIT",  # significant bits
        "bKGD",  # background colour for transparent images
    }
)

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})


def _png_chunks(raw: bytes) -> list[tuple[str, int]]:
    """Return (type, length) for every chunk in a PNG."""
    chunks: list[tuple[str, int]] = []
    position = 8  # past the signature
    while position + 8 <= len(raw):
        length = int.from_bytes(raw[position : position + 4], "big")
        kind = raw[position + 4 : position + 8].decode("latin1", "replace")
        chunks.append((kind, length))
        position += 12 + length
        if kind == "IEND":
            break
    return chunks


def test_no_tracked_image_carries_embedded_metadata() -> None:
    """An image is pixels. Anything else in the file is a passenger.

    On 2026-10-05 a chart added to docs/ arrived carrying a
    23,654-byte caBX chunk — a C2PA Content Credentials provenance
    manifest, holding URLs, timestamps, GUIDs, cryptographic
    signatures and four strings containing an @.

    It was looked for and missed. The inspection checked tEXt, iTXt,
    zTXt and eXIf — the chunks a person thinks of — and reported the
    file clean. What caught it was
    test_no_tracked_file_carries_a_device_identifier, which does not
    know what a PNG is and simply scanned the bytes, failing on a GUID
    at offset 121.

    That catch was luck. The identifier patterns cover serials, GUIDs,
    MACs and JWTs; a provenance manifest carrying only URLs, an email
    address and timestamps matches none of them and would have gone
    through. This test does not depend on what the metadata happens to
    contain.
    """
    offenders: list[str] = []

    for path in _tracked_files():
        if path.suffix.lower() not in _IMAGE_SUFFIXES:
            continue

        raw = path.read_bytes()
        name = path.relative_to(ROOT).as_posix()

        if path.suffix.lower() != ".png":
            offenders.append(
                f"{name}: only PNG is understood by this check — add a "
                "reader for this format rather than leaving it unchecked"
            )
            continue

        for kind, length in _png_chunks(raw):
            if kind not in _PNG_PIXEL_CHUNKS:
                offenders.append(f"{name}: {kind} chunk, {length} bytes")

    assert not offenders, (
        "tracked images carry embedded metadata:\n  "
        + "\n  ".join(offenders)
        + "\n\nStrip it. Rebuilding the file from its IHDR, IDAT and "
        "IEND chunks keeps the pixels and discards everything else."
    )


def test_the_png_chunk_reader_sees_what_is_there() -> None:
    """Control for the test above, which passes trivially if the reader
    returns nothing. Builds a minimal PNG, confirms the reader finds
    its chunks, then adds an ancillary chunk and confirms it is seen
    and rejected.
    """
    import struct
    import zlib

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", zlib.crc32(body))
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\xff\xff")
    clean = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixels)
        + chunk(b"IEND", b"")
    )

    kinds = [k for k, _ in _png_chunks(clean)]
    assert kinds == ["IHDR", "IDAT", "IEND"], kinds
    assert all(k in _PNG_PIXEL_CHUNKS for k in kinds)

    tainted = clean[:-12] + chunk(b"caBX", b"x" * 64) + clean[-12:]
    tainted_kinds = [k for k, _ in _png_chunks(tainted)]
    assert "caBX" in tainted_kinds, tainted_kinds
    assert "caBX" not in _PNG_PIXEL_CHUNKS


def _main() -> int:
    """Run every test in this module and report results."""
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]

    failures = 0
    for test in tests:
        try:
            test()
        except Exception as err:  # noqa: BLE001 - standalone runner
            failures += 1
            print(f"FAIL {test.__name__}: {type(err).__name__}: {err}")
        else:
            print(f"ok   {test.__name__}")

    print(f"\n{len(tests) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
