// Pokémon Scarlet & Violet's camera projection for windows that aren't 16:9 (runtime/hle/widescreen_proj.h, called
// from the patched projection builder: tools/sv_patches.py): no game files, no window.
// Build + run: tools/test_widescreen_proj.py (exits non-zero when a check fails).
// Each case goes window -> output_scale() (runtime/nvn/output_scale.c: the shape the render targets are reallocated
// at) -> widescreen_proj_aspect -> the game's builder (f_7000cf0ef4's perspective branches, written out from the
// disassembly) -> the matrix, checked against the reshaped 1920x1080 target it draws into:
//   - square pixels: P00 * target width == P11 * target height (a sphere stays round: no stretching);
//   - wider: P11 unchanged (the same vertical view as 16:9), tan of the horizontal half-view = window aspect x tan of
//     the vertical one (Hor+: 32:9 sees twice the 16:9 tangent, 21:9 2.37 / 1.78 of it);
//   - taller: P00 unchanged (the 16:9 horizontal view), more vertical view (Vert+);
//   - 16:9 windows and "Original 16:9": the game's own matrix, bit for bit; depth terms never change;
//   - cameras with another aspect (square / 4:3 render-to-texture views) and broken inputs: untouched.
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../runtime/settings.h"
#include "../runtime/nvn/output_scale.h"
#include "../runtime/hle/widescreen_proj.h"

Settings g_settings = {.res_mode = RES_MATCH};
bool g_gpu_integrated = false;
bool g_za_frame_hook = false;
bool am_handheld(void) { return false; }
void pace_hitch(const char *why) { (void)why; }
void pace_settings_changed(const char *what) { (void)what; }

static int g_fail, g_checks;
#define CHECK(cond, ...) do { g_checks++; if (!(cond)) { g_fail++; printf("FAIL %s:%d: ", __FILE__, __LINE__); \
    printf(__VA_ARGS__); printf("\n"); } } while (0)

// The game's camera fields the builder reads (offsets from the camera, x19).
typedef struct {
    float near_, far_, fovy, aspect;  // +0x184, +0x188, +0x18c, +0x190
} Cam;

// f_7000cf0ef4, perspective ([+0x180] == 0): column-major P (m[col][row]) as stored at [+0x200]. `patched`:
// half_fovy and aspect go through widescreen_proj_aspect with the output's `wide`, as the generated code does.
static void game_builder(const Cam *c, int patched, float wide, float m[4][4]) {
    float h = c->fovy * 0.5f;              // ldr s0, [x19, #0x18c]; fmov s1, #0.5; fmul s0, s0, s1
    float aspect = c->aspect;              // ldr s16 / s4, [x19, #0x190]
    if (patched) {
        (void)widescreen_proj_aspect(aspect, wide, &h);
        aspect = widescreen_proj_aspect(aspect, wide, NULL);
    }
    float n = c->near_, f = c->far_;
    float cot = 1.0f / tanf(h);            // inlined sincos -> cos / sin
    float d = f - n;                       // fsub s6, s17, s5
    memset(m, 0, 16 * sizeof(float));
    m[0][0] = cot / aspect;                // fdiv s4, s1, s16 / fdiv s3, s1, s4
    m[1][1] = cot;
    m[2][2] = n / d, m[2][3] = -1.0f;
    m[3][2] = n * f / d;
}

static int bits_equal(float a[4][4], float b[4][4]) { return memcmp(a, b, 16 * sizeof(float)) == 0; }
static float rel(float a, float b) { return fabsf(a - b) / fmaxf(fabsf(b), 1e-30f); }

static const float k169 = 16.0f / 9.0f;

