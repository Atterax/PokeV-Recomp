# Issue #22: GPU stop on Terastallized Water-type moves (NVIDIA)

**Status (v0.0.30):** a workaround ships, on by default (`LARECOMP_ISSUE22_FIX=0` turns it off). Its mechanism was
validated in game as experiment K5. The production build's in-game validation is recorded in section 7.
**The root cause is not known.**

Each statement below is tagged with how we know it:
- **[shown]**: demonstrated by a recorded run or a test;
- **[inferred]**: follows from shown facts but was not observed directly;
- **[hypothesis]**: not established.

## 1. Symptom

- **[shown]** In *Pokémon Violet* 4.0.0 (main build ID `709BFD6611529864`), a Terastallized Water-type move's effect
  stops the GPU on the effect's first frame. Reproduced with Kyogre's Water Spout after Water Terastallization, in the
  Elite Four battle against Larry. The log shows `[vk] timeout: ...` and
  `FATAL: [vk] GPU did not finish submission N within 10 s`, and the kernel logs `NVRM: Xid ... 109`
  (CTX SWITCH TIMEOUT). Every hang run gave Xid 109.
- **[shown]** At the timeout the Vulkan device was not lost: the timeline semaphore query still returned `VK_SUCCESS`.

## 2. Hardware and software of every run

| | |
|---|---|
| GPU | NVIDIA GeForce RTX 5070 Ti (Blackwell, 16 GB), Vulkan 1.4.357 |
| Driver | NVIDIA 620.73.04, open kernel module |
| OS | Bazzite 45 (Fedora-based), Linux 7.3.5, x86-64 |
| CPU | AMD Ryzen 7 7800X3D |
| Game code | Linux `GAMELIB_ID` `98edf5304c9c7e63`: the PR #42 generator, the same as v0.0.30's AppImage produces |

**Not tested:** other NVIDIA GPUs or drivers, AMD or Intel GPUs, Windows, macOS, Steam Deck.

## 3. The draws involved

**[shown]** The stop happens in a 960x540 pass that draws five soft-particle billboards, A to E. The two that matter
are D and E. They share the same fragment shader, at the same code address:

| Draw | Vertex shader | Fragment shader | Command |
|---|---|---|---|
| D | `2aadd88fa6502c59` | `55b596e702185e46` | |
| E | `09c1280fee686904` | `55b596e702185e46` | `vkCmdDraw(4, 1..5, 0, 0)` |

Hashes are `shader_stage_hash`: the shader code up to the end of its constants.

**[shown]** D and E vertex shaders:
- **Interface:** the same declarations and the same 17 attribute components read, with byte-identical compiler
  constants.
- **Inputs:** the same per-instance vertex layout.
- **Code:** no loops and no memory writes.
- **CPU evaluation:** D's code gives exactly E's outputs from E's captured inputs.

## 4. Experiments (isolated data roots, never the player's folder)

| Run | What changed | Result | What it shows |
|---|---|---|---|
| A–G | recorder, shader prep, session programs, shader dumps, checkpoints, GPU overlap off, spec-compliant timeout queries | HANG (Xid 109), every time | none of these causes it |
| H | each of A–E left out in turn | hang with A, B, C or D left out; **no hang with E left out** | E is needed |
| J6 / J8 | E left out only at one parameter set | HANG (E came with other parameters) | E must be matched by program |
| I | D's and E's inputs captured, evaluated on the CPU | sane | nothing malformed in E's data |
| K1 | E with a 0x0 scissor (no fragments) | HANG | E's fragment side is not needed |
| K2 | E bound, zero-count indirect draw | **no hang** | E's vertex work is needed |
| K2-R | as K1, indirect draw with E's real counts | HANG | the indirect path doesn't avoid it |
| K3 | as K2-R, with **D's vertex shader** in place of E's | **no hang** (40/40) | E's own compiled vertex shader is needed |
| K4 | K3 with E's own scissor (fragments drawn) | **no hang**: 4 Water Spouts, 0 errors | holds with E rendered normally |
| K5 | **only** D's vertex shader in place of E's: the game's own direct `vkCmdDraw`, parameters, scissor and fragment shader | **no hang**: **219/219** E drawn so; 0 indirect draws, 0 parameter or scissor mismatches, 0 substitution failures, 0 errors, 0 timeouts, 0 Xid; exit 0 | the substitution alone avoids it |

