#!/usr/bin/env python3
"""Unit & edge-case tests for PR #61's rt_sv_lua_clock_slot() in runtime/hle/framepace.c."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent

TEST_C_HEADER = r'''
#include <assert.h>
#include <math.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define ENV(k) getenv(k)
#define LOG(...) ((void)0)
#define RT_ON_RESET(fn) static void fn(void)

static bool rt_pla_is_60fps(void) {
    const char *e = getenv("TEST_60FPS");
    return !e || atoi(e) != 0;
}
'''

TEST_C_MAIN = r'''
typedef struct {
    uint8_t tstring[0x40];
    uint8_t node[24];
    uint8_t tbl[0x40];
    uint8_t arr[16 * 48];
    uint8_t closure[0x30];
    uint8_t proto[0x40];
    uint8_t stack[0x40];
    uint8_t ci_caller[0x30];
    uint8_t ci_cur[0x30];
    uint8_t L[0x40];
    uint8_t slot[0x20];
} FakeCam;

static void init_cam(FakeCam *c, int linedef, int sizecode, double f30) {
    memset(c, 0, sizeof *c);
    const char *mname = "FC0150FC2959FFA71";
    c->tstring[0xb] = (uint8_t)strlen(mname);
    memcpy(c->tstring + 0x18, mname, strlen(mname));

    uint64_t cl_ptr = (uint64_t)(uintptr_t)c->closure;
    uint64_t p_ptr = (uint64_t)(uintptr_t)c->proto;
    memcpy(c->closure + 0x18, &p_ptr, 8);
    memcpy(c->proto + 0x2c, &linedef, 4);
    memcpy(c->proto + 0x18, &sizecode, 4);

    // Hash node with method key -> Lua closure (tag 6)
    memcpy(c->node + 0, &cl_ptr, 8);
    c->node[8] = 6;
    c->node[9] = 0x44;
    uint64_t ts_ptr = (uint64_t)(uintptr_t)c->tstring;
    memcpy(c->node + 16, &ts_ptr, 8);

    // Table: alimit=48, array=c->arr, lsizenode=0, node=c->node
    uint32_t alimit = 48;
    uint64_t arr_ptr = (uint64_t)(uintptr_t)c->arr;
    uint64_t node_ptr = (uint64_t)(uintptr_t)c->node;
    memcpy(c->tbl + 0xc, &alimit, 4);
    memcpy(c->tbl + 0x10, &arr_ptr, 8);
    c->tbl[0xb] = 0;
    memcpy(c->tbl + 0x18, &node_ptr, 8);

    // [21] = acc = 0.0, [41] = f = f30 (float tag 0x13)
    c->arr[16 * 20 + 8] = 0x13;
    c->arr[16 * 40 + 8] = 0x13;
    memcpy(c->arr + 16 * 40, &f30, 8);

    // self (R0 at base + 16) -> tbl (tag 5)
    uint64_t tbl_ptr = (uint64_t)(uintptr_t)c->tbl;
    memcpy(c->stack + 16, &tbl_ptr, 8);
    c->stack[16 + 8] = 5;

    uint64_t base_ptr = (uint64_t)(uintptr_t)c->stack;
    uint64_t caller_ptr = (uint64_t)(uintptr_t)c->ci_caller;
    uint64_t cur_ptr = (uint64_t)(uintptr_t)c->ci_cur;
    memcpy(c->ci_caller + 0, &base_ptr, 8);
    memcpy(c->ci_cur + 0x10, &caller_ptr, 8);
    memcpy(c->L + 0x20, &cur_ptr, 8);

    uint64_t dur_ns = 16666667ULL;  // ~1/60 s
    memcpy(c->slot + 8, &dur_ns, 8);
}

int main(void) {
    const char *cam_env = getenv("LARECOMP_SV_LUA_CAMERA");
    bool cam_on = !cam_env || atoi(cam_env) != 0;

    // 1. Valid indoor camera at 60 FPS -> scales f and sets acc for 1 step
    FakeCam cam;
    init_cam(&cam, 279349, 243, 0.4);
    rt_sv_lua_clock_slot((uint64_t)(uintptr_t)cam.L, 2, (uint64_t)(uintptr_t)cam.slot);
    double f_got = 0, acc_got = 0;
    memcpy(&f_got, cam.arr + 16 * 40, 8);
    memcpy(&acc_got, cam.arr + 16 * 20, 8);
    if (cam_on) {
        double one_minus = 1.0 - f_got;
        assert(fabs(one_minus * one_minus - 0.6) < 1e-4);
        assert(acc_got + (16666667.0 / 1e9) >= (1.0 / 30.0));
    } else {
        assert(fabs(f_got - 0.4) < 1e-9);
        assert(fabs(acc_got) < 1e-9);
    }

    // 2. Switching back to 30 FPS restores original f30=0.4
    setenv("TEST_60FPS", "0", 1);
    rt_sv_lua_clock_slot((uint64_t)(uintptr_t)cam.L, 2, (uint64_t)(uintptr_t)cam.slot);
    memcpy(&f_got, cam.arr + 16 * 40, 8);
    assert(fabs(f_got - 0.4) < 1e-9);
    setenv("TEST_60FPS", "1", 1);

    // 3. Different Lua Proto (not indoor camera) -> untouched
    FakeCam other;
    init_cam(&other, 12345, 99, 0.4);
    rt_sv_lua_clock_slot((uint64_t)(uintptr_t)other.L, 2, (uint64_t)(uintptr_t)other.slot);
    memcpy(&f_got, other.arr + 16 * 40, 8);
    assert(fabs(f_got - 0.4) < 1e-9);

    // 4. Null / wrong slot guards -> no crash
    rt_sv_lua_clock_slot(0, 2, 0);
    rt_sv_lua_clock_slot((uint64_t)(uintptr_t)cam.L, 1, (uint64_t)(uintptr_t)cam.slot);

    // 5. > 8 cameras handled safely without overflow
    FakeCam cams[10];
    for (int i = 0; i < 10; i++) {
        init_cam(&cams[i], 279349, 243, 0.4);
        rt_sv_lua_clock_slot((uint64_t)(uintptr_t)cams[i].L, 2, (uint64_t)(uintptr_t)cams[i].slot);
    }

    printf("PASS: sv indoor camera 60fps tests\n");
    return 0;
}
'''


def main():
    if os.name == "nt":
        return
    cc = os.environ.get("CC") or shutil.which("clang") or shutil.which("gcc")
    assert cc, "No C compiler found"
    fp_text = (ROOT / "runtime" / "hle" / "framepace.c").read_text(encoding="utf-8")
    m_find = fp_text.find("static const uint8_t *lua_find(")
    m_find_end = fp_text.find("void rt_sv_lua_frame_time(")
    m_cam = fp_text.find("static uint8_t *lua_index(")
    assert m_find != -1 and m_find_end != -1 and m_cam != -1
    snippet = fp_text[m_find:m_find_end] + "\n" + fp_text[m_cam:]

    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        src = t / "test_sv_indoor_camera.c"
        exe = t / "test_sv_indoor_camera"
        src.write_text(TEST_C_HEADER + "\n" + snippet + "\n" + TEST_C_MAIN, encoding="utf-8")
        cmd = shlex.split(cc) + [
            "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Wno-unused-function",
            str(src), "-lm", "-pthread", "-o", str(exe)
        ]
        subprocess.run(cmd, cwd=ROOT, check=True)
        subprocess.run([str(exe)], cwd=ROOT, check=True)
        env = os.environ.copy()
        env["LARECOMP_SV_LUA_CAMERA"] = "0"
        subprocess.run([str(exe)], cwd=ROOT, env=env, check=True)


if __name__ == "__main__":
    main()
