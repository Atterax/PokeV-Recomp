"""Pokémon Scarlet & Violet widescreen projection (Phase 1):
1. The math (tools/test_widescreen_proj.c against runtime/nvn/output_scale.c and f_7000cf0ef4's perspective math).
2. Runtime widescreen_frame_boxed() & rt_sv_proj_* (runtime/hle/widescreen.c): Violet (g_game_is_la=0) is never
   globally boxed, while Link's Awakening (g_game_is_la=1) preserves its boxing rules.
3. The generator patches (tools/sv_patches.py): all 4 sites (2 branches x half_fovy + aspect) compiled against a
   stand-in CPU context, verifying register I/O, upper-bit clearing, guest memory preservation, and word assertions.
SV_MAIN=<exefs/main of 3.0.1>: also checks the 4 patch sites' words in the real executable (needs lz4)."""
import os
from pathlib import Path
import shlex
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))
import sv_patches  # noqa: E402

CC = shlex.split(os.environ.get('CC', 'clang'))
BASE = 0x7000000000

SITE_B1_HFOV = 0x7000cf1168
SITE_B1_ASP  = 0x7000cf11e8
SITE_B2_HFOV = 0x7000cf182c
SITE_B2_ASP  = 0x7000cf18a4

WORDS = {
    SITE_B1_HFOV - 0x2c: 0xbd418e60,  # ldr s0, [x19, #0x18c]
    SITE_B1_HFOV - 0x28: 0x1e2c1001,  # fmov s1, #0.5
    SITE_B1_HFOV:        0x1e210800,  # fmul s0, s0, s1
    SITE_B1_ASP:         0xbd419270,  # ldr s16, [x19, #0x190]
    SITE_B2_HFOV - 0x2c: 0xbd418e60,  # ldr s0, [x19, #0x18c]
    SITE_B2_HFOV - 0x28: 0x1e2c1001,  # fmov s1, #0.5
    SITE_B2_HFOV:        0x1e210800,  # fmul s0, s0, s1
    SITE_B2_ASP:         0xbd419264,  # ldr s4, [x19, #0x190]
}
failed = 0


class FakeNso:
    def __init__(self, words):
        max_off = max(a - BASE for a in words) + 0x10
        self.image = bytearray(max_off)
        for a, w in words.items():
            struct.pack_into('<I', self.image, a - BASE, w)


class FakeGen:
    base = BASE

    def __init__(self, words):
        self.nso = FakeNso(words)


def check(ok, what):
    global failed
    print(('ok   ' if ok else 'FAIL ') + what)
    failed += not ok