The experiment code (`LARECOMP_ISSUE22_K1` … `_K5` and the others) is **not part of the release**. It stays on the local
diagnostic branch `diag/issue-22-timeout-dump`. Each run's logs, summaries and launchers are in the investigation folder
outside the repository.

### Not established

- **[hypothesis]** Why E's vertex shader stops this GPU: a driver or compiler problem with that shader, or something in
  our Maxwell-to-GLSL translation of it that is valid but triggers one. Nothing here tells them apart.
- **[not shown]** That E's own pipeline object is defective. In K3–K5, E was drawn with D's program and pipeline, so
  the change tested is "D's vertex shader, through D's pipeline".
- **[not shown]** That no other move or effect has a draw like E. Only this effect was traced.
- **[inferred]** That E looks the same with D's vertex shader. The CPU evaluation gives identical outputs, and
  screenshots of later Water Spouts look plausible, but there was no side-by-side comparison against a reference.

## 5. The workaround (`runtime/nvn/vulkan/exec.c`, `i22fix_*`)

**What it does.** A draw whose program is E (both exact hashes, vertex and fragment stages only) is drawn with D's
vertex stage. Everything else is E's: fragment shader, vertex streams, constant buffers, textures, keys, fixed-function
state, scissor, viewport, and the direct draw with E's parameters. No other draw is touched, and nothing is added to
the command buffer.

**How the substitute is built.**
- **Program and pipeline.** The substitute is E's program with `stages[0]` replaced by D's. `get_program` finds
  programs by code address, code fingerprint and keys, so it returns the program D draws with, and the pipeline is D's.
  **The pipeline identity is part of the validated workaround.**
- **Where D's stage comes from.** It is copied by value from D's last draw (the pass draws D before E), under a mutex
  because shadow workers draw in parallel. D's code is hashed again before every use, because a battle's shaders load
  into new memory each time.
- **Lifetime.** The substitute is per-thread and is the bound program for that one draw only. The caller restores the
  game's program after every draw.
- **Identification.** Hashes are computed once per program and published with release/acquire ordering.

**When it can't substitute.**
- **No valid D** (not drawn yet, its code gone, or no vertex or no fragment stage): E is drawn as the game asks, with
  `[vk] issue #22: a draw of E ... drawn with its own vertex shader: <reason> (stage mask <m>, ...)` in the log. There
  is never another shader.
- **Other stage bits don't count.** The drawn program is the bound stages' composite (`st.comp`), which keeps a stage
  from an earlier bind: after a compute dispatch, graphics draws can have stage mask `0x23`. Draws are built from the
  vertex and fragment stages only, so D and E need just those two. The first v0.0.30 build required *exactly* vertex +
  fragment: in game both draws of E fell back ("stages other than vertex + fragment"; D's draw, in the same pass, was
  most likely refused the same way - its capture wasn't logged), and the GPU stopped (Xid 109). The fallback line
  now names the stage mask, and D's first capture is logged.
- **Substitute's program not built yet:** the draw is skipped, as for any program still building.

**What shows it is working.** `[vk] issue #22: D's vertex shader (...) kept for draws of E` once, at D's first draw;
then `[vk] issue #22 workaround: N draw(s) of E ... drawn with D's vertex shader` at the 1st, 10th, 100th and 1000th
draw, then every 10000th.

**Tests.** `tools/test_issue22_fix.py`, run in Linux CI:
- **source audit:** no Vulkan command, wait, allocation or experiment variable in the workaround; only `st.program`
  and the draw's program change, once built; D kept by value under the lock; the call right after the program lookup;
  the draw loop untouched; the caller restores the game's program;
- **identification:** E and D only by both exact hashes;
- **stage masks:** `0x23`, `0x13`, `0x07`, `0x43` and other extra bits (`0x0b`, `0x3f`, `0xff`, `0x80000003`)
  substitute E and keep D; masks without the vertex or the fragment bit (`1`, `2`, `0`, `0x21`, `0x22`, `0x3d`,
  `0x3e`, `0x20`) fall back, with the mask in the log line;
- **D's validation:** its code hashed again at every use; a replaced D never used; D kept again after a reload (its
  code at a new address); its first capture logged once;
