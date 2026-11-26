"""PR #43 / issue #27: the gfast switches (runtime/nvn/vulkan/exec.c gfast_storage_on / gfast_ubo_stage_on), compiled
from exec.c itself and checked for every LARECOMP_VK_GMEM_FAST x LARECOMP_VK_UBO_STAGE combination, discrete and
integrated GPU; and gfast_prepare's use of them (storage ranges behind `on`, the uniform-buffer loop behind `ubo_on`)."""
import os, re, shlex, subprocess, tempfile
from pathlib import Path
src = (Path(__file__).resolve().parents[1] / 'runtime/nvn/vulkan/exec.c').read_text()
a = src.index('// ---- gfast switches'); b = src.index('// ---- end of the gfast switches')
prep = src[b:src.index('\n}\n', b)]
assert 'on = gfast_storage_on(ENV("LARECOMP_VK_GMEM_FAST"), g_vk.props.deviceType == VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU)' in prep
assert 'ubo_on = gfast_ubo_stage_on(ENV("LARECOMP_VK_GMEM_FAST"), ENV("LARECOMP_VK_UBO_STAGE"))' in prep
assert 'bool do_in = on && ' in prep and re.search(r'for \(int i = 0; ubo_on && i < 16 && total < GMAP_MAX; i\+\+\)', prep)
# (GMEM_FAST, UBO_STAGE) -> (storage on: discrete, integrated; ubo staging on)
cases = {(None, None): (1, 0, 1), ('0', None): (0, 0, 0), ('1', None): (1, 1, 1), ('0', '0'): (0, 0, 0),
         ('0', '1'): (0, 0, 1), ('1', '0'): (1, 1, 0), ('1', '1'): (1, 1, 1), ('x', None): (0, 0, 0), (None, '0'): (1, 0, 0)}
lit = lambda v: 'NULL' if v is None else '"%s"' % v
body = ''.join('    if (gfast_storage_on(%s, false) != %d || gfast_storage_on(%s, true) != %d || gfast_ubo_stage_on(%s, %s) != %d) '
               '{ printf("FAIL %s %s\\n"); bad++; }\n' % (lit(f), s, lit(f), si, lit(f), lit(u), uo, f, u) for (f, u), (s, si, uo) in cases.items())
c = '#include <stdbool.h>\n#include <stdio.h>\n#include <stdlib.h>\n' + src[a:b] + 'int main(void) { int bad = 0;\n' + body + '    return bad; }\n'
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, 't.c'); Path(p).write_text(c)
    subprocess.run(shlex.split(os.environ.get('CC', 'cc')) + ['-Wall', '-Werror', '-Wno-unused-function', p, '-o', p + '.x'], check=True)
    r = subprocess.run([p + '.x'], capture_output=True, text=True); print(r.stdout, end='')
    assert r.returncode == 0, 'switch table mismatch'
print('PASS: gfast switches - %d GMEM_FAST x UBO_STAGE cases (discrete and integrated), and gfast_prepare uses them' % len(cases))
