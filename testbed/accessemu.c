/*
 * accessemu: field-calibrated access-network and home Wi-Fi emulator for the IntentQoS testbed.
 *
 * Runs in the ISP namespace on a TUN device that every forwarded packet is routed through
 * (the same plumbing as delayline.py). The kernel tbf before/after it stays the bottleneck
 * (rate + byte-limited FIFO); this program adds what tbf cannot:
 *
 *   WAN, per direction: base one-way delay, extra delay (e.g. 4G failover), half-normal jitter,
 *     GPON upstream DBA wait (uniform), Bernoulli loss and Gilbert-Elliott burst loss.
 *   Home Wi-Fi, per station (LAN device): lognormal access delay, contention growth above an
 *     airtime-utilisation knee (own band load + neighbour busy time), heavy-tailed spikes
 *     (stall episodes: Poisson in time, Pareto length), background-scan absences (Poisson), power-save wake delay for phones, residual loss.
 *   Packets are never reordered within a direction (WAN) or within a station and direction (Wi-Fi).
 *
 * Wi-Fi delay is applied here, on the WAN side of the gateway, rather than between the device and
 * the gateway: end-to-end delay is the same, but it means the gateway never queues behind Wi-Fi.
 * Documented as an approximation. Everything here is EMULATION (labelled, as the brief requires);
 * the parameters and their sources are in testbed/profiles/<name>.json.
 *
 * Configuration: a key=value file (path in argv[2]) re-read whenever it changes. A new "seed"
 * restarts every random process, so each arm of an experiment sees the same impairments.
 *
 *   accessemu <tun-name> <config-file>
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

#define MAXPKT 2048
#define NSTA 256

/* ------------------------------------------------------------------ RNG (xoshiro256**) */
static uint64_t rs[4];
static uint64_t rotl(uint64_t x, int k) { return (x << k) | (x >> (64 - k)); }
static uint64_t rnext(void) {
    uint64_t r = rotl(rs[1] * 5, 7) * 9, t = rs[1] << 17;
    rs[2] ^= rs[0]; rs[3] ^= rs[1]; rs[1] ^= rs[2]; rs[0] ^= rs[3]; rs[2] ^= t; rs[3] = rotl(rs[3], 45);
    return r;
}
static double runif(void) { return (rnext() >> 11) * (1.0 / 9007199254740992.0) + 1e-18; }
static void rseed(uint64_t s) {
    for (int i = 0; i < 4; i++) { s += 0x9E3779B97F4A7C15ull; uint64_t z = s;
        z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull; z = (z ^ (z >> 27)) * 0x94D049BB133111EBull; rs[i] = z ^ (z >> 31); }
}
static double rnorm(void) { return sqrt(-2.0 * log(runif())) * cos(2 * M_PI * runif()); }
/* per-station event streams: scans and stall episodes are scheduled in time from their own RNG, so every
   arm of an experiment (same seed) sees the same events at the same offsets, whatever traffic it carries */
static double sunif(uint64_t *q) {
    uint64_t r = rotl(q[1] * 5, 7) * 9, t = q[1] << 17;
    q[2] ^= q[0]; q[3] ^= q[1]; q[1] ^= q[2]; q[0] ^= q[3]; q[2] ^= t; q[3] = rotl(q[3], 45);
    return (r >> 11) * (1.0 / 9007199254740992.0) + 1e-18;
}
static void sseed(uint64_t *q, uint64_t s) {
    for (int i = 0; i < 4; i++) { s += 0x9E3779B97F4A7C15ull; uint64_t z = s;
        z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull; z = (z ^ (z >> 27)) * 0x94D049BB133111EBull; q[i] = z ^ (z >> 31); }
}
static double rexp(double mean) { return -mean * log(runif()); }

/* ------------------------------------------------------------------ configuration */
typedef struct {
    double base_ms, extra_ms, jitter_ms, dba_min_ms, dba_max_ms, loss;
    double ge_p, ge_r, ge_loss_bad, ge_loss_good; /* Gilbert-Elliott: p good->bad, r bad->good */
} dir_cfg;

