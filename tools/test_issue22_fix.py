"""Issue #22 production workaround (exec.c i22fix_*, LARECOMP_ISSUE22_FIX): a draw of E (vs 09c1280fee686904, fs
55b596e702185e46 - a Terastallized Water move's effect, which hung NVIDIA GPUs) drawn with D's vertex shader
(2aadd88fa6502c59), everything else of E's as it is. Checks: the workaround's code records nothing and changes nothing
but the draw's program (st.program and draw_'s pr), keeps D's vertex stage as a value copy under a lock, never keys a
cache on its per-thread substitute, needs none of the issue #22 experiments; draw_ calls it right after the drawn
program is found (before anything reads the program) and nothing else of draw_ - the scissor, viewport, the draw
commands and their parameters - knows about it; draw's caller puts the game's program back after every draw. Then
compiles the workaround with stubs and tests: E's identification (exact hashes, a vertex + a fragment stage - other
stage bits let through), D's selection and validation (its code hashed again at every use; one line when first kept),
E's fragment stage kept, the fallbacks (no D yet, D's code gone, no vertex or no fragment stage: E as the game asks,
with a line naming the stage mask; substitute not built: skipped as any building program), the kill switch, the hash
memo (once per program), other programs untouched, concurrent threads (each its own substitute; D captured and read
whole under the lock), and D / E drawn through exec.c's own CMD_BIND_PROGRAM composite (st.comp: a compute program's
stage left from an earlier bind - stage mask 0x23, the v0.0.30 release candidate's in-game fallback - geometry, binds
of some stages only, and unbound stages)."""
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'runtime/nvn/vulkan/exec.c').read_text()
nvn_h = (root / 'runtime/nvn/nvn.h').read_text()


def code_only(s):
    s = re.sub(r'/\*.*?\*/', ' ', s, flags=re.S)
    s = re.sub(r'"(?:\\.|[^"\\])*"', '""', s)
    return re.sub(r'//[^\n]*', ' ', s)


def one_space(s):
    return re.sub(r'\s+', ' ', s)


def block_after(text, head):
    i = text.index(head) + len(head)
    assert text[i] == '{', f'no block after {head!r}'
    depth = 0
    for j in range(i, len(text)):
        depth += {'{': 1, '}': -1}.get(text[j], 0)
        if depth == 0:
            return text[i + 1:j], j + 1
    raise AssertionError(f'unbalanced block after {head}')


fix_start = source.index('// ================================================================== issue #22: Terastallized Water moves')
fix_end = source.index('// ---- end of issue #22\n', fix_start)
dec_start = source.index('#define I22FIX_E_VH', fix_start)
dec_end = source.index('// ---- end of the issue #22 decision', dec_start)
fix_code = one_space(code_only(source[fix_start:fix_end]))
code = code_only(source)

# ---- the identities: exactly the validated ones
consts = dict(re.findall(r'#define (I22FIX_[EDF_VH]+) (0x[0-9a-f]+)ull', source[dec_start:dec_end]))
assert consts == {'I22FIX_E_VH': '0x09c1280fee686904', 'I22FIX_E_FH': '0x55b596e702185e46',
                  'I22FIX_D_VH': '0x2aadd88fa6502c59', 'I22FIX_D_FH': '0x55b596e702185e46'}, consts
print('ok   identities: E = 09c1280fee686904 / 55b596e702185e46, D = 2aadd88fa6502c59 / 55b596e702185e46')

# ---- audit: nothing recorded, nothing but the draw's program changed, no experiment involved
for pat in (r'\brec_\w+\(', r'\bvkCmd\w*', r'\bvk_cmd\(', r'\bvkQueue\w*', r'\bvkWait\w*', r'\bvk_wait\w*',
            r'\bvkDeviceWaitIdle\b', r'\bvk_checkpoint\(', r'\brenderer_submit\(', r'\bss\.', r'\bapply_scissor\(',
            r'\bvkCreate\w*', r'\bvma\w*', r'\bk[1-5]\w*\(', r'\bk[1-5]_\w+', r'\bi22_\w+', r'\bj[0-9]+_\w+', r'\bsetenv\(',
            r'\bfree\(', r'\bcalloc\(', r'\bmalloc\('):
    m = re.search(pat, fix_code)
    assert not m, f'the workaround must not call or touch {m.group(0)}'
assert re.findall(r'getenv\(([^)]*)\)', fix_code) == ['""'] and \
    '"LARECOMP_ISSUE22_FIX"' in one_space(source[fix_start:fix_end]), 'its own switch only'
assert 'LARECOMP_ISSUE22_K' not in code_only(source[fix_start:fix_end]), 'no experiment variable'
st_writes = re.findall(r'\bst\.([\w.\[\]>-]+)\s*(?:=(?!=)|&=|\|=|\+\+|--)', fix_code)
assert st_writes == ['program'], st_writes
assert fix_code.count('st.program = &i22fix_prog;') == 1
apply_, _ = block_after(fix_code, 'static int i22fix_apply(Prog **prp, const CmdRec *r, bool indexed, ShaderKey *vkey, '
                                  'ShaderKey *fkey) ')
