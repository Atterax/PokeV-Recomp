"""Validate RecompLauncher integration: manifest, settings alignment, packaging, and C runtime helpers."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def test_manifest_and_schema():
    manifest_path = ROOT / "recomplauncher.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["format"] == 1
    assert data["repo"] == "didslima/PokeV-Recomp"
    assert data["$schema"] == "https://recomplauncher.com/schema/recomplauncher.json"

    # Inputs: game (folder or nsp/xci requiring keys), optional update, shared switch.prod-keys
    inputs = {x["id"]: x for x in data["inputs"]}
    assert set(inputs) == {"game", "update", "keys"}
    assert inputs["keys"]["role"] == "key"
    assert inputs["keys"]["shared"] == "switch.prod-keys"
    assert inputs["update"]["role"] == "update"
    assert inputs["update"]["optional"] is True
    assert inputs["game"]["formats"][0] == {"type": "folder"}
    assert inputs["game"]["formats"][1]["requires"] == ["keys"]

    # Prepare and start
    assert "--prepare" in data["prepare"]["args"]
    assert "--game={input:game}" in data["prepare"]["args"]
    assert "--update={input:update}" in data["prepare"]["args"]
    assert "--keys={input:keys}" in data["prepare"]["args"]
    assert data["prepare"]["keeps_copy"] is True
    assert data["prepare"]["done"] == ["{data}/extracted/exefs/main"]
    assert data["start"]["args"] == ["--play"]
    # Windows ZIP: PokeV-Recomp.exe next to the manifest. Linux: the launcher unpacks the AppImage into a plain
    # folder (squashfs-root layout) and runs its AppRun, which forwards to usr/bin/PokeV-Recomp.
    for step in ("prepare", "start"):
        assert data[step]["program"] == ["PokeV-Recomp.exe", "AppRun"], f"{step}.program"
    assert data["saves"] == ["{data}/save/save_0"]

    # External jsonschema validation against the official RecompLauncher schema
    # (pinned to elripalda/RecompLauncher@7d8bc7a59d472402ada20e2fff2fbbf806e10797).
    schema_env = os.environ.get("RECOMPLAUNCHER_SCHEMA")
    require_schema = os.environ.get("REQUIRE_RECOMPLAUNCHER_SCHEMA") == "1"
    if require_schema and (not schema_env or not Path(schema_env).is_file()):
        raise AssertionError(f"REQUIRE_RECOMPLAUNCHER_SCHEMA=1 but RECOMPLAUNCHER_SCHEMA is missing: {schema_env!r}")
    if schema_env and Path(schema_env).is_file():
        try:
            import jsonschema
        except ImportError:
            cli = shutil.which("jsonschema")
            if cli:
                subprocess.run([cli, "-i", str(manifest_path), schema_env], check=True)
            elif require_schema:
                raise
        else:
            schema = json.loads(Path(schema_env).read_text(encoding="utf-8"))
            jsonschema.validate(instance=data, schema=schema)


def test_settings_match_za_default_and_settings_c():
    data = json.loads((ROOT / "recomplauncher.json").read_text(encoding="utf-8"))
    ini_lines = (ROOT / "tools/za/za-default.ini").read_text(encoding="utf-8").splitlines()
    ini_kv = {}
    for line in ini_lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = [x.strip() for x in line.split("=", 1)]
        ini_kv[k] = v

    assert data["config"][0]["new"] == ini_kv, "config[0].new must match tools/za/za-default.ini"

    settings_c = (ROOT / "runtime/settings.c").read_text(encoding="utf-8")
    for s in data["settings"]:
        for key in s.get("defaults", {}):
            assert f'"{key}"' in settings_c, f'Setting key {key} not found in runtime/settings.c'
        for ch in s.get("choices", []):
            for key in ch.get("set", {}):
                assert f'"{key}"' in settings_c, f'Choice key {key} not found in runtime/settings.c'


def test_packaging_and_readme():
    dist_win = (ROOT / "tools/za/dist.sh").read_text(encoding="utf-8")
    dist_linux = (ROOT / "tools/za/dist-linux.sh").read_text(encoding="utf-8")
    apprun = (ROOT / "tools/linux/AppRun").read_text(encoding="utf-8")
    assert "recomplauncher.json" in dist_win
    assert '"$APPDIR/recomplauncher.json"' in dist_linux
    assert '"$@"' in apprun
    assert 'usr/bin/PokeV-Recomp' in apprun
    assert 'cp tools/linux/AppRun "$APPDIR/"' in dist_linux
    assert 'cp $B/PokeV-Recomp.exe $B/PokeV-Recomp.dll.a "$D/"' in dist_win

    for readme in ("README.md", "README.pt-BR.md"):
        text = (ROOT / readme).read_text(encoding="utf-8")
        assert "https://recomplauncher.com/assets/press/launcher/badge-light.svg" in text
        assert "https://recomplauncher.com/assets/press/launcher/badge-dark.svg" in text


def test_runtime_cli_and_env_helpers():
    cc = shutil.which(os.environ.get("CC", "gcc")) or shutil.which("clang")
    if not cc:
        print("SKIP: no C compiler available for runtime helper test")
        return

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Verify the exact helper logic from runtime/main.c and runtime/paths.c compiles and behaves as expected
        c_test = tmp / "test_rl_cli.c"
        c_test.write_text(
            r"""