// One window: the output shape as the renderer works it out, then the main camera's matrix in it.
static void window_case(const char *name, double w, double h, int aspect_setting, float fovy_deg) {
    g_settings.aspect = aspect_setting;
    OutScale os;
    output_scale(w, h, aspect_setting == ASPECT_UNLOCKED, 1.0, -1, -1, &os);
    float wide = (float)os.shape;          // device.c: widescreen_set_factor(shape)
    // the 1920x1080 scene target as reallocated (textures.c: width x sx, height x sy)
    double tw = 1920.0 * os.sx, th = 1080.0 * os.sy;
    Cam cam = {0.1f, 6000.0f, fovy_deg * (float)M_PI / 180.0f, k169};
    float orig[4][4], m[4][4];
    game_builder(&cam, 0, 1.0f, orig);
    game_builder(&cam, 1, wide, m);
    double win_aspect = w / h, target_aspect = tw / th;
    int reshaped = fabs(os.shape - 1.0) > 1e-9;
    printf("%-26s %5.0fx%-5.0f fovy %3.0f  shape %.4f  target %.0fx%.0f  P00 %.5f -> %.5f  P11 %.5f -> %.5f\n", name, w, h,
           fovy_deg, os.shape, tw, th, orig[0][0], m[0][0], orig[1][1], m[1][1]);

    // depth rows and the w row are the game's whatever the shape
    CHECK(m[2][2] == orig[2][2] && m[2][3] == orig[2][3] && m[3][2] == orig[3][2] && m[3][3] == orig[3][3],
          "%s: depth terms changed", name);
    if (!reshaped) {
        CHECK(bits_equal(m, orig), "%s: not the game's own matrix (no reshape: 16:9 window or Original 16:9)", name);
        CHECK(rel((float)(m[0][0] * tw), (float)(m[1][1] * th)) < 1e-5f, "%s: 16:9 box not square", name);
        return;
    }
    CHECK(rel((float)target_aspect, (float)win_aspect) < 2e-3f, "%s: target %.4f vs window %.4f", name, target_aspect, win_aspect);
    // square pixels in the reshaped target: no stretching either way
    CHECK(rel((float)(m[0][0] * tw), (float)(m[1][1] * th)) < 1e-5f, "%s: pixels not square: P00 W %.4f, P11 H %.4f", name,
          m[0][0] * tw, m[1][1] * th);
    float tan_h0 = 1 / orig[0][0], tan_v0 = 1 / orig[1][1], tan_h = 1 / m[0][0], tan_v = 1 / m[1][1];
    if (wide > 1) {  // Hor+
        CHECK(m[1][1] == orig[1][1], "%s: vertical view changed (P11 %.6f vs %.6f)", name, m[1][1], orig[1][1]);
        CHECK(rel(tan_h, (float)win_aspect * tan_v0) < 1e-5f, "%s: horizontal half-view tan %.5f, want %.5f", name, tan_h,
              win_aspect * tan_v0);
        CHECK(tan_h > tan_h0, "%s: no wider than 16:9", name);
    } else {  // Vert+
        CHECK(rel(m[0][0], orig[0][0]) < 1e-5f, "%s: horizontal view changed (P00 %.6f vs %.6f)", name, m[0][0], orig[0][0]);
        CHECK(tan_v > tan_v0, "%s: no taller than 16:9", name);
        CHECK(rel(tan_v, tan_h0 / (float)win_aspect) < 1e-5f, "%s: vertical half-view tan %.5f, want %.5f", name, tan_v,
              tan_h0 / win_aspect);
    }
}