assert re.match(r' if \(!i22fix_on\(\)\) return -1;', apply_), 'off: nothing'
wb = apply_.index('if (!wait_built(&dp->state, r, indexed, false)) return')
assert wb < apply_.index('*prp = dp;') < apply_.index('st.program = &i22fix_prog;'), 'set only once its program is built'
assert apply_.index('i22fix_make(&i22fix_prog, st.program, &d);') < apply_.index('get_program(&i22fix_prog, vkey, fkey)'), \
    'the substitute with E\'s own keys'
# D's stage: a value copy, written and read only under the lock; nothing of the game's program kept
assert not re.search(r'static [^;(]*NvnProgram \*', fix_code), 'no NvnProgram pointer kept'
assert re.search(r'static __thread NvnProgram i22fix_prog;', fix_code), 'the substitute: one per thread'
lock, unlock = 'pthread_mutex_lock(&i22fix_lock);', 'pthread_mutex_unlock(&i22fix_lock);'
for var in ('i22fix_d', 'i22fix_have_d', 'i22fix_d_frame'):
    for m in re.finditer(r'\b%s\b(?!\w)' % var, apply_):
        before = apply_[:m.start()]
        assert before.rfind(lock) > before.rfind(unlock), f'{var} touched outside the lock'
assert 'i22fix_d = st.program->stages[0];' in apply_, 'D\'s vertex stage copied by value'
assert apply_.index('d = i22fix_d;') < apply_.index('shader_stage_hash(&d)'), 'D\'s code hashed again at each use'
# the hash memo: release / acquire, written only there
memo, _ = block_after(fix_code, 'static void prog_code_hashes(Prog *p, const NvnProgram *np, uint64_t *vh, uint64_t *fh) ')
assert 'atomic_load_explicit(&p->code_h[0], memory_order_acquire)' in memo
assert memo.index('atomic_store_explicit(&p->code_h[1], f, memory_order_relaxed);') < \
    memo.index('atomic_store_explicit(&p->code_h[0], v, memory_order_release);')
assert memo.count('code_h') == 4 and len(re.findall(r'\bcode_h\b', code)) == 5, 'code_h: the field and the memo only'
print('ok   audit: no command, wait or allocation; only st.program / pr changed, once built; D kept by value under the '
      'lock and hashed again at each use; per-thread substitute; no experiment code or variable')

# ---- get_program never keys a cache on the NvnProgram's address (the substitute is a per-thread object)
for head in ('static Prog *get_program(NvnProgram *np, ShaderKey *vkey, ShaderKey *fkey) ',
             'static Prog *get_program_(NvnProgram *np, ShaderKey *vkey, ShaderKey *fkey) '):
    body, _ = block_after(one_space(code), head)
    uses = re.findall(r'\bnp\b(->|\)|,)?', body)
    bare = [u for u in uses if u != '->']
    # (passed on as itself only: get_program_(np, ...) and prog_fp_check(..., np), which reads np->stages)
    assert len(bare) == len(re.findall(r'get_program_\(np,|prog_fp_check\([^;]*, np\)', body)), (head, bare)
fpc, _ = block_after(one_space(code), 'static void prog_fp_check(Prog *p, NvnProgram *np) ')
assert re.findall(r'\bnp\b(->)?', fpc) == ['->', '->'], 'prog_fp_check reads np->stages only'
print('ok   get_program: programs found by code address, code fingerprint and keys - never by the NvnProgram\'s address')

# ---- draw_: right after the program is found, before anything reads it; nothing else knows of it
d_start = source.index('static void draw_(CmdRec *r, bool indexed) {')
draw = code_only(source[d_start:source.index('\n}\n', d_start)])
first = draw.index('Prog *pr = get_program(st.program, &vkey, &fkey);')
guard = draw.index('return;', first)
call = draw.index('int i22fix_skip = i22fix_apply(&pr, r, indexed, &vkey, &fkey);')
assert first < guard < call and re.fullmatch(r'return;\s*\}\s*', draw[guard:call]), 'right after the program\'s guard'
assert re.match(r'int i22fix_skip = i22fix_apply\(&pr, r, indexed, &vkey, &fkey\);\s*'
                r'if \(i22fix_skip >= 0\) \{ SKIP\(i22fix_skip\); return; \}', draw[call:])
assert len(re.findall(r'\bi22fix_\w+', draw)) == 4, 'the call and its skip only'
for later in ('pr->vi->attr_mask', 'rec_CmdBindPipelineObj(', 'scissor_rect()',
              'for (int ri = 0; ri < nruns; ri++) {'):
    if later in draw:
        assert call < draw.index(later), f'before {later}'
assert len(re.findall(r'\bi22fix_apply\(', code)) == 2, 'defined once, called once (from draw_)'
# the draw commands and their parameters: as without the workaround
loop, _ = block_after(draw, 'for (int ri = 0; ri < nruns; ri++) ')
assert 'i22fix' not in loop
direct = re.search(r'else if \(!use_index\) \{?\s*rec_CmdDraw\(cb, count, run_n\[ri\], first, base_inst \+ run_at\[ri\]\);', loop)
indexed_ = re.search(r'else \{?\s*rec_CmdDrawIndexed\(cb, icount, run_n\[ri\], 0, base_vertex, base_inst \+ run_at\[ri\]\);', loop)
assert direct and indexed_, 'the direct draws, with the game\'s parameters'
caller = one_space(code)
assert caller.count('NvnProgram *bound = st.program; draw_(r, indexed); st.program = bound;') == 1, \
    'the game\'s program back after every draw'