static struct {
    uint64_t seed, epoch;
    dir_cfg d[2];                      /* 0 = down (towards the home), 1 = up */
    double w_med[2], w_sigma[2], w_cap_mbps[2], w_stall_rate[2], w_loss[2];
    double w_knee, w_tail_xm, w_tail_alpha, w_tail_cap, w_scan_mean_s, w_absence_min, w_absence_max;
    int w_dwell_min, w_dwell_max;
    double w_psm_timeout_ms, w_beacon_ms;
    int sta_band[NSTA];                /* -1 wired, 0 = 5 GHz, 1 = 2.4 GHz */
    double sta_nbusy[NSTA];            /* neighbour busy fraction on the station's channel */
    int sta_psm[NSTA];
    uint32_t lan4;                     /* 192.168.50.0/24 network (host order) */
    uint32_t gw4;                      /* gateway WAN /30 network */
    int enabled_wifi;
} C;

static void cfg_defaults(void) {
    memset(&C, 0, sizeof C);
    for (int i = 0; i < NSTA; i++) C.sta_band[i] = -1;
    C.w_med[0] = 1.0; C.w_sigma[0] = 0.8; C.w_cap_mbps[0] = 400; C.w_stall_rate[0] = 1.0 / 30; C.w_loss[0] = 0.0005;
    C.w_med[1] = 2.0; C.w_sigma[1] = 1.0; C.w_cap_mbps[1] = 100; C.w_stall_rate[1] = 1.0 / 10; C.w_loss[1] = 0.002;
    C.w_knee = 0.5; C.w_tail_xm = 10; C.w_tail_alpha = 1.5; C.w_tail_cap = 200;
    C.w_scan_mean_s = 180; C.w_dwell_min = 2; C.w_dwell_max = 4; C.w_absence_min = 20; C.w_absence_max = 40;
    C.w_psm_timeout_ms = 200; C.w_beacon_ms = 102.4;
    C.lan4 = (192u << 24) | (168u << 16) | (50u << 8);
    C.gw4 = (100u << 24) | (64u << 16);
    C.enabled_wifi = 1;
}

static int set_dir(dir_cfg *d, const char *k, double v) {
    if (!strcmp(k, "base_ms")) d->base_ms = v; else if (!strcmp(k, "extra_ms")) d->extra_ms = v;
    else if (!strcmp(k, "jitter_ms")) d->jitter_ms = v; else if (!strcmp(k, "dba_min_ms")) d->dba_min_ms = v;
    else if (!strcmp(k, "dba_max_ms")) d->dba_max_ms = v; else if (!strcmp(k, "loss")) d->loss = v;
    else if (!strcmp(k, "ge_p")) d->ge_p = v; else if (!strcmp(k, "ge_r")) d->ge_r = v;
    else if (!strcmp(k, "ge_loss_bad")) d->ge_loss_bad = v; else if (!strcmp(k, "ge_loss_good")) d->ge_loss_good = v;
    else return 0;
    return 1;
}

