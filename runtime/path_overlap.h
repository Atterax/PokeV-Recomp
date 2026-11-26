// Safe source-vs-destination overlap guard for game file preparation (C and C++: launcher.mm, tests).
#pragma once
#include <limits.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "platform.h"

#ifdef _WIN32
#define overlap_prefix(a, b, n) (_strnicmp((a), (b), (n)) == 0)
#define OVERLAP_SEP '\\'
#else
#define overlap_prefix(a, b, n) (strncmp((a), (b), (n)) == 0)
#define OVERLAP_SEP '/'
#endif

static inline bool overlap_within(const char *child, const char *parent) {
    size_t np = strlen(parent);
    while (np > 1 && (parent[np - 1] == '/' || parent[np - 1] == '\\')) {
#ifdef _WIN32
        if (np == 3 && parent[1] == ':') break;
#endif
        np--;
    }
    if (np == 0) return false;
    if (parent[np - 1] == '/' || parent[np - 1] == '\\') return overlap_prefix(child, parent, np);
    return overlap_prefix(child, parent, np) && (child[np] == '\0' || child[np] == '/' || child[np] == '\\');
}

// Resolve dest even when its final component does not exist yet (without creating any directory).
static inline bool overlap_resolve_dest(const char *dest, char *out, size_t n) {
    if (realpath(dest, out)) return true;
    const char *s1 = strrchr(dest, '/'), *s2 = strrchr(dest, '\\');
    const char *sep = s2 > s1 ? s2 : s1;
    char parent_buf[4096], resolved_parent[4096];
    const char *parent = ".", *base = dest;
    if (sep) {
        size_t plen = (size_t)(sep - dest);
        if (plen == 0) parent = "/";
        else if (plen >= sizeof parent_buf) return false;
        else {
            memcpy(parent_buf, dest, plen);
            parent_buf[plen] = '\0';
            parent = parent_buf;
        }
        base = sep + 1;
    }
    if (!*base || !strcmp(base, ".") || !strcmp(base, "..")) return false;
    if (!realpath(parent, resolved_parent)) return false;
    size_t rp = strlen(resolved_parent);
    bool has_sep = rp > 0 && (resolved_parent[rp - 1] == '/' || resolved_parent[rp - 1] == '\\');
    int written = has_sep ? snprintf(out, n, "%s%s", resolved_parent, base)
                          : snprintf(out, n, "%s%c%s", resolved_parent, OVERLAP_SEP, base);
    return written > 0 && (size_t)written < n;
}

// True if src and dest overlap in either direction (equal, src inside dest, dest inside src), or cannot be resolved.
static inline bool path_overlaps_dest(const char *src, const char *dest) {
    if (!src || !*src || !dest || !*dest) return true;
#ifdef PATH_MAX
    char a[PATH_MAX], b[PATH_MAX];
#else
    char a[4096], b[4096];
#endif
    if (!realpath(src, a) || !overlap_resolve_dest(dest, b, sizeof b)) return true;
    return overlap_within(a, b) || overlap_within(b, a);
}