print('ok   draw_: the workaround right after the program is found; the draw loop, scissor and parameters untouched; '
      'the caller restores the game\'s program')

# ---- compiled, with stubs
harness = r'''
#include <assert.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static pthread_mutex_t log_mu = PTHREAD_MUTEX_INITIALIZER;
static char log_buf[1 << 18];
static size_t log_len;
static void test_log(const char *fmt, ...) {
    pthread_mutex_lock(&log_mu);
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(log_buf + log_len, sizeof log_buf - log_len, fmt, ap);
    if (n > 0) log_len += (size_t)n < sizeof log_buf - log_len ? (size_t)n : sizeof log_buf - log_len - 1;
    va_end(ap);
    pthread_mutex_unlock(&log_mu);
}
#define LOG(...) test_log(__VA_ARGS__)
'''

stubs = r'''
typedef struct { int k; } ShaderKey;
typedef struct { uint64_t x[8]; } CmdRec;
typedef struct { _Atomic int state; _Atomic uint64_t code_h[2]; int id; } Prog;
enum { SK_NO_PROGRAM, SK_NO_STAGES, SK_PROG_BUILDING, SK_PROG_FAILED };
enum { CMD_BIND_PROGRAM = 1 };
/* draw_'s state: one per translating thread; comp: the bound stages' composite (exec.c's CMD_BIND_PROGRAM) */
static __thread struct { NvnProgram *program; NvnProgram comp; } t_st;
#define st t_st
#define ENV(name) getenv(name)
static _Atomic uint64_t g_frame = 100;
static uint64_t vk_frame_counter(void) { return atomic_load(&g_frame); }
/* (stub) a stage's code hash: the first 8 bytes of its "code" - tests write the hash they want there */
static _Atomic int hash_calls;
uint64_t shader_stage_hash(const NvnShaderStage *s) {
    atomic_fetch_add(&hash_calls, 1);
    if (!s->code_addr || !s->const_offset) return 0;
    uint64_t h;
    memcpy(&h, (const void *)(uintptr_t)s->code_addr, 8);
    return h;
}
/* (stub) the program table: D's vertex stage + any fragment stage -> d_prog; its build state is the test's */
static Prog d_prog = {.id = 4};
static _Atomic int gp_calls;
static __thread const ShaderKey *gp_vkey, *gp_fkey;  /* (per thread: what this thread's last lookup got) */
static __thread const NvnProgram *gp_np;
static Prog *get_program(NvnProgram *np, ShaderKey *vkey, ShaderKey *fkey) {
    atomic_fetch_add(&gp_calls, 1);
    gp_np = np, gp_vkey = vkey, gp_fkey = fkey;
    return shader_stage_hash(&np->stages[0]) == 0x2aadd88fa6502c59ULL ? &d_prog : NULL;
}
static bool wait_built(_Atomic int *state, const CmdRec *r, bool indexed, bool pipe) {
    (void)r, (void)indexed, (void)pipe;
    return atomic_load(state) == 1;
}
'''

