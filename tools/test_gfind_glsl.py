"""Issue #27 (PR #43) regression: the compute shaders that read and write the dispatch's device-local copies (gmap /
gfast_, runtime/nvn/maxwell/translate.c glsl_finish) get a gfind() helper written straight into the GLSL header. Only
the shader body goes through glsl_rewrite() (MSL spelling -> GLSL: ulong -> uint64_t ...), so the helper must be
written in GLSL itself: with `ulong` in it, every such compute shader failed to compile at run time.

Translates small compute programs that take that path (LDG with a 32- and a 64-bit address, STG, a byte STG, ATOM) to
Vulkan GLSL, checks that gfind() takes a uint64_t and that no `ulong` is left anywhere in the GLSL, then compiles each
with shaderc as the runtime does (runtime/nvn/vulkan/shaders.c: compute, Vulkan 1.3 and 1.2, no optimization), and
validates the SPIR-V with spirv-val when it is installed.

shaderc: build/linux/x86_64/deps (tools/za/build-linux.sh), SHADERC_PREFIX=<prefix>, or the system's (pkg-config);
without it the compile step is skipped and says so (REQUIRE_SHADERC=1: a failure instead). This does not run the shaders
on a GPU: the run-time behaviour (issue #27's reproduction) needs the game.
usage: python3 tools/test_gfind_glsl.py"""
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
translate_c = root / 'runtime/nvn/maxwell/translate.c'
source = translate_c.read_text()

# ---- the helper as written in the source: GLSL types only (the header is not rewritten)
m = re.search(r'sp\(&s, "uint gfind\((.*?)\\n", GMAP_MAX\);', source, re.S)
assert m, 'gfind() helper not found in glsl_finish'
helper = m.group(0)
src_bad = []
if 'uint gfind(uint64_t a)' not in helper:
    src_bad.append('gfind() must take a uint64_t (GLSL), not ulong (MSL)')
if re.search(r'\bulong\b', helper):
    src_bad.append('MSL `ulong` in the GLSL header helper: glsl_rewrite() never sees it')
for e in src_bad:
    print(f'FAIL source: {e}')
if not src_bad:
    print('ok   source: gfind(uint64_t a), no ulong in the header helper')


def find_shaderc():
    cands = []
    if os.environ.get('SHADERC_PREFIX'):
        cands.append(Path(os.environ['SHADERC_PREFIX']))
    cands += [root / 'build/linux/x86_64/deps', root / 'build/deps-win']
    for p in cands:
        if (p / 'include/shaderc/shaderc.h').exists() and any((p / 'lib').glob('libshaderc_shared*')):
            return [f'-I{p / "include"}'], [f'-L{p / "lib"}', f'-Wl,-rpath,{p / "lib"}', '-lshaderc_shared']
    if shutil.which('pkg-config') and subprocess.run(['pkg-config', '--exists', 'shaderc'], capture_output=True).returncode == 0:
        cf = subprocess.run(['pkg-config', '--cflags', 'shaderc'], capture_output=True, text=True).stdout.split()
        lf = subprocess.run(['pkg-config', '--libs', 'shaderc'], capture_output=True, text=True).stdout.split()
        return cf, lf
    return None