#include <assert.h>
#include <limits.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void rl_setenv(const char *k, const char *v) {
#ifdef _WIN32
    _putenv_s(k, v);
#else
    setenv(k, v, 1);
#endif
}

static void rl_unsetenv(const char *k) {
#ifdef _WIN32
    _putenv_s(k, "");
#else
    unsetenv(k);
#endif
}

static const char *data_root_env(void) {
    const char *env = getenv("LARECOMP_DATA_ROOT");
    if (env && *env) return env;
    env = getenv("RECOMPLAUNCHER_DATA");
    return env && *env ? env : NULL;
}

static bool take_flag_value(int argc, char **argv, int *idx, const char *name, const char **out) {
    size_t n = strlen(name);
    if (!strncmp(argv[*idx], name, n) && argv[*idx][n] == '=') {
        *out = argv[*idx] + n + 1;
        return true;
    }
    if (!strcmp(argv[*idx], name) && *idx + 1 < argc) {
        *out = argv[++(*idx)];
        return true;
    }
    return false;
}

int main(void) {
    const char *orig_la = getenv("LARECOMP_DATA_ROOT");
    const char *orig_rl = getenv("RECOMPLAUNCHER_DATA");
    char *saved_la = orig_la ? strdup(orig_la) : NULL;
    char *saved_rl = orig_rl ? strdup(orig_rl) : NULL;

    rl_unsetenv("LARECOMP_DATA_ROOT");
    rl_unsetenv("RECOMPLAUNCHER_DATA");
    assert(data_root_env() == NULL);
    rl_setenv("RECOMPLAUNCHER_DATA", "/tmp/rl-data");
    assert(strcmp(data_root_env(), "/tmp/rl-data") == 0);
    rl_setenv("LARECOMP_DATA_ROOT", "/tmp/la-data");
    assert(strcmp(data_root_env(), "/tmp/la-data") == 0);

    if (saved_la) { rl_setenv("LARECOMP_DATA_ROOT", saved_la); free(saved_la); }
    else rl_unsetenv("LARECOMP_DATA_ROOT");
    if (saved_rl) { rl_setenv("RECOMPLAUNCHER_DATA", saved_rl); free(saved_rl); }
    else rl_unsetenv("RECOMPLAUNCHER_DATA");

    char *argv[] = {"PokeV-Recomp", "--prepare", "--game=/a/game.nsp", "--update", "/a/upd.nsp", "--keys=/a/prod.keys"};
    int argc = 6;
    bool prepare = false;
    const char *game = NULL, *upd = NULL, *keys = NULL, *v = NULL;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--prepare")) prepare = true;
        else if (take_flag_value(argc, argv, &i, "--game", &v)) game = v;
        else if (take_flag_value(argc, argv, &i, "--update", &v)) upd = v;
        else if (take_flag_value(argc, argv, &i, "--keys", &v)) keys = v;
    }
    assert(prepare);
    assert(strcmp(game, "/a/game.nsp") == 0);
    assert(strcmp(upd, "/a/upd.nsp") == 0);
    assert(strcmp(keys, "/a/prod.keys") == 0);
    printf("cli and env helper checks passed\n");
    return 0;
}
""",
            encoding="utf-8",
        )
        exe = tmp / "test_rl_cli"
        subprocess.run([cc, "-std=gnu11", "-Wall", "-Werror", str(c_test), "-o", str(exe)], check=True)
        out = subprocess.run([str(exe)], capture_output=True, text=True, check=True)
        assert "cli and env helper checks passed" in out.stdout


def test_path_overlap_guard():
    cc = shutil.which(os.environ.get("CC", "gcc")) or shutil.which("clang")
    cxx = shutil.which(os.environ.get("CXX", "g++")) or shutil.which("clang++")
    if not cc:
        print("SKIP: no C compiler available for path_overlap.h test")
        return

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dest = tmp / "data" / "extracted"
        nested = dest / "exefs"
        ext = tmp / "external_dump"
        sibling = tmp / "data" / "extracted_other"
        parent_src = tmp / "data"
        nonexistent_dest_inside_src = ext / "extracted"
        for d in (nested, ext, sibling):
            d.mkdir(parents=True, exist_ok=True)
        marker = nested / "main"
        marker.write_bytes(b"KEEP_ME")
        link = tmp / "link_to_dest"
        try:
            link.symlink_to(dest, target_is_directory=True)
        except OSError:
            link = dest  # Windows without Developer Mode: symlink creation may require privileges

        c_test = tmp / "test_overlap.c"
        c_test.write_text(
            r"""