tail = r'''
#define E_VH 0x09c1280fee686904ULL
#define E_FH 0x55b596e702185e46ULL
#define D_VH 0x2aadd88fa6502c59ULL
static size_t count_of(const char *needle) {
    size_t n = 0;
    for (const char *p = log_buf; (p = strstr(p, needle)); p += strlen(needle)) n++;
    return n;
}
/* "code": 8 bytes of hash, then anything; a stage over it */
typedef struct { uint64_t h; uint8_t rest[56]; } Code;
static NvnShaderStage stage_of(Code *c, uint32_t stage, const uint8_t *control) {
    NvnShaderStage s = {.stage = stage, .code_addr = (uint64_t)(uintptr_t)c, .control = control, .const_offset = 0x40,
                        .const_size = 0x10};
    return s;
}
static NvnProgram program(Code *vs, Code *fs, uint32_t extra_mask) {
    static const uint8_t ctl[16];
    NvnProgram p;
    memset(&p, 0, sizeof p);
    p.stages[0] = stage_of(vs, 0, ctl);
    p.stages[1] = stage_of(fs, 1, ctl + 8);
    p.stage_mask = 3 | extra_mask;
    p.backend = (void *)0x5eed;
    return p;
}

static void decide(void) {
    const uint64_t vhs[] = {E_VH, D_VH, E_VH ^ 1, D_VH ^ 1, E_FH, 0xc435e3f4b9b1a4aaULL, 1};
    const uint64_t fhs[] = {E_FH, E_FH ^ 1, 0, E_VH};
    const uint32_t masks[] = {3, 1, 2, 7, 0x13, 0x23, 0x43, 0, 0x3f, 0x0b, 0x21, 0x22, 0x20, 0x3d, 0x3e};
    const uint64_t dvhs[] = {D_VH, D_VH ^ 1, 0, E_VH};
    int n = 0;
    for (size_t a = 0; a < sizeof vhs / sizeof vhs[0]; a++)
        for (size_t b = 0; b < sizeof fhs / sizeof fhs[0]; b++)
            for (size_t m = 0; m < sizeof masks / sizeof masks[0]; m++)
                for (int have = 0; have < 2; have++)
                    for (size_t c = 0; c < sizeof dvhs / sizeof dvhs[0]; c++) {
                        uint64_t vh = vhs[a], fh = fhs[b], dvh = dvhs[c];
                        bool e = vh == E_VH && fh == E_FH, d = vh == D_VH && fh == E_FH, vf = (masks[m] & 3) == 3;
                        int want = !e && !d ? I22FIX_NONE : !vf ? (e ? I22FIX_STAGES : I22FIX_NONE)
                                 : d ? I22FIX_IS_D : !have ? I22FIX_NO_D : dvh != D_VH ? I22FIX_D_GONE : I22FIX_SUB;
                        assert(i22fix_decide(vh, fh, masks[m], have, dvh) == want);
                        n++;
                    }
    /* exactly E: one bit off in either hash is not E */
    for (int bit = 0; bit < 64; bit++) {
        assert(i22fix_decide(E_VH ^ (1ULL << bit), E_FH, 3, true, D_VH) != I22FIX_SUB);
        assert(i22fix_decide(E_VH, E_FH ^ (1ULL << bit), 3, true, D_VH) != I22FIX_SUB);
        assert(i22fix_decide(E_VH, E_FH, 3, true, D_VH ^ (1ULL << bit)) == I22FIX_D_GONE);
        n += 3;
    }
    /* the stage mask, spelled out: extra stage bits (the composite's leftover compute 0x20, geometry 0x04,
       tessellation 0x08 / 0x10, any bit past them) never stop E's substitution nor D's capture; a missing vertex or
       fragment stage always does */
    const uint32_t extra[] = {0x23, 0x13, 0x07, 0x43, 0x0b, 0x3f, 0xff, 0x80000003u};
    for (size_t m = 0; m < sizeof extra / sizeof extra[0]; m++) {
        assert(i22fix_decide(E_VH, E_FH, extra[m], true, D_VH) == I22FIX_SUB);
        assert(i22fix_decide(D_VH, E_FH, extra[m], false, 0) == I22FIX_IS_D);
        assert(i22fix_decide(D_VH, E_FH, extra[m], true, D_VH) == I22FIX_IS_D);
        assert(i22fix_decide(E_VH, E_FH, extra[m], false, 0) == I22FIX_NO_D);
        n += 4;
    }
    const uint32_t missing[] = {1, 2, 0, 0x21, 0x22, 0x3d, 0x3e, 0x20};
    for (size_t m = 0; m < sizeof missing / sizeof missing[0]; m++) {
        assert(i22fix_decide(E_VH, E_FH, missing[m], true, D_VH) == I22FIX_STAGES);
        assert(i22fix_decide(E_VH, E_FH, missing[m], false, 0) == I22FIX_STAGES);
        assert(i22fix_decide(D_VH, E_FH, missing[m], false, 0) == I22FIX_NONE);  /* (D not kept) */
        n += 3;
    }
    printf("ok   i22fix_decide: %d cases - E only by both exact hashes, D's stage only while its code hashes as D's; "
           "stage masks 0x23 / 0x13 / 0x07 / 0x43 (and any other extra bits) substitute E and keep D, masks without "
           "the vertex or the fragment bit (1, 2, ...) fall back\n", n);
}

static void make(void) {
    Code ev = {.h = E_VH}, ef = {.h = E_FH}, dv = {.h = D_VH};
    NvnProgram e = program(&ev, &ef, 0), before = e, out;
    NvnShaderStage dvs = stage_of(&dv, 0, (const uint8_t *)0x1234);
    memset(&out, 0xAB, sizeof out);
    i22fix_make(&out, &e, &dvs);
    assert(!memcmp(&out.stages[0], &dvs, sizeof dvs));                      /* D's vertex stage, whole */
    assert(!memcmp(&out.stages[1], &e.stages[1], sizeof e.stages[1]));      /* E's fragment stage */
    assert(!memcmp(&out.stages[2], &e.stages[2], 4 * sizeof e.stages[2]));  /* the rest as E's */
    assert(out.stage_mask == e.stage_mask && out.backend == e.backend);
    assert(!memcmp(&e, &before, sizeof e));                                 /* E's own program untouched */
    printf("ok   i22fix_make: D's vertex stage; E's fragment stage, stage mask and the rest kept; E's program untouched\n");
}

static Code e_vs = {.h = E_VH}, e_fs = {.h = E_FH}, d_vs = {.h = D_VH}, d_vs2 = {.h = D_VH}, o_vs = {.h = 0xc435e3f4b9b1a4aaULL}, o_fs = {.h = 0xe9733d073a497506ULL};
static int draw_one(NvnProgram *np, Prog *own, Prog **out) {  /* draw_'s call, as there */
    ShaderKey vk = {1}, fk = {2};
    CmdRec r = {0};
    st.program = np;
    Prog *pr = own;
    int skip = i22fix_apply(&pr, &r, false, &vk, &fk);
    if (skip < 0 && pr == &d_prog) assert(gp_vkey && gp_fkey);  /* (the substitute looked up with this draw's keys) */
    *out = pr;
    return skip;
}

static void flow(void) {
    unsetenv("LARECOMP_ISSUE22_FIX");
    atomic_store(&d_prog.state, 1);
    NvnProgram e = program(&e_vs, &e_fs, 0), d = program(&d_vs, &e_fs, 0), o = program(&o_vs, &o_fs, 0);
    NvnProgram d_other_fs = program(&d_vs2, &o_fs, 0), e_geo = program(&e_vs, &e_fs, 1u << 2);
    Prog pe = {.id = 1}, pd = {.id = 2}, po = {.id = 3}, pdo = {.id = 5}, pe2 = {.id = 6};
    Prog *out;
    /* another program: untouched, hashed once */
    atomic_store(&hash_calls, 0);
    assert(draw_one(&o, &po, &out) == -1 && out == &po && st.program == &o);
    assert(atomic_load(&hash_calls) == 2);
    for (int i = 0; i < 100; i++) assert(draw_one(&o, &po, &out) == -1 && out == &po && st.program == &o);
    assert(atomic_load(&hash_calls) == 2 && atomic_load(&gp_calls) == 0);  /* memo: once per program */
    /* E before any D: drawn as the game asks, with a line */
    assert(draw_one(&e, &pe, &out) == -1 && out == &pe && st.program == &e);
    assert(strstr(log_buf, "[vk] issue #22: a draw of E (vs 09c1280fee686904 fs 55b596e702185e46) drawn with its own "
                           "vertex shader: no draw of D (vs 2aadd88fa6502c59) seen yet this session (stage mask 0x3, 1 so "
                           "far"));
    /* D's vertex shader with another fragment shader: not D, nothing kept */
    assert(draw_one(&d_other_fs, &pdo, &out) == -1 && out == &pdo);
    assert(!i22fix_have_d && count_of("[vk] issue #22: D's vertex shader") == 0);
    /* D: its vertex stage kept, its draw untouched; one line, the first time */
    assert(draw_one(&d, &pd, &out) == -1 && out == &pd && st.program == &d && i22fix_have_d);
    assert(!memcmp(&i22fix_d, &d.stages[0], sizeof d.stages[0]));
    assert(count_of("[vk] issue #22: D's vertex shader (vs 2aadd88fa6502c59 at ") == 1 &&
           count_of(", fs 55b596e702185e46, stage mask 0x3) kept for draws of E, frame ") == 1);
    for (int i = 0; i < 5; i++) assert(draw_one(&d, &pd, &out) == -1 && out == &pd);
    assert(count_of("[vk] issue #22: D's vertex shader") == 1);
    /* E: D's vertex shader, E's fragment shader, the substitute's program */
    NvnProgram e_before = e;
    assert(draw_one(&e, &pe, &out) == -1);
    assert(out == &d_prog && st.program == &i22fix_prog && gp_np == &i22fix_prog);
    assert(!memcmp(&st.program->stages[0], &d.stages[0], sizeof d.stages[0]));
    assert(!memcmp(&st.program->stages[1], &e.stages[1], sizeof e.stages[1]));
    assert(st.program->stage_mask == e.stage_mask && !memcmp(&e, &e_before, sizeof e));
    assert(count_of("[vk] issue #22 workaround: 1 draw(s) of E (vs 09c1280fee686904 fs 55b596e702185e46) drawn with "
                    "D's vertex shader (vs 2aadd88fa6502c59") == 1);
    /* the caller puts the game's program back (draw); the next E again */
    st.program = &e;
    for (int i = 2; i <= 10; i++) assert(draw_one(&e, &pe, &out) == -1 && out == &d_prog && st.program == &i22fix_prog);
    assert(count_of("[vk] issue #22 workaround: 10 draw(s) of E") == 1 && atomic_load(&i22fix_subs) == 10);
    /* the substitute's program not built yet / failed: skipped (as a building program), nothing changed */
    atomic_store(&d_prog.state, 0);
    assert(draw_one(&e, &pe, &out) == SK_PROG_BUILDING && out == &pe && st.program == &e);
    atomic_store(&d_prog.state, 2);
    assert(draw_one(&e, &pe, &out) == SK_PROG_FAILED && out == &pe && st.program == &e);
    atomic_store(&d_prog.state, 1);
    /* D's code gone (other code at its address): never used - E as the game asks */
    d_vs.h = 0x1111222233334444ULL;
    assert(draw_one(&e, &pe, &out) == -1 && out == &pe && st.program == &e);
    assert(strstr(log_buf, "drawn with its own vertex shader: the vertex stage kept from D's draw no longer holds D's code"));
    /* D drawn again (the next battle, its code elsewhere): kept again, used again */
    NvnProgram d2 = program(&d_vs2, &e_fs, 0);
    Prog pd2 = {.id = 7};
    assert(draw_one(&d2, &pd2, &out) == -1 && out == &pd2);
    assert(draw_one(&e, &pe, &out) == -1 && out == &d_prog && st.program->stages[0].code_addr == (uint64_t)(uintptr_t)&d_vs2);
    assert(count_of("[vk] issue #22: D's vertex shader") == 1);  /* (kept again: no second line) */
    /* E with another stage bound too (geometry): D's vertex shader all the same, E's stage mask kept */
    uint64_t subs = atomic_load(&i22fix_subs);
    assert(draw_one(&e_geo, &pe2, &out) == -1 && out == &d_prog && st.program == &i22fix_prog);
    assert(st.program->stage_mask == 7 && st.program->stages[0].code_addr == (uint64_t)(uintptr_t)&d_vs2);
    assert(!memcmp(&st.program->stages[1], &e_geo.stages[1], sizeof e_geo.stages[1]));
    assert(!memcmp(&st.program->stages[2], &e_geo.stages[2], sizeof e_geo.stages[2]));
    assert(atomic_load(&i22fix_subs) == subs + 1);
    /* E's hashes but no fragment / no vertex stage bit: as the game asks, with a line naming the mask */
    NvnProgram e_novs = program(&e_vs, &e_fs, 0), e_nofs = program(&e_vs, &e_fs, 0);
    e_novs.stage_mask = 2, e_nofs.stage_mask = 1;
    Prog pe3 = {.id = 8}, pe4 = {.id = 9};
    assert(draw_one(&e_nofs, &pe3, &out) == -1 && out == &pe3 && st.program == &e_nofs);
    assert(strstr(log_buf, "drawn with its own vertex shader: the program has no vertex stage or no fragment stage "
                           "(stage mask 0x1, 3 so far"));
    assert(draw_one(&e_novs, &pe4, &out) == -1 && out == &pe4 && st.program == &e_novs);
    assert(strstr(log_buf, "drawn with its own vertex shader: the program has no vertex stage or no fragment stage "
                           "(stage mask 0x2, 4 so far"));
    assert(atomic_load(&i22fix_kept) == 4 && atomic_load(&i22fix_subs) == subs + 1);
    assert(!strstr(log_buf, "stages other than vertex + fragment"));
    st.program = NULL;
    printf("ok   i22fix_apply: other programs untouched (hashed once); E before D, D's code gone, no vertex or no "
           "fragment stage -> E as the game asks with a line (with the stage mask); an extra stage -> substituted; D "
           "with another fragment shader not kept; D's first capture logged once; E -> D's vertex shader, E's fragment "
           "shader, E's keys; substitute not built -> skipped; D kept again after a reload\n");
}

static void off(void) {
    setenv("LARECOMP_ISSUE22_FIX", "0", 1);
    NvnProgram e = program(&e_vs, &e_fs, 0), d = program(&d_vs, &e_fs, 0);
    Prog pe = {.id = 1}, pd = {.id = 2}, *out;
    assert(draw_one(&d, &pd, &out) == -1 && out == &pd);
    assert(draw_one(&e, &pe, &out) == -1 && out == &pe && st.program == &e);
    assert(!i22fix_have_d && atomic_load(&i22fix_subs) == 0 && atomic_load(&hash_calls) == 0 && atomic_load(&gp_calls) == 0);
    assert(strstr(log_buf, "[vk] issue #22 workaround off (LARECOMP_ISSUE22_FIX=0)"));
    st.program = NULL;
    printf("ok   LARECOMP_ISSUE22_FIX=0: nothing done, not even a hash\n");
}

/* threads: two drawing E, one drawing D (two D stages in turn, as two battles' copies) */
static Code td_a = {.h = D_VH}, td_b = {.h = D_VH};
static _Atomic int stop, d_started;
static NvnProgram *mine[2];
static void *draw_e(void *arg) {
    int t = (int)(intptr_t)arg;
    NvnProgram e = program(&e_vs, &e_fs, 0);
    Prog pe = {.id = 10 + t}, *out;
    mine[t] = &i22fix_prog;
    for (int i = 0; i < 20000; i++) {
        assert(draw_one(&e, &pe, &out) == -1);
        if (out == &d_prog) {
            assert(st.program == &i22fix_prog);  /* this thread's own substitute */
            uint64_t a = st.program->stages[0].code_addr;
            assert(a == (uint64_t)(uintptr_t)&td_a || a == (uint64_t)(uintptr_t)&td_b);
            assert(st.program->stages[0].const_offset == 0x40 && st.program->stages[0].stage == 0);  /* never torn */
            assert(!memcmp(&st.program->stages[1], &e.stages[1], sizeof e.stages[1]));
        }
        st.program = &e;  /* (draw's caller) */
    }
    st.program = NULL;  /* (e ends here) */
    return NULL;
}
static void *draw_d(void *arg) {
    (void)arg;
    NvnProgram da = program(&td_a, &e_fs, 0), db = program(&td_b, &e_fs, 0);
    Prog pda = {.id = 20}, pdb = {.id = 21}, *out;
    for (int i = 0; !atomic_load(&stop); i++) {
        draw_one(i & 1 ? &da : &db, i & 1 ? &pda : &pdb, &out);
        atomic_store(&d_started, 1);
    }
    st.program = NULL;
    return NULL;
}
static void threads(void) {
    unsetenv("LARECOMP_ISSUE22_FIX");
    atomic_store(&d_prog.state, 1);
    pthread_t te[2], td;
    assert(!pthread_create(&td, NULL, draw_d, NULL));
    while (!atomic_load(&d_started)) ;
    for (int t = 0; t < 2; t++) assert(!pthread_create(&te[t], NULL, draw_e, (void *)(intptr_t)t));
    for (int t = 0; t < 2; t++) pthread_join(te[t], NULL);
    atomic_store(&stop, 1);
    pthread_join(td, NULL);
    assert(mine[0] && mine[1] && mine[0] != mine[1]);
    assert(atomic_load(&i22fix_subs) == 40000);
    printf("ok   threads: 40000 draws of E on two threads while D is kept from two copies - each thread its own "
           "substitute, D's stage never torn\n");
}

/* D and E drawn as the game binds them: through exec.c's own CMD_BIND_PROGRAM code (bind_program, compiled from
   exec.c), so the drawn program is the composite st.comp with whatever stages earlier binds left. NVN stage bits:
   vertex 0x1, fragment 0x2, geometry 0x4, tess control 0x8, tess eval 0x10, compute 0x20 (0: all). */
static Code c_cs = {.h = 0x0ade703718c689d4ULL}, c_gs = {.h = 0x6e6f6d657472790aULL};
static void draw_composite(Prog *own, int want_skip, Prog *want_out) {  /* draw's call and its caller's restore */
    Prog *out;
    NvnProgram *bound = st.program;
    assert(bound == &st.comp);
    assert(draw_one(&st.comp, own, &out) == want_skip && out == want_out);
    st.program = bound;
}
static void composite(void) {
    unsetenv("LARECOMP_ISSUE22_FIX");
    atomic_store(&d_prog.state, 1);
    static const uint8_t ctl[16];
    NvnProgram cs, gs, e = program(&e_vs, &e_fs, 0), d = program(&d_vs, &e_fs, 0);
    memset(&cs, 0, sizeof cs), memset(&gs, 0, sizeof gs);
    cs.stages[5] = stage_of(&c_cs, 5, ctl), cs.stage_mask = 0x20;
    gs.stages[2] = stage_of(&c_gs, 2, ctl), gs.stage_mask = 0x04;
    Prog pd = {.id = 30}, pe = {.id = 31}, pe_nofs = {.id = 32}, pe_novs = {.id = 33};
    /* 1. a dispatch's compute program, then D and E bound with every graphics bit (0x1f): the compute stage stays in
          the composite - stage mask 0x23, what the v0.0.30 release candidate's draws of E had in game */
    bind_program(&cs, 0x20);
    assert(st.program == &st.comp && st.comp.stage_mask == 0x20);
    bind_program(&d, 0x1f);
    assert(st.comp.stage_mask == 0x23 && st.comp.stages[5].code_addr == (uint64_t)(uintptr_t)&c_cs);
    draw_composite(&pd, -1, &pd);
    assert(i22fix_have_d && !memcmp(&i22fix_d, &d.stages[0], sizeof d.stages[0]));
    assert(count_of("[vk] issue #22: D's vertex shader (vs 2aadd88fa6502c59") == 1 &&
           count_of("stage mask 0x23) kept for draws of E") == 1);
    bind_program(&e, 0x1f);
    assert(st.comp.stage_mask == 0x23);
    Prog *out;
    assert(draw_one(&st.comp, &pe, &out) == -1 && out == &d_prog && st.program == &i22fix_prog);
    assert(!memcmp(&st.program->stages[0], &d.stages[0], sizeof d.stages[0]));      /* D's vertex shader */
    assert(!memcmp(&st.program->stages[1], &e.stages[1], sizeof e.stages[1]));      /* E's fragment shader */
    assert(st.program->stage_mask == 0x23 && st.program->stages[5].code_addr == (uint64_t)(uintptr_t)&c_cs);
    assert(gp_np == &i22fix_prog);
    st.program = &st.comp;  /* (draw's caller) */
    assert(st.comp.stages[0].code_addr == (uint64_t)(uintptr_t)&e_vs);  /* the composite itself untouched */
    assert(atomic_load(&i22fix_subs) == 1 && count_of("[vk] issue #22 workaround: 1 draw(s) of E") == 1);
    /* 2. a geometry program bound, then D and E by their vertex + fragment bits only (0x03): 0x27 */
    bind_program(&gs, 0x04);
    bind_program(&d, 0x03);
    assert(st.comp.stage_mask == 0x27);
    draw_composite(&pd, -1, &pd);
    bind_program(&e, 0x03);
    assert(st.comp.stage_mask == 0x27);
    draw_composite(&pe, -1, &d_prog);
    assert(atomic_load(&i22fix_subs) == 2);
    /* 3. E bound with 0 (every stage): the composite is E's own program again, 0x03 */
    bind_program(&e, 0);
    assert(st.comp.stage_mask == 0x03 && !st.comp.stages[5].code_addr && !st.comp.stages[2].code_addr);
    draw_composite(&pe, -1, &d_prog);
    assert(atomic_load(&i22fix_subs) == 3);
    /* 4. E's fragment stage unbound, a compute program bound again (0x21): never substituted, E as it is */
    bind_program(&cs, 0x20);
    bind_program(NULL, 0x02);
    assert(st.comp.stage_mask == 0x21 && !st.comp.stages[1].code_addr);
    draw_composite(&pe_nofs, -1, &pe_nofs);
    /* 5. E's vertex stage unbound, its fragment stage bound: never substituted */
    bind_program(&e, 0x02);
    bind_program(NULL, 0x01);
    assert(st.comp.stage_mask == 0x22 && !st.comp.stages[0].code_addr);
    draw_composite(&pe_novs, -1, &pe_novs);
    assert(atomic_load(&i22fix_subs) == 3 && atomic_load(&i22fix_kept) == 0);
    assert(!count_of("drawn with its own vertex shader") && count_of("[vk] issue #22: D's vertex shader") == 1);
    st.program = NULL;
    printf("ok   composite (exec.c's CMD_BIND_PROGRAM): compute then D / E with 0x1f (stage mask 0x23) -> D kept, E "
           "substituted (the composite's compute stage carried along, as K5); geometry + 0x03 binds (0x27) and 0 binds "
           "(0x03) -> substituted; fragment or vertex stage unbound -> never substituted; D's line once\n");
}

int main(int argc, char **argv) {
    assert(argc == 2);
    if (!strcmp(argv[1], "decide")) decide();
    else if (!strcmp(argv[1], "make")) make();
    else if (!strcmp(argv[1], "flow")) flow();
    else if (!strcmp(argv[1], "off")) off();
    else if (!strcmp(argv[1], "threads")) threads();
    else if (!strcmp(argv[1], "composite")) composite();
    else return 2;
    return 0;
}
'''

