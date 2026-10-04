"""G.711 mu-law helpers.

Python 3.13 removed the `audioop` module, so the decode table is built by hand here.
"""
import sys
from array import array

SILENCE = 0xFF  # mu-law encoding of zero amplitude


def _decode_byte(u: int) -> int:
    u = ~u & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return -sample if sign else sample


_TABLE = [_decode_byte(i) for i in range(256)]


def ulaw_to_pcm16(payload: bytes) -> array:
    """mu-law bytes -> signed 16-bit samples."""
    return array("h", (_TABLE[b] for b in payload))


def interleave_stereo(left: array, right: array) -> bytes:
    """Two equal-length mono sample arrays -> little-endian stereo PCM bytes."""
    out = array("h", [0]) * (len(left) * 2)
    out[0::2], out[1::2] = left, right
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()