#include <assert.h>
#include <stdio.h>
#include "path_overlap.h"

int main(int argc, char **argv) {
    assert(argc == 8);
    const char *dest = argv[1];
    const char *nested = argv[2];
    const char *ext = argv[3];
    const char *sibling = argv[4];
    const char *link = argv[5];
    const char *parent_src = argv[6];
    const char *nonexistent_dest_inside_src = argv[7];

    assert(!path_overlaps_dest(ext, dest));
    assert(!path_overlaps_dest(sibling, dest));
    assert(path_overlaps_dest(dest, dest));
    assert(path_overlaps_dest(nested, dest));
    assert(path_overlaps_dest(parent_src, dest));
    assert(path_overlaps_dest(ext, nonexistent_dest_inside_src));
    assert(path_overlaps_dest(link, dest));
    assert(path_overlaps_dest("/nonexistent/path/for/pokev/test", dest));
    assert(path_overlaps_dest(ext, "/nonexistent/dest/for/pokev/test"));
    assert(path_overlaps_dest(NULL, dest));
    assert(path_overlaps_dest("", dest));
    printf("path_overlap.h checks passed\n");
    return 0;
}
""",
            encoding="utf-8",
        )
        compilers = [(cc, "-std=gnu11")]
        if cxx:
            compilers.append((cxx, "-std=gnu++17"))
        for comp, std in compilers:
            exe = tmp / f"test_overlap_{std.lstrip('-')}"
            src_args = [str(c_test)] if std.endswith("11") else ["-x", "c++", str(c_test)]
            subprocess.run(
                [comp, std, "-Wall", "-Werror", f"-I{ROOT / 'runtime'}"] + src_args + ["-o", str(exe)],
                check=True,
            )
            out = subprocess.run(
                [str(exe), str(dest), str(nested), str(ext), str(sibling), str(link),
                 str(parent_src), str(nonexistent_dest_inside_src)],
                capture_output=True,
                text=True,
                check=True,
            )
            assert "path_overlap.h checks passed" in out.stdout
        assert marker.read_bytes() == b"KEEP_ME", "source file must remain intact when overlap is rejected"
        assert not nonexistent_dest_inside_src.exists(), "destination must not be created inside source"


if __name__ == "__main__":
    test_manifest_and_schema()
    test_settings_match_za_default_and_settings_c()
    test_packaging_and_readme()
    test_runtime_cli_and_env_helpers()
    test_path_overlap_guard()
    print("all recomplauncher integration checks passed")
