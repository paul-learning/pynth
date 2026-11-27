/* Pynth DSP-Kern in C: die Schleifen pro Sample, die in numpy nicht oder nur
 * teuer gehen. Wird beim Start von pynth/fast.py einmal mit `cc` uebersetzt
 * und per ctypes geladen. Fehlt der Compiler, rechnet die Engine wie bisher
 * mit numpy/scipy (gleicher Klang, nur langsamer).
 *
 * Alles double, alle Arrays zusammenhaengend (C-Order).
 */
#include <math.h>
#include <stddef.h>

#define EXP_K 6.9          /* muss zu engine.EXP_K passen */
#define TWO_PI 6.283185307179586

/* Wellenform-IDs, muessen zu fast.WAVE_ID passen */
enum { W_SINE = 0, W_SAW = 1, W_SQUARE = 2, W_TRI = 3 };

static inline double polyblep(double t, double dt)
{
    if (t < dt) {
        double x = t / dt;
        return x + x - x * x - 1.0;
    }
    if (t > 1.0 - dt) {
        double x = (t - 1.0) / dt;
        return x * x + x + x + 1.0;
    }
    return 0.0;
}

static inline double osc(int w, double t, double dt)
{
    switch (w) {
    case W_SINE:
        return sin(TWO_PI * t);
    case W_SAW:
        return 2.0 * t - 1.0 - polyblep(t, dt);
    case W_SQUARE: {
        double t2 = t + 0.5;
        if (t2 >= 1.0) t2 -= 1.0;
        return (t < 0.5 ? 1.0 : -1.0) + polyblep(t, dt) - polyblep(t2, dt);
    }
    case W_TRI:
        return 4.0 * fabs(t - 0.5) - 1.0;
    }
    return 0.0;
}

/* Parameter pro Stimme (NVP doubles):
 *  0 wave1  1 inc1  2 level1  3 wave2  4 inc2  5 level2
 *  6 released  7 t (bzw. t_rel wenn released)  8 start_level
 *  9 attack[samples]  10 decay[samples]  11 sustain  12 rel_level
 *  13 release[samples]  14 velocity
 * phases: 2 pro Stimme (Osc 1, Osc 2), werden fortgeschrieben.
 * pmod: Tonhoehen-Faktor pro Sample oder NULL.
 * mix/envsum werden aufaddiert (vorher nullen).
 */
#define NVP 16

void render_voices(int nv, int n, const double *vp, double *phases,
                   const double *pmod, double *mix, double *envsum)
{
    for (int v = 0; v < nv; v++) {
        const double *q = vp + v * NVP;
        int w1 = (int)q[0], w2 = (int)q[3];
        double inc1 = q[1], l1 = q[2], inc2 = q[4], l2 = q[5];
        int released = q[6] != 0.0;
        double t = q[7], start = q[8], a = q[9], d = q[10], s = q[11];
        double rel = q[12], r = q[13], vel = q[14];
        double ph1 = phases[2 * v], ph2 = phases[2 * v + 1];

        /* Huellkurve rekursiv statt exp() pro Sample */
        double e = 0.0, mul = 1.0, dec = 0.0, dmul = exp(-EXP_K / d);
        if (released) {
            e = rel * exp(-EXP_K * t / r);
            mul = exp(-EXP_K / r);
        } else if (t >= a) {
            dec = exp(-EXP_K * (t - a) / d);
        }

        for (int i = 0; i < n; i++) {
            double env;
            if (released) {
                env = e;
                e *= mul;
            } else {
                double tt = t + i;
                if (tt < a) {
                    env = start + (1.0 - start) * (tt / a);
                } else {
                    if (tt - a < 1.0) dec = exp(-EXP_K * (tt - a) / d);  /* Uebergang Attack->Decay */
                    env = s + (1.0 - s) * dec;
                    dec *= dmul;
                }
            }
            env *= vel;

            double pm = pmod ? pmod[i] : 1.0;
            double dt1 = inc1 * pm, dt2 = inc2 * pm;
            ph1 += dt1;
            ph1 -= floor(ph1);
            ph2 += dt2;
            ph2 -= floor(ph2);
            double o = 0.0;
            if (l1 > 0.0) o += l1 * osc(w1, ph1, dt1);
            if (l2 > 0.0) o += l2 * osc(w2, ph2, dt2);
            mix[i] += o * env;
            envsum[i] += env;
        }
        phases[2 * v] = ph1;
        phases[2 * v + 1] = ph2;
    }
}