- **the other fallbacks and the kill switch** (`LARECOMP_ISSUE22_FIX=0`: nothing hashed or changed);
- **once-per-program hashing;**
- **the composite:** `exec.c`'s own `CMD_BIND_PROGRAM` code (both copies checked identical), compiled into the test and
  driven by real bind sequences: a compute bind then D and E with `0x1f` (mask `0x23`), a geometry bind then `0x03`
  binds (`0x27`), and a `0` bind (`0x03`) - E substituted; E's fragment or vertex stage unbound - never substituted;
- **threads:** 40,000 draws of E on two threads while D is kept again and again, each thread its own substitute, D's
  stage never torn. The same run under ThreadSanitizer reports no data race - **only where the compiler supports it**
  (the Ubuntu 22.04 / clang 21 build container); elsewhere that step is skipped and says so.

**Mutation checks.**
- **Final code (targeted, all caught):** the stage check back to `& 0x3f`, and changed to `& 0x1f`, `& 1`, `& 2`, or
  removed; D's capture line never logged, or logged at every draw; the stage mask left out of the fallback line;
  `CMD_BIND_PROGRAM`'s "0 = every stage" changed in either of its two copies.
- **First implementation (historical):** 21 of 21 deliberate mutations were caught. That set was **not** repeated on
  the final code.

## 6. Known limitations

- **One configuration.** Validated only on the hardware above. It is not limited to NVIDIA, so on AMD, Intel and
  macOS, where no hang was reported, E's vertex shader is changed too. The change is output-equivalent in the CPU
  evaluation, but was not checked visually there.
- **Depends on D being drawn first.** If E came before any D in a session, it would be drawn as before, with the log
  line.
- **Exact hashes.** A game update that changes either shader disables the workaround for it (no match), rather than
  misapplying it.
- **Experiments need it off.** Rerunning the experiments on top of this code needs `LARECOMP_ISSUE22_FIX=0`, because
  they select E by its own program.

## 7. Reproduce and validate

1. In a scratch data root, never the player's folder, use Water Spout with a Water-Terastallized Kyogre, several times.
2. The log should show `[vk] issue #22 workaround: 1 draw(s) of E ...` (then 10, 100), and none of these:
   - `drawn with its own vertex shader`;
   - `[vk] timeout:`;
   - `FATAL: [vk] GPU did not finish`;
   - `Xid` in `journalctl -k`.
3. Take F12 screenshots during the effect.
4. To see the original stop again, set `LARECOMP_ISSUE22_FIX=0`. On the hardware above, expect the timeout and Xid 109
   on the effect's first frame.

### Validation of the v0.0.30 build

**First candidate: hung.** Before the stage-mask correction (section 5), both draws of E fell back to E's own vertex
shader ("the program has stages other than vertex + fragment", frames 15703 and 15704), and the GPU stopped: `GPU did
not finish submission 79834 within 10 s`, kernel Xid 109. **[shown]**

**Final build: no hang.** **[shown]** The AppImage with its default settings (recorder, shader preparation, session
programs and GPU overlap as players get them), in an isolated data root:

| | |
|---|---|
| GPU | NVIDIA GeForce RTX 5070 Ti, driver 620.73.04 |
| System | Bazzite 45, kernel 7.3.5-ogc2.1.fc45, AMD Ryzen 7 7800X3D |
| AppImage | SHA-256 `61f0527b1d87a20a1143a036585a23de694c9cafb705c6a75587f975e3881b0c` |
| Game code | `GAMELIB_ID` `98edf5304c9c7e63` (no rebuild during the run) |

- **D kept:** `[vk] issue #22: D's vertex shader (vs 2aadd88fa6502c59 at 745465a000, fs 55b596e702185e46, stage mask
  0x23) kept for draws of E`. The `0x23` is the composite's leftover compute stage that the first candidate refused.
- **D kept again at a new address:** later draws of E used D's vertex shader at `76c359f000`, kept from a later draw
  of D. D's code was found again at its new address, and the substitution went on.
- **E substituted:** the 1st, 10th and 100th draws of E were drawn with D's vertex shader, and **0** draws of E with
  their own (no `drawn with its own vertex shader` line).
- **No hang:** no `[vk] timeout:`, no `FATAL: [vk] GPU did not finish`, and no Xid in `journalctl -k`. About 60 fps.
- **Clean exit:** the window closed normally (exit code 0), and the player's own data folder was unchanged (the same
  fingerprint before and after).
- **Picture not checked:** no screenshots were taken in this run.
