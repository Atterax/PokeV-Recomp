"""Compile/run the production cache logic with synthetic inputs."""
import os
from pathlib import Path
import platform
import shlex
import subprocess
import tempfile

root=Path(__file__).resolve().parent.parent


def sdl3_flags():
    arch = 'aarch64' if platform.machine() in ('arm64', 'aarch64') else 'x86_64'
    candidates = [
        root / f'build/linux/{arch}/deps',
        root / 'build/deps-linux',
        root / 'build/deps',
        root / 'build/deps-win',
        Path('/opt/homebrew/opt/sdl3'),
        Path('/usr/local/opt/sdl3'),
    ]
    cflags, ldflags = [], []
    for prefix in candidates:
        if (prefix / 'include/SDL3').is_dir():
            cflags.append(f'-I{prefix / "include"}')
            lib = prefix / 'lib'
            if lib.is_dir() and any(lib.glob('*SDL3*')):
                ldflags.append(f'-L{lib}')
                if os.name != 'nt':
                    ldflags.append(f'-Wl,-rpath,{lib}')
            break
    ldflags.append('-lSDL3')
    return cflags, ldflags


with tempfile.TemporaryDirectory() as tmp:
    tmp=Path(tmp).absolute()
    exe=tmp/('cache-test.exe' if os.name=='nt' else 'cache-test')
    cxx=shlex.split(os.environ.get('CXX','clang++'))
    sdl_cflags, sdl_ldflags = sdl3_flags()
    subprocess.run(cxx+['-x','c++','-std=c++17','-pthread','-Iruntime']+sdl_cflags+[
                        'runtime/build_cache.cpp','tools/test_build_cache.cpp',
                        '-o',str(exe)]+sdl_ldflags,cwd=root,check=True)
    data = tmp/'data'
    if os.name == 'nt':
        target, linked = tmp/'target', tmp/'junction'
        target.mkdir()
        assert target.resolve().is_relative_to(tmp.resolve())
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(linked).replace('/', '\\'), str(target).replace('/', '\\')],
                       check=True, stdout=subprocess.DEVNULL)
        data = linked/'data'
    subprocess.run([str(exe),str(data)],check=True)
    if os.name == 'nt':
        # Exercise the production path check with Windows metadata, without requiring a OneDrive account.
        production = (root/'runtime/build_cache.cpp').read_text(encoding='utf-8')
        check = production[production.index('bool is_link('):production.index('void plain(')]
        harness = r'''
#include <windows.h>
#include <filesystem>
#include <cassert>
namespace fs = std::filesystem;
static DWORD attributes, tag;
static bool missing;
static int closes;
static HANDLE find_file(const wchar_t *, WIN32_FIND_DATAW *data) {
    if (missing) return INVALID_HANDLE_VALUE;
    data->dwFileAttributes = attributes; data->dwReserved0 = tag;
    return (HANDLE)1;
}
static BOOL close_find(HANDLE) { ++closes; return TRUE; }
#define FindFirstFileW find_file
#define FindClose close_find
''' + check + r'''
int main() {
    attributes = FILE_ATTRIBUTE_REPARSE_POINT;
    for (DWORD value : {IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT}) {
        tag = value; assert(is_link(L"cache-entry"));
    }
    for (DWORD value : {IO_REPARSE_TAG_CLOUD, IO_REPARSE_TAG_CLOUD_1, IO_REPARSE_TAG_CLOUD_F}) {
        tag = value; assert(!is_link(L"cache-entry"));
    }
    attributes = FILE_ATTRIBUTE_NORMAL; tag = IO_REPARSE_TAG_SYMLINK;
    assert(!is_link(L"ordinary-file"));
    assert(closes == 6);
    missing = true; assert(!is_link(L"not-created-yet")); assert(closes == 6);
}
'''
        source, check_exe = tmp/'reparse.cpp', tmp/'reparse.exe'
        source.write_text(harness)
        subprocess.run(cxx+['-std=c++17', str(source), '-o', str(check_exe)], check=True)
        subprocess.run([str(check_exe)], check=True)
        print('Windows reparse tags: reject symlinks/junctions, allow cloud placeholders and ordinary files')