types = ''.join(re.search(p, nvn_h, re.S).group(0) + '\n' for p in
                (r'typedef struct NvnShaderStage \{.*?\} NvnShaderStage;', r'typedef struct NvnProgram \{.*?\} NvnProgram;'))
# exec.c's own CMD_BIND_PROGRAM (both command-list walkers: the same code), compiled as bind_program for the composite
# test - st.program is st.comp, whose stage bits persist from earlier binds
binds = []
for m in re.finditer(r'case CMD_BIND_PROGRAM: (?=\{)', source):
    body, _ = block_after(source[m.start():], 'case CMD_BIND_PROGRAM: ')
    binds.append(body)
assert len(binds) == 2 and one_space(code_only(binds[0])) == one_space(code_only(binds[1])), 'two identical binds'
bind_code = one_space(code_only(binds[0]))
assert 'st.comp.stage_mask |= 1u << i;' in bind_code and 'st.comp.stage_mask &= ~(1u << i);' in bind_code and \
    'st.program = st.comp.stage_mask ? &st.comp : NULL;' in bind_code and 'if (!m) m = 0x3F;' in bind_code, \
    'the composite as the composite test models it'
bind_src = ('static void bind_program(NvnProgram *bp, uint32_t bits) {\n'
            '    CmdRec rec = {{(uint64_t)(uintptr_t)bp, bits}};\n'
            '    CmdRec *r = &rec;\n'
            '    switch (CMD_BIND_PROGRAM) {\n'
            '    case CMD_BIND_PROGRAM: {' + binds[0] + '}\n'
            '    }\n'
            '}\n')