static void cfg_load(const char *path) {
    FILE *f = fopen(path, "r");
    if (!f) return;
    cfg_defaults();
    char line[512];
    while (fgets(line, sizeof line, f)) {
        char *eq = strchr(line, '=');
        if (!eq || line[0] == '#') continue;
        *eq = 0;
        char *k = line, *v = eq + 1;
        v[strcspn(v, "\r\n")] = 0;
        double x = atof(v);
        if (!strcmp(k, "seed")) C.seed = strtoull(v, NULL, 10);
        else if (!strcmp(k, "epoch")) C.epoch = strtoull(v, NULL, 10);
        else if (!strcmp(k, "lan4") || !strcmp(k, "gw4")) {   /* optional: addresses of this deployment */
            unsigned a, b, c, d;
            if (sscanf(v, "%u.%u.%u.%u", &a, &b, &c, &d) == 4) {
                uint32_t ip = a << 24 | b << 16 | c << 8 | d;
                if (k[0] == 'l') C.lan4 = ip & 0xFFFFFF00u; else C.gw4 = ip & 0xFFFFFFFCu;
            }
        }
        else if (!strncmp(k, "down.", 5)) set_dir(&C.d[0], k + 5, x);
        else if (!strncmp(k, "up.", 3)) set_dir(&C.d[1], k + 3, x);
        else if (!strcmp(k, "wifi5.med_ms")) C.w_med[0] = x; else if (!strcmp(k, "wifi5.sigma")) C.w_sigma[0] = x;
        else if (!strcmp(k, "wifi5.cap_mbps")) C.w_cap_mbps[0] = x; else if (!strcmp(k, "wifi5.stall_per_s")) C.w_stall_rate[0] = x;
        else if (!strcmp(k, "wifi5.loss")) C.w_loss[0] = x;
        else if (!strcmp(k, "wifi24.med_ms")) C.w_med[1] = x; else if (!strcmp(k, "wifi24.sigma")) C.w_sigma[1] = x;
        else if (!strcmp(k, "wifi24.cap_mbps")) C.w_cap_mbps[1] = x; else if (!strcmp(k, "wifi24.stall_per_s")) C.w_stall_rate[1] = x;
        else if (!strcmp(k, "wifi24.loss")) C.w_loss[1] = x;
        else if (!strcmp(k, "wifi.knee")) C.w_knee = x; else if (!strcmp(k, "wifi.tail_xm_ms")) C.w_tail_xm = x;
        else if (!strcmp(k, "wifi.tail_alpha")) C.w_tail_alpha = x; else if (!strcmp(k, "wifi.tail_cap_ms")) C.w_tail_cap = x;
        else if (!strcmp(k, "wifi.scan_mean_s")) C.w_scan_mean_s = x;
        else if (!strcmp(k, "wifi.dwell_min")) C.w_dwell_min = (int)x; else if (!strcmp(k, "wifi.dwell_max")) C.w_dwell_max = (int)x;
        else if (!strcmp(k, "wifi.absence_min_ms")) C.w_absence_min = x; else if (!strcmp(k, "wifi.absence_max_ms")) C.w_absence_max = x;
        else if (!strcmp(k, "wifi.psm_timeout_ms")) C.w_psm_timeout_ms = x; else if (!strcmp(k, "wifi.beacon_ms")) C.w_beacon_ms = x;
        else if (!strcmp(k, "wifi.enabled")) C.enabled_wifi = (int)x;
        else if (!strncmp(k, "sta.", 4)) {           /* sta.<host>=band,neighbour_busy,psm   band 5|24 */
            int h = atoi(k + 4);
            if (h > 0 && h < NSTA) {
                int band = atoi(v); double nb = 0; int psm = 0;
                char *c1 = strchr(v, ','); if (c1) { nb = atof(c1 + 1); char *c2 = strchr(c1 + 1, ','); if (c2) psm = atoi(c2 + 1); }
                C.sta_band[h] = band == 5 ? 0 : band == 24 ? 1 : -1; C.sta_nbusy[h] = nb; C.sta_psm[h] = psm;
            }
        }
    }
    fclose(f);
}

/* ------------------------------------------------------------------ state */
static double now_ms(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec * 1e3 + t.tv_nsec / 1e6; }

typedef struct {
    double last[2]; double last_seen;
    uint64_t q[4];                         /* this station's event stream */
    double scan_next, dw_t, abs_s, abs_e; int dw_left;
    double st_s, st_e;
} sta_state;
static sta_state S[NSTA];
static double wan_last[2];
static int ge_bad[2];
static double band_bytes[2], band_t0;   /* bytes in the current 100 ms window per band */
static double band_load[2];             /* utilisation estimate (EWMA, fraction of capacity) */
static uint64_t cnt_in, cnt_drop_wan, cnt_drop_wifi;

