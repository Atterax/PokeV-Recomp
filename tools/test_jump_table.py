#!/usr/bin/env python3
"""tools/gen.py jump-table detection: small synthetic functions (hand-encoded AArch64, no game files) run through
Module.scan, and - when the player's own Scarlet / Violet 4.0.0 files are at hand - the four byte-offset tables whose guard
sits 23 to 28 instructions before the br (issues #18 / #19: "no function at 7001d1e7a8").
Usage: python tools/test_jump_table.py   (needs the generator's own packages: pip install capstone==5.0.9 unicorn==2.1.4
lz4==4.4.5; without them it FAILS rather than skips, so CI can't lose it silently)
Real files: LARECOMP_TEST_EXEFS=<folder with main, sdk, subsdk0> (default: the data folder's extracted/exefs); skipped when absent.
"""
import os, sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import gen
    from lifter import Lifter
except ImportError as e:
    print(f'FAIL: the generator cannot be imported ({e}): pip install capstone==5.0.9 unicorn==2.1.4 lz4==4.4.5')
    sys.exit(1)

BASE = 0x7000000000
TEXT = 0x1000      # the synthetic code
TABLE = 0x8000     # the byte table, in "rodata"
SIZE = 0x10000


# ---- hand-encoded instructions (only what the tests use)
def sub_imm(rd, rn, imm): return 0x51000000 | imm << 10 | rn << 5 | rd          # sub wd, wn, #imm
def cmp_imm(rn, imm): return 0x7100001F | imm << 10 | rn << 5                    # cmp wn, #imm
def b_cond(cond, frm, to): return 0x54000000 | ((to - frm) >> 2 & 0x7FFFF) << 5 | cond
def b_hi(frm, to): return b_cond(8, frm, to)
def b(frm, to): return 0x14000000 | ((to - frm) >> 2 & 0x3FFFFFF)
def cbz_x(rt, frm, to): return 0xB4000000 | ((to - frm) >> 2 & 0x7FFFF) << 5 | rt
def adrp(rd, frm, to): p = (to >> 12) - (frm >> 12); return 0x90000000 | (p & 3) << 29 | (p >> 2 & 0x7FFFF) << 5 | rd
def add_imm(rd, rn, imm): return 0x91000000 | imm << 10 | rn << 5 | rd          # add xd, xn, #imm
def adr(rd, frm, to): o = to - frm; return 0x10000000 | (o & 3) << 29 | (o >> 2 & 0x7FFFF) << 5 | rd
def ldrb_reg(rt, rn, rm): return 0x38606800 | rm << 16 | rn << 5 | rt            # ldrb wt, [xn, xm]
def add_lsl2(rd, rn, rm): return 0x8B000000 | rm << 16 | 2 << 10 | rn << 5 | rd  # add xd, xn, xm, lsl #2
def br(rn): return 0xD61F0000 | rn << 5
def ret(): return 0xD65F03C0
def nop(): return 0xD503201F
def ldr_x(rt, rn): return 0xF9400000 | rn << 5 | rt                               # ldr xt, [xn]