with tempfile.TemporaryDirectory() as td:
    c_path = os.path.join(td, 't_fix.c')
    bin_path = os.path.join(td, 't_fix')
    with open(c_path, 'w') as f:
        f.write(harness + types + stubs + source[fix_start:fix_end] + '\n' + bind_src + tail)
    cc = shlex.split(os.environ.get('CC', 'cc'))
    flags = ['-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread', '-O1']
    subprocess.run(cc + flags + [c_path, '-o', bin_path], check=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith('LARECOMP_')}
    for mode in ('decide', 'make', 'flow', 'off', 'threads', 'composite'):
        subprocess.run([bin_path, mode], check=True, env=env)
    # the threads again under ThreadSanitizer, where the compiler has it
    tsan = os.path.join(td, 't_fix_tsan')
    if subprocess.run(cc + flags + ['-fsanitize=thread', '-g', c_path, '-o', tsan], capture_output=True).returncode == 0:
        r = subprocess.run([tsan, 'threads'], capture_output=True, text=True, env=env)
        if 'unexpected memory mapping' in r.stderr or 'FATAL: ThreadSanitizer' in r.stderr:
            print('skip threads under ThreadSanitizer (it cannot run in this environment)')
        else:
            assert r.returncode == 0 and 'WARNING: ThreadSanitizer' not in r.stderr, r.stderr[-3000:]
            print('ok   threads under ThreadSanitizer: no data race')
    else:
        print('skip threads under ThreadSanitizer (not available with this compiler)')
print('PASS: issue #22 production workaround')
