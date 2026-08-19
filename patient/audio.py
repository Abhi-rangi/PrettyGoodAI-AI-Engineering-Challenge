"""G.711 mu-law helpers.

Python 3.13 removed the `audioop` module, so the decode table is built by hand here.
It is 20 lines and removes a dependency, which felt like the right trade.
"""

SILENCE = 0xFF  # mu-law encoding of zero amplitude


def _decode_byte(u: int) -> int:
    u = ~u & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return -sample if sign else sample


_TABLE = [_decode_byte(i) for i in range(256)]


def ulaw_to_pcm16(payload: bytes) -> bytes:
    """mu-law bytes -> little-endian signed 16-bit PCM."""
    out = bytearray(len(payload) * 2)
    for i, byte in enumerate(payload):
        sample = _TABLE[byte]
        if sample < 0:
            sample += 0x10000
        out[2 * i] = sample & 0xFF
        out[2 * i + 1] = (sample >> 8) & 0xFF
    return bytes(out)