harness = r'''
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "maxwell/shader.h"
#ifdef WITH_SHADERC
#include <shaderc/shaderc.h>
#endif

#define PT_GUARD (7ull << 16)
#define NOP 0x50b0000000070f00ull
#define EXIT 0xe30000000007000full

/* LDG / STG Rd, [Ra(+Ra+1 with .E) + off]: size 4 = 32-bit, 0 = u8 */
static uint64_t ldst(bool store, int d, int a, bool wide, int size, int32_t off) {
    return (store ? 0xeed8000000000000ull : 0xeed0000000000000ull) | PT_GUARD | (uint64_t)d | (uint64_t)a << 8 |
           (uint64_t)((uint32_t)off & 0xFFFFFF) << 20 | (uint64_t)wide << 45 | (uint64_t)size << 48;
}
/* ATOM.op Rd, [Ra(+Ra+1) + off], Rb: size 0 = 32-bit, op 0 = add */
static uint64_t atom(int d, int a, int b, bool wide, int32_t off) {
    return 0xed00000000000000ull | PT_GUARD | (uint64_t)d | (uint64_t)a << 8 | (uint64_t)b << 20 |
           (uint64_t)((uint32_t)off & 0xFFFFF) << 28 | (uint64_t)wide << 48;
}

int main(int argc, char **argv) {
    const char *dir = argc > 1 ? argv[1] : NULL;
    maxwell_gmem_base = 0x80000000ull, maxwell_gmem_size = 0x40000000ull;  /* (as tools/test_translate.c) */
    struct { const char *name; uint64_t insn[3]; } cs[] = {
        {"ldg32_addr32", {ldst(false, 0, 2, false, 4, 0x10)}},
        {"ldg32_addr64", {ldst(false, 0, 2, true, 4, 0x10)}},
        {"stg32_addr64", {ldst(true, 4, 2, true, 4, 0)}},
        {"stg8_addr64", {ldst(true, 4, 2, true, 0, 3)}},
        {"atom_add_addr64", {atom(0, 2, 4, true, 8)}},
    };
    int bad = 0, n = (int)(sizeof cs / sizeof cs[0]);
#ifdef WITH_SHADERC
    shaderc_compiler_t comp = shaderc_compiler_initialize();
#endif
    for (int i = 0; i < n; i++) {
        uint64_t prog[8];
        int m = 0;
        prog[m++] = 0;  /* scheduling word */
        for (int k = 0; k < 3 && cs[i].insn[k]; k++) prog[m++] = cs[i].insn[k];
        prog[m++] = EXIT;
        while (m % 4) prog[m++] = NOP;
        ShaderKey key = {0};  /* (compute: as exec.c translate_compute) */
        key.glsl = 1;
        ShaderInfo *s = maxwell_translate((const uint8_t *)prog, (size_t)m * 8, STAGE_COMPUTE, &key);
        if (!s->msl) { printf("FAIL %s: translation: %s\n", cs[i].name, s->error); bad++; continue; }
        if (!s->uses_gmem_map) { printf("FAIL %s: not on the gmap / gfast_ path\n", cs[i].name); bad++; }
        if (!strstr(s->msl, "uint gfind(uint64_t a)")) { printf("FAIL %s: no gfind(uint64_t a) in the GLSL\n", cs[i].name); bad++; }
        for (const char *p = s->msl; (p = strstr(p, "ulong")); p += 5) {
            bool word = (p == s->msl || !(p[-1] == '_' || (p[-1] >= 'a' && p[-1] <= 'z') || (p[-1] >= 'A' && p[-1] <= 'Z') ||
                                          (p[-1] >= '0' && p[-1] <= '9'))) &&
                        !(p[5] == '_' || (p[5] >= 'a' && p[5] <= 'z') || (p[5] >= 'A' && p[5] <= 'Z') || (p[5] >= '0' && p[5] <= '9'));
            if (word) { printf("FAIL %s: `ulong` left in the GLSL\n", cs[i].name); bad++; break; }
        }
        if (dir) {
            char path[1024];
            snprintf(path, sizeof path, "%s/%s.comp", dir, cs[i].name);
            FILE *f = fopen(path, "w");
            if (f) { fputs(s->msl, f); fclose(f); }
        }
#ifdef WITH_SHADERC
        const shaderc_env_version envs[2] = {shaderc_env_version_vulkan_1_3, shaderc_env_version_vulkan_1_2};
        for (int e = 0; e < 2; e++) {
            shaderc_compile_options_t o = shaderc_compile_options_initialize();
            shaderc_compile_options_set_target_env(o, shaderc_target_env_vulkan, envs[e]);
            shaderc_compile_options_set_optimization_level(o, shaderc_optimization_level_zero);
            shaderc_compilation_result_t r = shaderc_compile_into_spv(comp, s->msl, strlen(s->msl), shaderc_compute_shader,
                                                                      cs[i].name, "main", o);
            shaderc_compile_options_release(o);
            if (shaderc_result_get_compilation_status(r) != shaderc_compilation_status_success) {
                printf("FAIL %s (Vulkan 1.%d): GLSL compile:\n%s\n", cs[i].name, e ? 2 : 3, shaderc_result_get_error_message(r));
                bad++;
            } else if (dir && e == 0) {
                char path[1024];
                snprintf(path, sizeof path, "%s/%s.spv", dir, cs[i].name);
                FILE *f = fopen(path, "wb");
                if (f) { fwrite(shaderc_result_get_bytes(r), 1, shaderc_result_get_length(r), f); fclose(f); }
            }
            shaderc_result_release(r);
        }
#endif
        if (!bad) printf("ok   %s\n", cs[i].name);
    }
    if (bad) { printf("%d failure(s)\n", bad); return 1; }
    return 0;
}
'''

sc = find_shaderc()
if sc is None and os.environ.get('REQUIRE_SHADERC') == '1':
    sys.exit('FAIL: shaderc not found (REQUIRE_SHADERC=1)')
cc = shlex.split(os.environ.get('CC', 'cc'))
with tempfile.TemporaryDirectory() as td:
    c_path, exe = os.path.join(td, 't_gfind.c'), os.path.join(td, 't_gfind')
    Path(c_path).write_text(harness)
    cflags, lflags = (sc[0] + ['-DWITH_SHADERC'], sc[1]) if sc else ([], [])
    # (the harness with -Werror; translate.c with the flags tools/test_spirv_val.sh uses for it)
    obj = os.path.join(td, 'translate.o')
    subprocess.run(cc + ['-O1', '-Wall', '-c', str(translate_c), '-o', obj], check=True)
    subprocess.run(cc + ['-O1', '-Wall', '-Werror', f'-I{root / "runtime/nvn"}'] + cflags + [c_path, obj, '-o', exe, '-lm'] +
                   lflags, check=True)
    r = subprocess.run([exe, td], capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit('FAIL: gfind GLSL (see above)')
    print('ok   compiled with shaderc (compute, Vulkan 1.3 and 1.2), as the runtime does' if sc else
          'skip shaderc compile: no shaderc found (build/linux/x86_64/deps, SHADERC_PREFIX or pkg-config)')
    if sc and shutil.which('spirv-val'):
        for spv in sorted(Path(td).glob('*.spv')):
            v = subprocess.run(['spirv-val', '--target-env', 'vulkan1.3', str(spv)], capture_output=True, text=True)
            if v.returncode != 0:
                sys.exit(f'FAIL: spirv-val {spv.name}: {v.stdout}{v.stderr}')
        print('ok   spirv-val (Vulkan 1.3)')
    elif sc:
        print('skip spirv-val: not installed')
if src_bad:
    sys.exit('FAIL: gfind() helper in the source (see above)')
print('PASS: gfind() GLSL for compute shaders on the dispatch-buffer path (issue #27)')
