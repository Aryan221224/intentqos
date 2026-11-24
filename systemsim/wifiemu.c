/*
 * wifiemu: layer-2 home Wi-Fi emulator for the IntentQoS full-system emulation (EMULATION).
 *
 * The router VM's two radio ports (5 GHz, 2.4 GHz) are virtual Ethernet ports that OpenWrt bridges into br-lan,
 * as it bridges phy1-ap0/phy0-ap0 on a GL-MT3000. This program is the air between those ports and the stations:
 * it owns one "AP" TAP per band (on the band's host bridge, next to the router's radio port) and one TAP per
 * station (moved into the station's namespace as its eth0, with the station's MAC). Frames keep their MACs, so
 * DHCP, ARP and the router's per-device view are exactly as on real Wi-Fi.
 *
 * Model (same parameters and formulas as testbed/accessemu.c, from the field profile):
 *   access delay   lognormal (5 GHz median 1 ms, 2.4 GHz 2 ms one way), growing above an airtime-utilisation
 *                  knee (own band load + neighbour busy time);
 *   airtime        one half-duplex medium per band at its usable rate (5 GHz 400, 2.4 GHz 100 Mbit/s) times
 *                  (1 - neighbour busy); every frame occupies it for its transmit time; a frame that would wait
 *                  more than 60 ms for the medium is dropped (mac80211's per-station FQ-CoDel keeps Wi-Fi queues
 *                  short; this is a drop-tail stand-in);
 *   stall episodes Poisson in time, Pareto length (10-200 ms); background scans (2-4 dwells of 20-40 ms,
 *                  about every 3 min); power-save wake delay for phones idle > 200 ms (up to one beacon);
 *   loss           residual per-band loss; per-station extra delay/loss can be set at run time ("degrade").
 *   Frames are never reordered within a station and direction.
 *
 *   wifiemu <config>      config lines: key=value (re-read when the file changes)
 *     ap5=<tap>  ap24=<tap>
 *     sta=<idx>,<tap>,<band 5|24>,<mac>,<neighbour busy>,<psm 0|1>[,<extra ms>,<loss>]
 *     wifi5.med_ms ... (same keys as accessemu)
 *   Stats every second to <config>.stats.jsonl; every stall/scan to <config>.events.jsonl.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/if.h>
#include <linux/if_tun.h>
#include <math.h>
#include <poll.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define NSTA 16
#define MAXF 2048

static uint64_t rs[4];
static uint64_t rotl(uint64_t x, int k) { return (x << k) | (x >> (64 - k)); }
static uint64_t rnext(void) {
    uint64_t r = rotl(rs[1] * 5, 7) * 9, t = rs[1] << 17;
    rs[2] ^= rs[0]; rs[3] ^= rs[1]; rs[1] ^= rs[2]; rs[0] ^= rs[3]; rs[2] ^= t; rs[3] = rotl(rs[3], 45);
    return r;
}
static double runif(void) { return (rnext() >> 11) * (1.0 / 9007199254740992.0) + 1e-18; }
static void seed4(uint64_t *q, uint64_t s) {
    for (int i = 0; i < 4; i++) { s += 0x9E3779B97F4A7C15ull; uint64_t z = s;
        z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull; z = (z ^ (z >> 27)) * 0x94D049BB133111EBull; q[i] = z ^ (z >> 31); }
}
static double sunif(uint64_t *q) {
    uint64_t r = rotl(q[1] * 5, 7) * 9, t = q[1] << 17;
    q[2] ^= q[0]; q[3] ^= q[1]; q[1] ^= q[2]; q[0] ^= q[3]; q[2] ^= t; q[3] = rotl(q[3], 45);
    return (r >> 11) * (1.0 / 9007199254740992.0) + 1e-18;
}
static double rnorm(void) { return sqrt(-2.0 * log(runif())) * cos(2 * M_PI * runif()); }
static double now_ms(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec * 1e3 + t.tv_nsec / 1e6; }
static double wall(void) { struct timespec t; clock_gettime(CLOCK_REALTIME, &t); return t.tv_sec + t.tv_nsec / 1e9; }

static struct {
    double med[2], sigma[2], cap[2], stall_rate[2], loss[2];
    double knee, tail_xm, tail_alpha, tail_cap, scan_mean_s, abs_min, abs_max, psm_timeout, beacon, max_wait;
    int dwell_min, dwell_max;
    char ap[2][32];
} C;

typedef struct {
    int used, band, psm, fd; char tap[32]; unsigned char mac[6]; double nbusy, extra_ms, loss;
    double last[2], last_seen; uint64_t q[4];
    double scan_next, dw_t, abs_s, abs_e; int dw_left; double st_s, st_e;
    uint64_t tx[2], drop[2]; double dsum[2]; uint64_t dn[2];
} sta_t;
static sta_t S[NSTA];
static int apfd[2] = {-1, -1};
static double busy_until[2], band_bytes[2], band_load[2], band_t0;
static FILE *evlog, *stlog;

static void defaults(void) {
    C.med[0] = 1.0; C.sigma[0] = 0.8; C.cap[0] = 400; C.stall_rate[0] = 1.0 / 30; C.loss[0] = 0.0005;
    C.med[1] = 2.0; C.sigma[1] = 1.0; C.cap[1] = 100; C.stall_rate[1] = 1.0 / 10; C.loss[1] = 0.002;
    C.knee = 0.5; C.tail_xm = 10; C.tail_alpha = 1.5; C.tail_cap = 200; C.scan_mean_s = 180;
    C.dwell_min = 2; C.dwell_max = 4; C.abs_min = 20; C.abs_max = 40; C.psm_timeout = 200; C.beacon = 102.4;
    C.max_wait = 60;
}

static int open_tap(const char *name) {
    int fd = open("/dev/net/tun", O_RDWR | O_NONBLOCK);
    if (fd < 0) { perror("tun"); exit(1); }
    struct ifreq ifr; memset(&ifr, 0, sizeof ifr);
    ifr.ifr_flags = IFF_TAP | IFF_NO_PI;
    strncpy(ifr.ifr_name, name, IFNAMSIZ - 1);
    if (ioctl(fd, TUNSETIFF, &ifr) < 0) { perror(name); exit(1); }
    return fd;
}

static void load(const char *path, int first) {
    FILE *f = fopen(path, "r");
    if (!f) return;
    char line[512];
    while (fgets(line, sizeof line, f)) {
        char *eq = strchr(line, '='); if (!eq || line[0] == '#') continue;
        *eq = 0; char *k = line, *v = eq + 1; v[strcspn(v, "\r\n")] = 0; double x = atof(v);
        if (!strcmp(k, "ap5")) snprintf(C.ap[0], 32, "%s", v);
        else if (!strcmp(k, "ap24")) snprintf(C.ap[1], 32, "%s", v);
        else if (!strcmp(k, "wifi5.med_ms")) C.med[0] = x; else if (!strcmp(k, "wifi5.sigma")) C.sigma[0] = x;
        else if (!strcmp(k, "wifi5.cap_mbps")) C.cap[0] = x; else if (!strcmp(k, "wifi5.stall_per_s")) C.stall_rate[0] = x;
        else if (!strcmp(k, "wifi5.loss")) C.loss[0] = x;
        else if (!strcmp(k, "wifi24.med_ms")) C.med[1] = x; else if (!strcmp(k, "wifi24.sigma")) C.sigma[1] = x;
        else if (!strcmp(k, "wifi24.cap_mbps")) C.cap[1] = x; else if (!strcmp(k, "wifi24.stall_per_s")) C.stall_rate[1] = x;
        else if (!strcmp(k, "wifi24.loss")) C.loss[1] = x;
        else if (!strcmp(k, "wifi.knee")) C.knee = x; else if (!strcmp(k, "wifi.tail_xm_ms")) C.tail_xm = x;
        else if (!strcmp(k, "wifi.tail_alpha")) C.tail_alpha = x; else if (!strcmp(k, "wifi.tail_cap_ms")) C.tail_cap = x;
        else if (!strcmp(k, "wifi.scan_mean_s")) C.scan_mean_s = x;
        else if (!strcmp(k, "wifi.dwell_min")) C.dwell_min = (int)x; else if (!strcmp(k, "wifi.dwell_max")) C.dwell_max = (int)x;
        else if (!strcmp(k, "wifi.absence_min_ms")) C.abs_min = x; else if (!strcmp(k, "wifi.absence_max_ms")) C.abs_max = x;
        else if (!strcmp(k, "wifi.psm_timeout_ms")) C.psm_timeout = x; else if (!strcmp(k, "wifi.beacon_ms")) C.beacon = x;
        else if (!strcmp(k, "sta")) {
            int i, band, psm = 0; char tap[32]; unsigned m[6]; double nb = 0, ex = 0, lo = -1;
            int n = sscanf(v, "%d,%31[^,],%d,%x:%x:%x:%x:%x:%x,%lf,%d,%lf,%lf", &i, tap, &band,
                           &m[0], &m[1], &m[2], &m[3], &m[4], &m[5], &nb, &psm, &ex, &lo);
            if (n >= 9 && i >= 0 && i < NSTA) {
                sta_t *s = &S[i];
                if (first) {
                    s->used = 1; snprintf(s->tap, 32, "%s", tap); s->band = band == 24 ? 1 : 0;
                    for (int j = 0; j < 6; j++) s->mac[j] = (unsigned char)m[j];
                }
                s->nbusy = nb; s->psm = psm; s->extra_ms = ex; s->loss = lo;
            }
        }
    }
    fclose(f);
}

static void sta_reset(int i, double t) {
    sta_t *s = &S[i];
    seed4(s->q, 0x5EED0000ull + (uint64_t)i * 1442695040888963407ull);
    s->last_seen = t; s->scan_next = t - C.scan_mean_s * 1000.0 * log(sunif(s->q));
    s->st_s = s->st_e = t; s->abs_s = s->abs_e = 0;
}

static double events_hold(int i, double t) {
    sta_t *s = &S[i]; int b = s->band;
    for (;;) {
        if (s->dw_left > 0 && s->dw_t <= t) {
            double a = C.abs_min + sunif(s->q) * (C.abs_max - C.abs_min);
            s->abs_s = s->dw_t; s->abs_e = s->dw_t + a; s->dw_t = s->abs_e + 20.0; s->dw_left--;
            fprintf(evlog, "{\"t\":%.3f,\"station\":%d,\"event\":\"scan_dwell\",\"ms\":%.1f}\n", wall() - (t - s->abs_s) / 1000.0, i, a);
            continue;
        }
        if (s->dw_left == 0 && s->scan_next <= t) {
            s->dw_left = C.dwell_min + (int)(sunif(s->q) * (C.dwell_max - C.dwell_min + 1));
            s->dw_t = s->scan_next; s->scan_next -= C.scan_mean_s * 1000.0 * log(sunif(s->q));
            continue;
        }
        break;
    }
    double rate = C.stall_rate[b] * (1.0 + 2.0 * s->nbusy);
    while (rate > 0 && s->st_e <= t) {
        double gap = -1000.0 / rate * log(sunif(s->q));
        double p = C.tail_xm / pow(sunif(s->q), 1.0 / C.tail_alpha);
        s->st_s = s->st_e + gap; s->st_e = s->st_s + (p > C.tail_cap ? C.tail_cap : p);
        fprintf(evlog, "{\"t\":%.3f,\"station\":%d,\"event\":\"stall\",\"ms\":%.1f}\n",
                wall() + (s->st_s - t) / 1000.0, i, s->st_e - s->st_s);
    }
    double h1 = (t >= s->abs_s && t < s->abs_e) ? s->abs_e - t : 0;
    double h2 = (t >= s->st_s && t < s->st_e) ? s->st_e - t : 0;
    return h1 > h2 ? h1 : h2;
}

/* returns release time, or -1 to drop */
static double air(int i, int dir, int len, double t) {
    sta_t *s = &S[i]; int b = s->band;
    if (t - band_t0 >= 100.0) {
        for (int k = 0; k < 2; k++) {
            double util = band_bytes[k] * 8.0 / ((t - band_t0) / 1000.0) / (C.cap[k] * 1e6);
            band_load[k] = 0.7 * band_load[k] + 0.3 * util; band_bytes[k] = 0;
        }
        band_t0 = t;
    }
    band_bytes[b] += len;
    double rho = band_load[b] + s->nbusy, sigma = C.sigma[b], mult = 1.0;
    if (rho >= C.knee) { double r = rho > 0.95 ? 0.95 : rho; mult = 1.0 / (1.0 - r); sigma += 0.3; }
    double d = C.med[b] * exp(sigma * rnorm()) * mult + s->extra_ms;
    if (dir == 0 && s->psm && t - s->last_seen > C.psm_timeout) d += runif() * C.beacon;
    s->last_seen = t;
    d += events_hold(i, t);
    double lo = s->loss >= 0 ? s->loss : C.loss[b];
    if (runif() < lo) return -1;
    /* the shared half-duplex medium: frames queue for airtime (transmit time only); a station's own access
       wait, stall or scan delays that station's frames without occupying the medium for others */
    double txt = len * 8.0 / (C.cap[b] * (1.0 - s->nbusy) * 1e6) * 1e3;
    double start = t;
    if (busy_until[b] > start) {
        if (busy_until[b] - start > C.max_wait) return -1;
        start = busy_until[b];
    }
    busy_until[b] = start + txt;
    double td = start + txt + d;
    if (td < s->last[dir]) td = s->last[dir];
    s->last[dir] = td;
    s->dsum[dir] += td - t; s->dn[dir]++;
    return td;
}

