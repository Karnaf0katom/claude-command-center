"""Minimal QR Code encoder (byte mode) rendered as SVG. Stdlib only.

Used by the Phone access wizard to show the tailnet URL as a scannable code
without a JS dependency or a network call. Only what a URL needs: byte mode,
error-correction levels L and M, versions 1-40, automatic mask selection.

The construction follows ISO/IEC 18004 and the structure of Project Nayuki's
"QR Code generator library" (MIT License): data codewords -> Reed-Solomon
blocks -> interleave -> place -> mask. Output is bit-identical to libqrencode
for the same mask (tests/test_phone_access.py pins a golden matrix).
"""

from __future__ import annotations

# Index 0 is unused so a version number can index directly.
_ECC_PER_BLOCK = {
    "L": (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28,
          28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    "M": (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
          26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
}
_NUM_BLOCKS = {
    "L": (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8,
          8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    "M": (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16,
          17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
}
_FORMAT_BITS = {"L": 1, "M": 0}


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _num_raw_data_modules(ver: int) -> int:
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        numalign = ver // 7 + 2
        result -= (25 * numalign - 10) * numalign - 55
        if ver >= 7:
            result -= 36
    return result


def _num_data_codewords(ver: int, ecl: str) -> int:
    return _num_raw_data_modules(ver) // 8 - _ECC_PER_BLOCK[ecl][ver] * _NUM_BLOCKS[ecl][ver]


def _alignment_positions(ver: int, size: int) -> list[int]:
    if ver == 1:
        return []
    numalign = ver // 7 + 2
    step = (ver * 8 + numalign * 3 + 5) // (numalign * 4 - 4) * 2
    result = [size - 7 - i * step for i in range(numalign - 1)] + [6]
    return list(reversed(result))


class _Matrix:
    def __init__(self, ver: int):
        self.ver = ver
        self.size = ver * 4 + 17
        self.mod = [[False] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]

    def set_fn(self, x: int, y: int, dark: bool) -> None:
        self.mod[y][x] = dark
        self.fn[y][x] = True

    def draw_function_patterns(self, ecl: str) -> None:
        size = self.size
        for i in range(size):
            self.set_fn(6, i, i % 2 == 0)
            self.set_fn(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    xx, yy = cx + dx, cy + dy
                    if 0 <= xx < size and 0 <= yy < size:
                        self.set_fn(xx, yy, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _alignment_positions(self.ver, size)
        n = len(pos)
        for i in range(n):
            for j in range(n):
                if (i == 0 and j == 0) or (i == 0 and j == n - 1) or (i == n - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_fn(pos[i] + dx, pos[j] + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format_bits(ecl, 0)
        self.draw_version()

    def draw_format_bits(self, ecl: str, mask: int) -> None:
        data = _FORMAT_BITS[ecl] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: ((bits >> i) & 1) != 0  # noqa: E731
        size = self.size
        for i in range(0, 6):
            self.set_fn(8, i, bit(i))
        self.set_fn(8, 7, bit(6))
        self.set_fn(8, 8, bit(7))
        self.set_fn(7, 8, bit(8))
        for i in range(9, 15):
            self.set_fn(14 - i, 8, bit(i))
        for i in range(0, 8):
            self.set_fn(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_fn(8, size - 15 + i, bit(i))
        self.set_fn(8, size - 8, True)  # the always-dark module

    def draw_version(self) -> None:
        if self.ver < 7:
            return
        rem = self.ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.ver << 12 | rem
        for i in range(18):
            dark = ((bits >> i) & 1) != 0
            a = self.size - 11 + i % 3
            b = i // 3
            self.set_fn(a, b, dark)
            self.set_fn(b, a, dark)

    def draw_codewords(self, data: list[int]) -> None:
        size = self.size
        i = 0
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = size - 1 - vert if upward else vert
                    if not self.fn[y][x] and i < len(data) * 8:
                        self.mod[y][x] = ((data[i >> 3] >> (7 - (i & 7))) & 1) != 0
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        for y in range(self.size):
            for x in range(self.size):
                if self.fn[y][x]:
                    continue
                if mask == 0:
                    inv = (x + y) % 2 == 0
                elif mask == 1:
                    inv = y % 2 == 0
                elif mask == 2:
                    inv = x % 3 == 0
                elif mask == 3:
                    inv = (x + y) % 3 == 0
                elif mask == 4:
                    inv = (x // 3 + y // 2) % 2 == 0
                elif mask == 5:
                    inv = x * y % 2 + x * y % 3 == 0
                elif mask == 6:
                    inv = (x * y % 2 + x * y % 3) % 2 == 0
                else:
                    inv = ((x + y) % 2 + x * y % 3) % 2 == 0
                if inv:
                    self.mod[y][x] = not self.mod[y][x]

    def penalty(self) -> int:
        size = self.size
        m = self.mod
        score = 0
        lines = [m[y] for y in range(size)] + [[m[y][x] for y in range(size)] for x in range(size)]
        pat_a = [True, False, True, True, True, False, True, False, False, False, False]
        pat_b = list(reversed(pat_a))
        for line in lines:
            run = 1
            for k in range(1, size + 1):
                if k < size and line[k] == line[k - 1]:
                    run += 1
                    continue
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
            for k in range(size - 10):
                window = line[k:k + 11]
                if window == pat_a or window == pat_b:
                    score += 40
        for y in range(size - 1):
            for x in range(size - 1):
                c = m[y][x]
                if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(1 for c in row if c) for row in m)
        total = size * size
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        score += k * 10
        return score


def encode(text: str, ecl: str = "M", mask: int | None = None) -> list[list[bool]]:
    """Return the QR module matrix (rows of booleans, True = dark) for
    ``text`` in byte mode. Picks the smallest version that fits."""
    if ecl not in _ECC_PER_BLOCK:
        raise ValueError(f"unsupported error-correction level {ecl!r}")
    data = text.encode("utf-8")
    for ver in range(1, 41):
        cc_bits = 8 if ver <= 9 else 16
        used = 4 + cc_bits + len(data) * 8
        capacity = _num_data_codewords(ver, ecl) * 8
        if used <= capacity:
            break
    else:
        raise ValueError("text too long for a QR code")
    bits: list[int] = []

    def append(val: int, n: int) -> None:
        for i in reversed(range(n)):
            bits.append((val >> i) & 1)

    append(0b0100, 4)
    append(len(data), cc_bits)
    for b in data:
        append(b, 8)
    append(0, min(4, capacity - len(bits)))
    append(0, (8 - len(bits) % 8) % 8)
    pad = 0xEC
    while len(bits) < capacity:
        append(pad, 8)
        pad ^= 0xEC ^ 0x11
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

    numblocks = _NUM_BLOCKS[ecl][ver]
    blockecclen = _ECC_PER_BLOCK[ecl][ver]
    rawcodewords = _num_raw_data_modules(ver) // 8
    numshort = numblocks - rawcodewords % numblocks
    shortlen = rawcodewords // numblocks
    divisor = _rs_divisor(blockecclen)
    blocks = []
    k = 0
    for i in range(numblocks):
        dat = codewords[k:k + shortlen - blockecclen + (0 if i < numshort else 1)]
        k += len(dat)
        ecc = _rs_remainder(dat, divisor)
        if i < numshort:
            dat = dat + [0]
        blocks.append(dat + ecc)
    final = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != shortlen - blockecclen or j >= numshort:
                final.append(blk[i])

    qr = _Matrix(ver)
    qr.draw_function_patterns(ecl)
    qr.draw_codewords(final)
    if mask is None:
        best, best_score = 0, None
        for candidate in range(8):
            qr.apply_mask(candidate)
            qr.draw_format_bits(ecl, candidate)
            score = qr.penalty()
            if best_score is None or score < best_score:
                best, best_score = candidate, score
            qr.apply_mask(candidate)  # XOR again undoes it
        mask = best
    qr.apply_mask(mask)
    qr.draw_format_bits(ecl, mask)
    return qr.mod


def to_svg(text: str, ecl: str = "M", border: int = 4, scale: int = 6) -> str:
    """Encode ``text`` and render it as a self-contained SVG string."""
    matrix = encode(text, ecl)
    size = len(matrix)
    dim = size + border * 2
    parts = []
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                parts.append(f"M{x + border},{y + border}h1v1h-1z")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" '
        f'width="{dim * scale}" height="{dim * scale}" shape-rendering="crispEdges" '
        f'role="img" aria-label="QR code">'
        f'<rect width="100%" height="100%" fill="#fff"/>'
        f'<path d="{"".join(parts)}" fill="#000"/></svg>'
    )