class Fn:
    """A function at TEXT: sub w10, w3, #1; cmp w10, #n-1; b.hi default; <lookup>; <filler>; br x20, then n cases (each a
    `b` to the default) and the default (ret). `filler` plain instructions sit between the lookup and the br, which
    sets how far back the guard is."""
    def __init__(self, ncases=12, filler=0, guard=True, lookup='table', between=None):
        self.words = []
        pc = lambda: TEXT + 4 * len(self.words)
        self.words.append(sub_imm(10, 3, 1))
        self.words.append(cmp_imm(10, ncases - 1))
        guard_at = pc()
        self.words.append(0)  # (the guard: patched when the default's address is known)
        if lookup == 'table':   # adrp/add the byte table, adr the case base, ldrb, add lsl #2
            self.words.append(adrp(19, pc(), TABLE)); self.words.append(add_imm(19, 19, TABLE & 0xFFF))
            self.words.append(adr(20, pc(), 0)); adr_at = len(self.words) - 1
            self.words.append(ldrb_reg(21, 19, 10)); self.words.append(add_lsl2(20, 20, 21))
        else:                   # a br that doesn't depend on the index
            self.words.append(adr(20, pc(), 0)); adr_at = len(self.words) - 1
        for i in range(filler):
            if between is not None and i == filler // 2:
                self.words.append(between(pc()))
            else:
                self.words.append(add_imm(8 + i % 4, 7, 4 * i & 0xFFF))
        if not guard:
            self.words[2] = nop()
        self.br_pc = pc()
        self.words.append(br(20))
        self.case0 = pc()
        self.words[adr_at] = adr(20, TEXT + 4 * adr_at, self.case0)
        self.dflt = self.case0 + 4 * ncases
        for k in range(ncases):
            self.words.append(b(pc(), self.dflt))
        self.words.append(ret())
        self.entry, self.end = TEXT, pc()
        if guard:
            self.words[2] = b_hi(guard_at, self.dflt)
        self.ncases, self.lookup = ncases, lookup
        self.guard_dist = (self.br_pc - guard_at) // 4  # instructions from the guard to the br

    def table_bytes(self):  # case k's offset from the first case, in instructions: a permuted layout
        return bytes((k * 7) % self.ncases for k in range(self.ncases))

    def expect(self):
        return sorted({self.case0 + 4 * b for b in self.table_bytes()}) if self.lookup == 'table' else [self.case0]

    def module(self):
        img = bytearray(SIZE)
        for i, w in enumerate(self.words):
            img[TEXT + 4 * i:TEXT + 4 * i + 4] = w.to_bytes(4, 'little')
        img[TABLE:TABLE + self.ncases] = self.table_bytes()
        m = gen.Module.__new__(gen.Module)
        m.nso = type('N', (), {'image': bytes(img)})()
        m.base, m.lo, m.hi = BASE, BASE + TEXT, BASE + self.end
        m.words = [int.from_bytes(img[TEXT + 4 * i:TEXT + 4 * i + 4], 'little') for i in range((self.end - TEXT) // 4)]
        m.lifter, m.cache, m.stats, m.mu, m.setjmp_stubs = Lifter(BASE, 'c'), {}, Counter(), None, set()
        return m


def scan(f):
    m = f.module()
    labels, ext, tables = m.scan(BASE + f.entry, BASE + f.end)
    return m, tables.get(BASE + f.br_pc)


def at(f, targets): return [BASE + t for t in targets]


fails = []
def check(name, ok, detail=''):
    print(('ok   ' if ok else 'FAIL ') + name + (f'  ({detail})' if detail and not ok else ''))
    if not ok:
        fails.append(name)


# 1. guard within 16: resolved by the stock recognizer, the wide search not involved
f = Fn(filler=3)
m, jt = scan(f)
check('guard within 16 instructions resolves (guard %d back)' % f.guard_dist, f.guard_dist <= 16 and jt == at(f, f.expect()), jt)
check('  ... without the wide search', m.stats['jumptables'] == 1 and m.stats['jumptables_wide'] == 0)

# 2. guard 23 instructions back (the Scarlet / Violet shape): stock recognizers leave it, the wide search resolves it
f = Fn(filler=17)
m = f.module()
check('guard 23 back: stock jump_table leaves it', f.guard_dist == 23 and m.jump_table(BASE + f.br_pc, BASE + f.entry, BASE + f.end) is None)
check('guard 23 back: computed_target leaves it', not m.computed_target(BASE + f.br_pc, BASE + f.entry))
m, jt = scan(f)
check('guard 23 back: scan resolves all 12 targets in order', jt == at(f, f.expect()) and len(jt) == 12, jt)
check('  ... through the wide search', m.stats['jumptables_wide'] == 1 and m.stats['br_dispatch'] == 0)
check('  ... cases are labels of the function', bool(jt) and all(t in m.scan(BASE + f.entry, BASE + f.end)[0] for t in jt))

# 3. reach: guard 32 back resolves, 33 back doesn't
f = Fn(filler=26)
check('guard 32 back resolves', f.guard_dist == 32 and scan(f)[1] == at(f, f.expect()))
f = Fn(filler=27)
m, jt = scan(f)
check('guard 33 back stays unresolved', f.guard_dist == 33 and jt is None and m.stats['br_dispatch'] == 1)

# 4. more or fewer cases
for n in (2, 5, 12, 40):
    f = Fn(ncases=n, filler=20)
    check('%d cases at guard %d back' % (n, f.guard_dist), scan(f)[1] == at(f, f.expect()))

# 5. ambiguous / invalid: must stay unresolved
f = Fn(filler=20, between=lambda pc: cbz_x(0, pc, pc + 8))  # a conditional branch between guard and br: not dominated
m, jt = scan(f)
check('a conditional branch between guard and br is not a table', jt is None and m.stats['br_dispatch'] == 1, jt)
f = Fn(filler=20, between=lambda pc: b_cond(0, pc, pc + 8))  # (b.eq)
check('  ... also for b.cond', scan(f)[1] is None)
f = Fn(filler=20, lookup='fixed')  # the br doesn't depend on the index: one target is not a table
m, jt = scan(f)
check('a br independent of the index is not a table', jt is None and m.stats['br_dispatch'] == 1, jt)
# 6. no guard
f = Fn(filler=20, guard=False)
m, jt = scan(f)
check('no guard at all stays unresolved', jt is None, jt)

# 7. existing behaviour untouched: the 'all' fallback (a computed br within 16, no guard) and plain dispatch
f = Fn(filler=3, guard=False)
m, jt = scan(f)
check("a computed br without a guard still gets the 'all' fallback", jt == 'all' and m.stats['jumptables_wide'] == 0, jt)
m = f.module()
m.words[(f.br_pc - TEXT) // 4 - 1] = ldr_x(20, 0)  # br x20 loaded from memory: a call through a pointer
m.cache = {}
check('a br through a loaded pointer is plain dispatch', m.scan(BASE + f.entry, BASE + f.end)[2].get(BASE + f.br_pc) is None
      and m.stats['br_dispatch'] == 1)
# the same functions with the wide search switched off must give the same result for everything resolved before
for fn in (Fn(filler=3), Fn(filler=15), Fn(filler=3, guard=False), Fn(filler=20, guard=False)):
    a = fn.module(); b = fn.module()
    b.jump_table_wide = lambda *x: None
    ra, rb = a.scan(BASE + fn.entry, BASE + fn.end)[2], b.scan(BASE + fn.entry, BASE + fn.end)[2]
    check('scan unchanged by the wide search where it was not needed (guard %d back)' % fn.guard_dist, ra == rb)

# 8. the real files, if present: the four tables resolve to their targets
exefs = os.environ.get('LARECOMP_TEST_EXEFS') or os.path.expanduser('~/.local/share/pokev-recomp/extracted/exefs')
# (module, base, function start, end, br, distinct targets, guard distance): the byte tables have 12, 12, 10 and 20 entries
# (the last maps them onto 5 cases), their guards 23, 23, 28 and 24 instructions before the br
SITES = [('main', 0x7000000000, 0x7001d1e6dc, 0x7001d1eb54, 0x7001d1e7a4, 12),
         ('main', 0x7000000000, 0x7001d1eb54, 0x7001d1f004, 0x7001d1ec20, 12),
         ('sdk', 0x7008000000, 0x7008252b74, 0x700825315c, 0x7008252dd4, 10),
         ('subsdk0', 0x700a000000, 0x700a053d90, 0x700a053f30, 0x700a053dfc, 5)]
try:
    import lz4.block  # noqa: F401
    have = all(os.path.exists(os.path.join(exefs, s[0])) for s in SITES)
except ImportError:
    have = False
if have:
    from nso import Nso
    mods = {}
    for name, base, entry, end, brpc, want in SITES:
        if name not in mods:
            mods[name] = gen.Module(name, os.path.join(exefs, name), base, 'c')
        m = mods[name]
        if m.nso.build_id[:16].upper() != '709BFD6611529864' and name == 'main':
            print('SKIP real files: not Scarlet / Violet 4.0.0 (build 709BFD66...)'); break
        labels, ext, tables = m.scan(entry, end)
        jt = tables.get(brpc)
        check('real %s br %x resolves to %d targets, all inside the function' % (name, brpc, want),
              isinstance(jt, list) and len(jt) == want and all(entry <= t < end for t in jt), jt)
        check('  ... through the wide search', m.stats['jumptables_wide'] >= 1)
        m.stats.clear()
else:
    print('SKIP real files (no lz4 or no exefs at %s)' % exefs)

print('\n%d failure(s)' % len(fails) if fails else '\nall jump-table checks passed')
sys.exit(1 if fails else 0)