typedef struct { double t; uint64_t seq; int fd, len; unsigned char *buf; } item;
static item *H; static size_t hn, hcap; static uint64_t hseq;
static int lt(item *a, item *b) { return a->t < b->t || (a->t == b->t && a->seq < b->seq); }
static void push(item it) {
    if (hn == hcap) { hcap = hcap ? hcap * 2 : 4096; H = realloc(H, hcap * sizeof(item)); }
    size_t i = hn++; H[i] = it;
    while (i) { size_t p = (i - 1) / 2; if (lt(&H[p], &H[i])) break; item x = H[p]; H[p] = H[i]; H[i] = x; i = p; }
}
static item pop(void) {
    item top = H[0]; H[0] = H[--hn]; size_t i = 0;
    for (;;) { size_t l = 2 * i + 1, r = l + 1, m = i;
        if (l < hn && lt(&H[l], &H[m])) m = l;
        if (r < hn && lt(&H[r], &H[m])) m = r;
        if (m == i) break; item x = H[m]; H[m] = H[i]; H[i] = x; i = m; }
    return top;
}
static void enqueue(int i, int dir, int fd, unsigned char *buf, int n, double t) {
    double td = air(i, dir, n, t);
    if (td < 0) { S[i].drop[dir]++; return; }
    S[i].tx[dir]++;
    item it = { td, hseq++, fd, n, malloc(n) }; memcpy(it.buf, buf, n); push(it);
}

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s <config>\n", argv[0]); return 2; }
    struct sched_param sp = { .sched_priority = 50 };
    sched_setscheduler(0, SCHED_FIFO, &sp);
    defaults(); load(argv[1], 1);
    char p[600];
    snprintf(p, sizeof p, "%s.events.jsonl", argv[1]); evlog = fopen(p, "a");
    snprintf(p, sizeof p, "%s.stats.jsonl", argv[1]); stlog = fopen(p, "a");
    struct pollfd pf[NSTA + 2]; int map[NSTA + 2]; int np = 0;
    for (int b = 0; b < 2; b++) if (C.ap[b][0]) { apfd[b] = open_tap(C.ap[b]); pf[np].fd = apfd[b]; pf[np].events = POLLIN; map[np++] = -1 - b; }
    double t0 = now_ms(); rs[0] = 1; rs[1] = 2; rs[2] = 3; rs[3] = 4;
    for (int i = 0; i < NSTA; i++) if (S[i].used) {
        S[i].fd = open_tap(S[i].tap); sta_reset(i, t0);
        pf[np].fd = S[i].fd; pf[np].events = POLLIN; map[np++] = i;
    }
    fprintf(stderr, "wifiemu: %d ports\n", np);
    struct stat st; time_t mt = 0; if (!stat(argv[1], &st)) mt = st.st_mtime;
    double next_cfg = t0 + 200, next_stat = t0 + 1000;
    unsigned char buf[MAXF];
    for (;;) {
        double t = now_ms(), wait = 50;
        if (hn) { wait = H[0].t - t; if (wait < 0) wait = 0; if (wait > 50) wait = 50; }
        struct timespec ts = { (time_t)(wait / 1000.0), (long)(fmod(wait, 1000.0) * 1e6) };
        ppoll(pf, np, &ts, NULL);
        for (int k = 0; k < np; k++) {
            if (!(pf[k].revents & POLLIN)) continue;
            for (int r = 0; r < 64; r++) {
                int n = read(pf[k].fd, buf, sizeof buf);
                if (n < 14) break;
                t = now_ms();
                if (map[k] < 0) {                       /* from the router's radio port: downlink */
                    int b = -1 - map[k];
                    int bcast = buf[0] & 1;
                    for (int i = 0; i < NSTA; i++) {
                        if (!S[i].used || S[i].band != b) continue;
                        if (bcast || !memcmp(buf, S[i].mac, 6)) enqueue(i, 0, S[i].fd, buf, n, t);
                    }
                } else {                                 /* from a station: uplink to its band's AP port */
                    int i = map[k];
                    enqueue(i, 1, apfd[S[i].band], buf, n, t);
                }
            }
        }
        t = now_ms();
        while (hn && H[0].t <= t + 0.02) { item it = pop(); if (write(it.fd, it.buf, it.len) < 0) {} free(it.buf); }
        if (t >= next_cfg) {
            next_cfg = t + 200;
            if (!stat(argv[1], &st) && st.st_mtime != mt) { mt = st.st_mtime; load(argv[1], 0); }
        }
        if (t >= next_stat) {
            next_stat = t + 1000;
            fprintf(stlog, "{\"t\":%.3f,\"load5\":%.3f,\"load24\":%.3f,\"sta\":{", wall(), band_load[0], band_load[1]);
            int first = 1;
            for (int i = 0; i < NSTA; i++) if (S[i].used) {
                fprintf(stlog, "%s\"%d\":[%llu,%llu,%llu,%llu,%.2f,%.2f]", first ? "" : ",", i,
                        (unsigned long long)S[i].tx[0], (unsigned long long)S[i].drop[0],
                        (unsigned long long)S[i].tx[1], (unsigned long long)S[i].drop[1],
                        S[i].dn[0] ? S[i].dsum[0] / S[i].dn[0] : 0, S[i].dn[1] ? S[i].dsum[1] / S[i].dn[1] : 0);
                S[i].dsum[0] = S[i].dsum[1] = 0; S[i].dn[0] = S[i].dn[1] = 0; first = 0;
            }
            fprintf(stlog, "}}\n"); fflush(stlog); fflush(evlog);
        }
    }
}