with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    # 1. the math
    exe = d / 'widescreen-proj-test'
    subprocess.run(CC + ['-std=gnu11', '-O1', '-Iruntime', 'runtime/nvn/output_scale.c', 'tools/test_widescreen_proj.c',
                         '-lm', '-o', str(exe)], cwd=ROOT, check=True)
    r = subprocess.run([str(exe)], capture_output=True, text=True)
    print(r.stdout, end='')
    check(r.returncode == 0, 'projection math (tools/test_widescreen_proj.c)')

    # 2. widescreen_frame_boxed() & rt_sv_proj_* in runtime/hle/widescreen.c
    box_src = d / 'box_test.c'
    box_src.write_text('''#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "settings.h"
#include "rt.h"
#include "game_addresses.h"

int g_game_is_la = 0;
uint8_t *g_mem_base = NULL;
static GameAddrs g_stub_ga;
const GameAddrs *g_ga = &g_stub_ga;
Settings g_settings = {.aspect = ASPECT_UNLOCKED};
static int g_stub_in_play = 0;
static char g_stub_level[32] = "Field";
static uint64_t g_stub_ps_ptr = 0;

int game_in_play(void) { return g_stub_in_play; }
uint64_t renderer_present_count(void) { return 0; }
uint64_t renderer_exec_frame(void) { return 0; }

extern bool widescreen_frame_boxed(uint64_t frame);
extern void widescreen_set_factor(float f);
extern float rt_sv_proj_aspect(float aspect);
extern float rt_sv_proj_half_fovy(float half_fovy, float aspect);

int main(void) {
    /* Make GA(PLAYER_STATUS_PTR) point to g_stub_ps_ptr holding (uintptr_t)g_stub_level */
    g_stub_ps_ptr = (uintptr_t)g_stub_level;
    g_stub_ga.PLAYER_STATUS_PTR = (uintptr_t)&g_stub_ps_ptr;

    /* Violet (g_game_is_la == 0): never globally boxed, even when game_in_play() == 0 */
    g_game_is_la = 0;
    g_stub_in_play = 0;
    int ok = !widescreen_frame_boxed(100) && !widescreen_frame_boxed(101);

    /* Link's Awakening (g_game_is_la == 1): boxed when not in play, unboxed on "Field", boxed in interior */
    g_game_is_la = 1;
    g_stub_in_play = 0;
    ok = ok && widescreen_frame_boxed(102);
    g_stub_in_play = 1;
    strcpy(g_stub_level, "Field");
    ok = ok && !widescreen_frame_boxed(103);
    strcpy(g_stub_level, "House01");
    ok = ok && widescreen_frame_boxed(104);

    /* rt_sv_proj_aspect / rt_sv_proj_half_fovy integration */
    widescreen_set_factor(2.0f);
    float a32 = rt_sv_proj_aspect(16.0f / 9.0f);
    float h32 = rt_sv_proj_half_fovy(0.4f, 16.0f / 9.0f);
    float a_non169 = rt_sv_proj_aspect(4.0f / 3.0f);
    ok = ok && fabsf(a32 - 32.0f / 9.0f) < 1e-6f && h32 == 0.4f && fabsf(a_non169 - 4.0f / 3.0f) < 1e-6f;

    widescreen_set_factor(1.0f);
    float a16 = rt_sv_proj_aspect(16.0f / 9.0f);
    ok = ok && a16 == 16.0f / 9.0f;

    return !ok;
}
''')
    deps_inc = ROOT / 'build/linux/x86_64/deps/include'
    inc_flags = ['-Iruntime'] + ([f'-I{deps_inc}'] if deps_inc.is_dir() else [])
    box_exe = d / 'box-test'
    subprocess.run(CC + ['-std=gnu11', '-O1'] + inc_flags +
                   ['runtime/hle/widescreen.c', str(box_src), '-lm', '-o', str(box_exe)],
                   cwd=ROOT, check=True)
    r = subprocess.run([str(box_exe)], capture_output=True, text=True)
    check(r.returncode == 0, 'runtime/hle/widescreen.c: widescreen_frame_boxed() and rt_sv_proj_*')

    # 3. the generator patches: in the table for 3.0.1, emitted C behaves like the instructions
    table = sv_patches.sv_patches('709bfd6611529864')
    check(table.get(SITE_B1_HFOV) is sv_patches._sv_proj_half_fovy, f'{SITE_B1_HFOV:#x} patched for 3.0.1')
    check(table.get(SITE_B1_ASP) is sv_patches._sv_proj_aspect_s16, f'{SITE_B1_ASP:#x} patched for 3.0.1')
    check(table.get(SITE_B2_HFOV) is sv_patches._sv_proj_half_fovy, f'{SITE_B2_HFOV:#x} patched for 3.0.1')
    check(table.get(SITE_B2_ASP) is sv_patches._sv_proj_aspect_s4, f'{SITE_B2_ASP:#x} patched for 3.0.1')

    gen = FakeGen(WORDS)
    s_b1_hfov = sv_patches._sv_proj_half_fovy(gen, SITE_B1_HFOV)
    s_b1_asp  = sv_patches._sv_proj_aspect_s16(gen, SITE_B1_ASP)
    s_b2_hfov = sv_patches._sv_proj_half_fovy(gen, SITE_B2_HFOV)
    s_b2_asp  = sv_patches._sv_proj_aspect_s4(gen, SITE_B2_ASP)

    psrc = d / 'patch_test.c'
    psrc.write_text(f'''#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef struct {{ uint64_t x[32]; __uint128_t v[32]; }} Cpu;
static float seen_hfov, seen_hfov_asp, seen_asp;
float rt_sv_proj_half_fovy(float h, float a) {{ seen_hfov = h; seen_hfov_asp = a; return h + 0.1f; }}
float rt_sv_proj_aspect(float a) {{ seen_asp = a; return a * 2.0f; }}
static void site_b1_hfov(Cpu *c) {{ {s_b1_hfov} }}
static void site_b1_asp(Cpu *c)  {{ {s_b1_asp} }}
static void site_b2_hfov(Cpu *c) {{ {s_b2_hfov} }}
static void site_b2_asp(Cpu *c)  {{ {s_b2_asp} }}
int main(void) {{
    static float cam[0x200 / 4];
    cam[0x190 / 4] = 16.0f / 9.0f;
    Cpu c;
    float fovy = 0.8726646f, half = 0.5f;
    float v0, v16, v4;

    /* Branch 1 */
    memset(&c, 0xa5, sizeof c);
    c.x[19] = (uintptr_t)cam;
    c.v[0] = 0; memcpy(&c.v[0], &fovy, 4);
    c.v[1] = 0; memcpy(&c.v[1], &half, 4);
    site_b1_hfov(&c);
    memcpy(&v0, &c.v[0], 4);
    int ok = seen_hfov == fovy * 0.5f && seen_hfov_asp == 16.0f / 9.0f &&
             v0 == fovy * 0.5f + 0.1f && (c.v[0] >> 32) == 0;
    site_b1_asp(&c);
    memcpy(&v16, &c.v[16], 4);
    ok = ok && seen_asp == 16.0f / 9.0f && v16 == 32.0f / 9.0f && (c.v[16] >> 32) == 0 &&
         cam[0x190 / 4] == 16.0f / 9.0f;

    /* Branch 2 */
    memset(&c, 0xa5, sizeof c);
    c.x[19] = (uintptr_t)cam;
    c.v[0] = 0; memcpy(&c.v[0], &fovy, 4);
    c.v[1] = 0; memcpy(&c.v[1], &half, 4);
    site_b2_hfov(&c);
    memcpy(&v0, &c.v[0], 4);
    ok = ok && v0 == fovy * 0.5f + 0.1f && (c.v[0] >> 32) == 0;
    site_b2_asp(&c);
    memcpy(&v4, &c.v[4], 4);
    ok = ok && seen_asp == 16.0f / 9.0f && v4 == 32.0f / 9.0f && (c.v[4] >> 32) == 0 &&
         cam[0x190 / 4] == 16.0f / 9.0f;

    return !ok;
}}
''')
    pexe = d / 'patch-test'
    subprocess.run(CC + ['-std=gnu11', '-O1', str(psrc), '-o', str(pexe)], check=True)
    r = subprocess.run([str(pexe)], capture_output=True, text=True)
    check(r.returncode == 0, 'emitted C: all 4 sites read [x19+0x190]/s0/s1, write s0/s16/s4 (upper bits 0), guest mem kept')

    for at in WORDS:
        bad = dict(WORDS)
        bad[at] ^= 1
        fn = sv_patches._sv_proj_aspect_s16 if at == SITE_B1_ASP else (
             sv_patches._sv_proj_aspect_s4 if at == SITE_B2_ASP else sv_patches._sv_proj_half_fovy)
        site = at if at in (SITE_B1_ASP, SITE_B2_ASP) else (
               SITE_B1_HFOV if at <= SITE_B1_HFOV else SITE_B2_HFOV)
        try:
            fn(FakeGen(bad), site)
            check(False, f'a different word at {at:#x} is refused')
        except AssertionError:
            check(True, f'a different word at {at:#x} is refused')

    # 4. optionally, the real executable
    main = os.environ.get('SV_MAIN')
    if main:
        from nso import Nso
        n = Nso(main)
        check(n.build_id[:16] == '709bfd6611529864', f'SV_MAIN is Violet 3.0.1 ({n.build_id[:16]})')

        class G:
            nso, base = n, BASE

        for site, fn in table.items():
            if site in (SITE_B1_HFOV, SITE_B1_ASP, SITE_B2_HFOV, SITE_B2_ASP):
                try:
                    fn(G, site)
                    check(True, f'{site:#x} words match the real executable')
                except AssertionError as e:
                    check(False, f'{site:#x} words in the real executable: {e}')

print('all widescreen projection checks passed' if not failed else f'{failed} widescreen projection check(s) FAILED')
sys.exit(1 if failed else 0)