int main(void) {
    static const struct { const char *name; double w, h; } wins[] = {
        {"16:9 1080p", 1920, 1080}, {"16:9 1440p", 2560, 1440}, {"16:9 4K", 3840, 2160}, {"21:9 3440x1440", 3440, 1440},
        {"21:9 2560x1080", 2560, 1080}, {"32:9 5120x1440", 5120, 1440}, {"32:9 3840x1080", 3840, 1080},
        {"24:10 3840x1600", 3840, 1600}, {"16:10 Steam Deck", 1280, 800}, {"16:10 1920x1200", 1920, 1200},
        {"4:3 1600x1200", 1600, 1200}, {"near 16:9 1366x768", 1366, 768}, {"portrait 1080x1920", 1080, 1920},
    };
    static const float fovs[] = {30, 45, 60, 75};
    static const int modes[] = {RES_MATCH, 2, RES_Q50};
    for (size_t mi = 0; mi < sizeof modes / sizeof *modes; mi++) {
        g_settings.res_mode = modes[mi];
        printf("-- resolution mode %d\n", modes[mi]);
        for (size_t i = 0; i < sizeof wins / sizeof *wins; i++)
            for (size_t f = 0; f < sizeof fovs / sizeof *fovs; f++) {
                window_case(wins[i].name, wins[i].w, wins[i].h, ASPECT_UNLOCKED, fovs[f]);
                window_case(wins[i].name, wins[i].w, wins[i].h, ASPECT_ORIGINAL, fovs[f]);
            }
    }

    // 32:9 (5120x1440) and 21:9 (3440x1440) explicit Hor+ checks
    {
        float h = 25.0f * (float)M_PI / 180.0f, h0 = h;
        float a32 = widescreen_proj_aspect(k169, 2.0f, &h);
        CHECK(fabsf(a32 - 32.0f / 9.0f) < 1e-6f && h == h0, "32:9: aspect %.6f (want 3.555556), fovy/2 %.6f", a32, h);
        float hfov169 = 2 * atanf(k169 * tanf(h0)) * 180 / (float)M_PI;
        float hfov329 = 2 * atanf(a32 * tanf(h)) * 180 / (float)M_PI;
        CHECK(hfov329 > hfov169 && fabsf(tanf(hfov329 * (float)M_PI / 360) / tanf(hfov169 * (float)M_PI / 360) - 2) < 1e-5f,
              "32:9 horizontal tangent not doubled");

        float wide21 = (3440.0f / 1440.0f) / k169;
        float a21 = widescreen_proj_aspect(k169, wide21, &h);
        CHECK(fabsf(a21 - 3440.0f / 1440.0f) < 1e-5f && h == h0, "21:9: aspect %.6f, fovy/2 %.6f", a21, h);
    }

    // cameras that aren't the 16:9 view, and broken inputs: untouched
    {
        static const float aspects[] = {1.0f, 4.0f / 3.0f, 0.5625f, 2.0f, 1.76f, 1.80f, 0, -k169, NAN, INFINITY};
        for (size_t i = 0; i < sizeof aspects / sizeof *aspects; i++) {
            float h = 0.4f, a = widescreen_proj_aspect(aspects[i], 2.0f, &h);
            CHECK((isnan(aspects[i]) ? isnan(a) : a == aspects[i]) && h == 0.4f, "aspect %f changed to %f", aspects[i], a);
            h = 0.4f, a = widescreen_proj_aspect(aspects[i], 0.9375f, &h);
            CHECK((isnan(aspects[i]) ? isnan(a) : a == aspects[i]) && h == 0.4f, "aspect %f (taller) changed", aspects[i]);
        }
        static const float wides[] = {0, -1, 1, NAN, INFINITY, 1e-9f, 1e9f};
        for (size_t i = 0; i < sizeof wides / sizeof *wides; i++) {
            float h = 0.4f, a = widescreen_proj_aspect(k169, wides[i], &h);
            CHECK(a == k169 && h == 0.4f, "wide %f: aspect %f / fovy/2 %f changed", wides[i], a, h);
        }
        static const float halves[] = {0, -0.3f, 1.5708f, 3.0f, NAN};
        for (size_t i = 0; i < sizeof halves / sizeof *halves; i++) {
            float h = halves[i], a = widescreen_proj_aspect(k169, 0.9375f, &h);
            CHECK(a == k169 && (isnan(halves[i]) ? isnan(h) : h == halves[i]), "fovy/2 %f (taller): changed", halves[i]);
        }
        // NULL half_fovy is safe for both wider and taller outputs
        CHECK(fabsf(widescreen_proj_aspect(k169, 2.0f, NULL) - 32.0f / 9.0f) < 1e-6f, "NULL half_fovy failed for 32:9");
        CHECK(fabsf(widescreen_proj_aspect(k169, 0.75f, NULL) - (k169 * 0.75f)) < 1e-6f, "NULL half_fovy failed for 4:3");
    }

    printf("%d of %d checks passed\n", g_checks - g_fail, g_checks);
    return g_fail != 0;
}