static void state_reset(void) {
    rseed(C.seed ? C.seed : 1);
    double t = now_ms();
    memset(S, 0, sizeof S);
    for (int i = 0; i < NSTA; i++) {
        S[i].last_seen = t;
        sseed(S[i].q, (C.seed ? C.seed : 1) * 6364136223846793005ull + (uint64_t)i * 1442695040888963407ull);
        S[i].scan_next = t - C.w_scan_mean_s * 1000.0 * log(sunif(S[i].q));
        S[i].st_s = S[i].st_e = t;   /* the first stall episode is drawn from the reset time */
        S[i].abs_s = S[i].abs_e = 0;
    }
    wan_last[0] = wan_last[1] = 0; ge_bad[0] = ge_bad[1] = 0;
    band_bytes[0] = band_bytes[1] = 0; band_load[0] = band_load[1] = 0; band_t0 = t;
}

/* ------------------------------------------------------------------ heap of pending packets */
typedef struct { double t; uint64_t seq; int len; unsigned char *buf; } item;
static item *H; static size_t hn, hcap; static uint64_t hseq;
static void hpush(item it) {
    if (hn == hcap) { hcap = hcap ? hcap * 2 : 4096; H = realloc(H, hcap * sizeof(item)); }
    size_t i = hn++; H[i] = it;
    while (i) { size_t p = (i - 1) / 2;
        if (H[p].t < H[i].t || (H[p].t == H[i].t && H[p].seq < H[i].seq)) break;
        item x = H[p]; H[p] = H[i]; H[i] = x; i = p; }
}
static item hpop(void) {
    item top = H[0]; H[0] = H[--hn]; size_t i = 0;
    for (;;) { size_t l = 2 * i + 1, r = l + 1, m = i;
        if (l < hn && (H[l].t < H[m].t || (H[l].t == H[m].t && H[l].seq < H[m].seq))) m = l;
        if (r < hn && (H[r].t < H[m].t || (H[r].t == H[m].t && H[r].seq < H[m].seq))) m = r;
        if (m == i) break;
        item x = H[m]; H[m] = H[i]; H[i] = x; i = m; }
    return top;
}

/* ------------------------------------------------------------------ models */
static int wan_drop(int dir) {
    dir_cfg *d = &C.d[dir];
    if (d->loss > 0 && runif() < d->loss) return 1;
    if (d->ge_p > 0) {
        if (ge_bad[dir]) { if (runif() < d->ge_r) ge_bad[dir] = 0; }
        else if (runif() < d->ge_p) ge_bad[dir] = 1;
        double pl = ge_bad[dir] ? d->ge_loss_bad : d->ge_loss_good;
        if (pl > 0 && runif() < pl) return 1;
    }
    return 0;
}

static double wan_delay(int dir) {
    dir_cfg *d = &C.d[dir];
    double x = d->base_ms + d->extra_ms;
    if (d->jitter_ms > 0) x += fabs(rnorm()) * d->jitter_ms;
    if (d->dba_max_ms > 0) x += d->dba_min_ms + runif() * (d->dba_max_ms - d->dba_min_ms);
    return x;
}

static double events_hold(sta_state *s, int b, double nbusy, double t) {
    /* advance this station's scheduled events up to t; return how long a packet arriving at t is held */
    for (;;) {
        if (s->dw_left > 0 && s->dw_t <= t) {           /* off-channel dwell of a background scan */
            double a = C.w_absence_min + sunif(s->q) * (C.w_absence_max - C.w_absence_min);
            s->abs_s = s->dw_t; s->abs_e = s->dw_t + a; s->dw_t = s->abs_e + 20.0; s->dw_left--;
            continue;
        }
        if (s->dw_left == 0 && s->scan_next <= t) {
            s->dw_left = C.w_dwell_min + (int)(sunif(s->q) * (C.w_dwell_max - C.w_dwell_min + 1));
            s->dw_t = s->scan_next; s->scan_next -= C.w_scan_mean_s * 1000.0 * log(sunif(s->q));
            continue;
        }
        break;
    }
    double rate = C.w_stall_rate[b] * (1.0 + 2.0 * nbusy);       /* busier channel, more stalls */
    while (rate > 0 && s->st_e <= t) {                             /* draw the next stall episode after */
        double gap = -1000.0 / rate * log(sunif(s->q));             /* the previous one ends: interference */
        double p = C.w_tail_xm / pow(sunif(s->q), 1.0 / C.w_tail_alpha);  /* burst, retries, rate collapse */
        s->st_s = s->st_e + gap; s->st_e = s->st_s + (p > C.w_tail_cap ? C.w_tail_cap : p);
    }
    double h1 = (t >= s->abs_s && t < s->abs_e) ? s->abs_e - t : 0;
    double h2 = (t >= s->st_s && t < s->st_e) ? s->st_e - t : 0;
    return h1 > h2 ? h1 : h2;
}