/* Tiefpass als State-Variable-Filter (TPT, Zavalishin/Simper).
 * Gleicher Frequenzgang wie der RBJ-Biquad, aber stabil und knackfrei bei
 * Cutoff-Aenderungen pro Sample (LFO aufs Filter).
 * cutoff: Hz pro Sample (schon begrenzt), k = 1/Q, state: 2 doubles. */
void svf_lowpass(int n, const double *x, double *y, const double *cutoff,
                 double k, double sr, double *state)
{
    double ic1 = state[0], ic2 = state[1];
    double pi_sr = M_PI / sr;
    for (int i = 0; i < n; i++) {
        double g = tan(cutoff[i] * pi_sr);
        double a1 = 1.0 / (1.0 + g * (g + k));
        double a2 = g * a1, a3 = g * a2;
        double v3 = x[i] - ic2;
        double v1 = a1 * ic1 + a2 * v3;
        double v2 = ic2 + a2 * ic1 + a3 * v3;
        ic1 = 2.0 * v1 - ic1;
        ic2 = 2.0 * v2 - ic2;
        y[i] = v2;
    }
    /* Denormals/NaN abfangen, damit ein Ausreisser nicht ewig haengt */
    if (!isfinite(ic1) || !isfinite(ic2)) ic1 = ic2 = 0.0;
    state[0] = ic1;
    state[1] = ic2;
}

/* Ringpuffer v[n] = x[n] + g * v[n-d] (Delay, Comb, Allpass).
 * Schreibt v und das um d verzoegerte v. w = Schreibposition (in/out). */
void feedback_line(int n, const double *x, double *v, double *vd,
                   double *buf, int size, int *w, int d, double g)
{
    int wp = *w;
    int rp = wp - d;
    if (rp < 0) rp += size;
    for (int i = 0; i < n; i++) {
        double past = buf[rp];
        double cur = x[i] + g * past;
        buf[wp] = cur;
        v[i] = cur;
        vd[i] = past;
        if (++wp == size) wp = 0;
        if (++rp == size) rp = 0;
    }
    *w = wp;
}

/* 4-Pol-Ladder-Tiefpass (24 dB/Okt.) im Stil des Moog-Filters, als
 * Zero-Delay-Feedback-Struktur aus vier TPT-One-Poles (Zavalishin).
 * Saettigung: tanh() am Eingang der Kette (nach der Rueckkopplung). Dadurch
 * bleibt die Resonanz begrenzt: bei k > 4 schwingt das Filter sanft selbst,
 * statt zu explodieren.
 *  cutoff: Hz pro Sample (schon begrenzt), k: Resonanz 0..~4.2
 *  drive: Vorverstaerkung in die Saettigung, makeup: Ausgangspegel
 *  comp: Bass-Ausgleich (Ladder verliert mit Resonanz Pegel)
 *  state: 4 doubles */
void ladder_lowpass(int n, const double *x, double *y, const double *cutoff,
                    double k, double drive, double makeup, double comp,
                    double sr, double *state)
{
    double s1 = state[0], s2 = state[1], s3 = state[2], s4 = state[3];
    double pi_sr = M_PI / sr;
    for (int i = 0; i < n; i++) {
        double g = tan(cutoff[i] * pi_sr);
        double G = g / (1.0 + g);
        double inv = 1.0 / (1.0 + g);
        /* Ausgang der Kette ohne Eingang (nur Zustaende) */
        double S = (G * G * G * s1 + G * G * s2 + G * s3 + s4) * inv;
        double G4 = G * G * G * G;
        double u = (drive * comp * x[i] - k * S) / (1.0 + k * G4);
        u = tanh(u);
        double v, y1, y2, y3, y4;
        v = (u - s1) * G;  y1 = v + s1; s1 = y1 + v;
        v = (y1 - s2) * G; y2 = v + s2; s2 = y2 + v;
        v = (y2 - s3) * G; y3 = v + s3; s3 = y3 + v;
        v = (y3 - s4) * G; y4 = v + s4; s4 = y4 + v;
        y[i] = y4 * makeup;
    }
    if (!isfinite(s1) || !isfinite(s2) || !isfinite(s3) || !isfinite(s4))
        s1 = s2 = s3 = s4 = 0.0;
    state[0] = s1; state[1] = s2; state[2] = s3; state[3] = s4;
}