static double wifi_delay(int h, int dir, int len, double t, int *drop) {
    int b = C.sta_band[h];
    sta_state *s = &S[h];
    /* airtime utilisation: own band load (EWMA over 100 ms windows) + neighbour busy time */
    if (t - band_t0 >= 100.0) {
        for (int k = 0; k < 2; k++) {
            double util = band_bytes[k] * 8.0 / ((t - band_t0) / 1000.0) / (C.w_cap_mbps[k] * 1e6);
            band_load[k] = 0.7 * band_load[k] + 0.3 * util; band_bytes[k] = 0;
        }
        band_t0 = t;
    }
    band_bytes[b] += len;
    double rho = band_load[b] + C.sta_nbusy[h];
    double sigma = C.w_sigma[b], mult = 1.0;
    if (rho >= C.w_knee) { double r = rho > 0.95 ? 0.95 : rho; mult = 1.0 / (1.0 - r); sigma += 0.3; }
    double d = C.w_med[b] * exp(sigma * rnorm()) * mult;
    if (dir == 0 && C.sta_psm[h] && t - s->last_seen > C.w_psm_timeout_ms) d += runif() * C.w_beacon_ms;
    s->last_seen = t;
    d += events_hold(s, b, C.sta_nbusy[h], t);
    if (runif() < C.w_loss[b]) *drop = 1;
    return d;
}

/* ------------------------------------------------------------------ packet classification */
static int classify(const unsigned char *p, int n, int *dir, int *host) {
    *host = -1;
    if (n >= 20 && (p[0] >> 4) == 4) {
        uint32_t src = (uint32_t)p[12] << 24 | p[13] << 16 | p[14] << 8 | p[15];
        uint32_t dst = (uint32_t)p[16] << 24 | p[17] << 16 | p[18] << 8 | p[19];
        if ((dst & 0xFFFFFF00u) == C.lan4) { *dir = 0; *host = dst & 0xFF; return 1; }
        if ((dst & 0xFFFFFFFCu) == C.gw4) { *dir = 0; return 1; }
        *dir = 1;
        if ((src & 0xFFFFFF00u) == C.lan4) *host = src & 0xFF;
        return 1;
    }
    if (n >= 40 && (p[0] >> 4) == 6) {            /* LAN fd00:50::/64, gateway WAN fd00:64::/64 */
        const unsigned char *s = p + 8, *d = p + 24;
        static const unsigned char lan6[8] = {0xfd, 0, 0, 0x50, 0, 0, 0, 0}, gw6[8] = {0xfd, 0, 0, 0x64, 0, 0, 0, 0};
        if (!memcmp(d, lan6, 8)) { *dir = 0; *host = d[15]; return 1; }
        if (!memcmp(d, gw6, 8)) { *dir = 0; return 1; }
        *dir = 1;
        if (!memcmp(s, lan6, 8)) *host = s[15];
        return 1;
    }
    return 0;
}

static int open_tun(const char *name) {
    int fd = open("/dev/net/tun", O_RDWR | O_NONBLOCK);
    if (fd < 0) { perror("tun"); exit(1); }
    struct ifreq ifr; memset(&ifr, 0, sizeof ifr);
    ifr.ifr_flags = IFF_TUN | IFF_NO_PI; strncpy(ifr.ifr_name, name, IFNAMSIZ - 1);
    if (ioctl(fd, TUNSETIFF, &ifr) < 0) { perror("TUNSETIFF"); exit(1); }
    return fd;
}

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: %s <tun> <config>\n", argv[0]); return 2; }
    struct sched_param sp = { .sched_priority = 50 };
    sched_setscheduler(0, SCHED_FIFO, &sp);
    int fd = open_tun(argv[1]);
    char cmd[256];
    snprintf(cmd, sizeof cmd, "ip link set %s up mtu 1500 && ip link set dev %s txqueuelen 10000", argv[1], argv[1]);
    if (system(cmd) != 0) fprintf(stderr, "link setup failed\n");
    cfg_defaults(); cfg_load(argv[2]); state_reset();
    uint64_t seed_now = C.seed, epoch_now = C.epoch;
    struct stat st; time_t mt = 0; long mtn = 0;
    if (!stat(argv[2], &st)) { mt = st.st_mtim.tv_sec; mtn = st.st_mtim.tv_nsec; }
    double next_cfg = now_ms() + 100, next_log = now_ms() + 10000;
    fprintf(stderr, "accessemu %s: down %.1f ms, up %.1f ms, seed %llu\n", argv[1], C.d[0].base_ms, C.d[1].base_ms,
            (unsigned long long)C.seed);
    unsigned char buf[MAXPKT];
    for (;;) {
        double t = now_ms();
        double wait = 50.0;
        if (hn) { wait = H[0].t - t; if (wait < 0) wait = 0; if (wait > 50) wait = 50; }
        /* ppoll takes a nanosecond timeout; with SCHED_FIFO the timer slack is zero, so the release is precise */
        struct pollfd pfd = { fd, POLLIN, 0 };
        struct timespec ts = { (time_t)(wait / 1000.0), (long)(fmod(wait, 1000.0) * 1e6) };
        ppoll(&pfd, 1, &ts, NULL);
        for (int k = 0; k < 256; k++) {
            int n = read(fd, buf, sizeof buf);
            if (n <= 0) break;
            t = now_ms(); cnt_in++;
            int dir, host, drop = 0;
            if (!classify(buf, n, &dir, &host)) { if (write(fd, buf, n) < 0) {} continue; }
            if (wan_drop(dir)) { cnt_drop_wan++; continue; }
            double td;
            int wifi = C.enabled_wifi && host >= 0 && host < NSTA && C.sta_band[host] >= 0;
            if (dir == 0) {
                td = t + wan_delay(0); if (td < wan_last[0]) td = wan_last[0]; wan_last[0] = td;
                if (wifi) { td += wifi_delay(host, 0, n, td, &drop); if (td < S[host].last[0]) td = S[host].last[0]; S[host].last[0] = td; }
            } else {
                /* upload: the Wi-Fi hop keeps each station's packets in order, but a stalled station must not
                   hold back the others (or the router's own packets), so WAN ordering is per sender too */
                td = t;
                if (wifi) td += wifi_delay(host, 1, n, t, &drop);
                td += wan_delay(1);
                if (host >= 0 && host < NSTA) { if (td < S[host].last[1]) td = S[host].last[1]; S[host].last[1] = td; }
                else { if (td < wan_last[1]) td = wan_last[1]; wan_last[1] = td; }
            }
            if (drop) { cnt_drop_wifi++; continue; }
            item it = { td, hseq++, n, malloc(n) }; memcpy(it.buf, buf, n); hpush(it);
        }
        t = now_ms();
        while (hn && H[0].t <= t + 0.02) { item it = hpop(); if (write(fd, it.buf, it.len) < 0) {} free(it.buf); }
        if (t >= next_cfg) {
            next_cfg = t + 100;
            if (!stat(argv[2], &st) && (st.st_mtim.tv_sec != mt || st.st_mtim.tv_nsec != mtn)) {
                mt = st.st_mtim.tv_sec; mtn = st.st_mtim.tv_nsec; cfg_load(argv[2]);
                if (C.seed != seed_now || C.epoch != epoch_now) { seed_now = C.seed; epoch_now = C.epoch; state_reset(); }
            }
        }
        if (t >= next_log) {
            next_log = t + 10000;
            fprintf(stderr, "in %llu, wan drops %llu, wifi drops %llu, queued %zu, load5 %.2f load24 %.2f\n",
                    (unsigned long long)cnt_in, (unsigned long long)cnt_drop_wan, (unsigned long long)cnt_drop_wifi, hn,
                    band_load[0], band_load[1]);
        }
    }
}
