/*
 * iq-capture -- header-only flow accounting for IntentQoS on routers.
 *
 * Replaces the engine's Python capture loop on low-power gateways. Packets are read from a
 * TPACKET_V3 memory-mapped ring (no per-packet system call); a classic BPF filter truncates
 * every packet to SNAPLEN bytes inside the kernel, so payload never reaches user space.
 * Only Ethernet/VLAN, IPv4/IPv6 and TCP/UDP header fields are parsed.
 *
 * Per flow it keeps exactly the aggregates the Python FlowTable keeps (counts, byte sums, sums of
 * squares, maxima, inter-arrival sums, small/large counts, the first 16 packets), so the engine's
 * features and model are unchanged.
 *
 * Experience fields (IntentQoS 2.0), also header-only:
 *   - ECN codepoints (ECT(0), ECT(1) = L4S, CE) and the highest DSCP the LAN device sent,
 *   - TCP retransmissions (or reordering) per direction, from sequence numbers,
 *   - passive RTT from TCP timestamps (ePPing method), split at the router into the internet part
 *     (router -> server -> router) and the LAN part (router -> device -> router),
 *   - QUIC spin-bit RTT (RFC 9000 s17.4: one public header bit meant for passive observers),
 *   - video-frame bursts per direction (header-only call-quality estimate).
 *
 * Kernel flow engine (-e OBJ): instead of reading packets, attach iq-flow.bpf.o with tc (LAN ingress and
 * egress, WAN egress), which keeps the same aggregates inside the kernel and applies the engine's class
 * decisions (mark, priority, DSCP) in the same pass. This helper then reads the pinned flow map once per
 * window and prints the same lines, so the engine cannot tell the two modes apart except by speed.
 *
 * Protocol on stdin/stdout (driven by intentqos/capture.py: HelperCapture):
 *   engine -> helper   "R\n"                      close the current 0.5 s window
 *                      "M <fam> <lan> <remote> <proto> <lport> <rport> <tin>\n"   class verdict (-e; tin 255 = none)
 *                      "P <mark_dscp> <prio0> <dscp0> ... <prio4> <dscp4>\n"       per-class priority/DSCP (-e)
 *                      "C\n"                                                        clear all verdicts (-e)
 *   helper -> engine   "W <flow fields...>\n"    one line per live flow (its closed window)
 *                      "X <fam> <lan> <remote> <proto> <lport> <rport>\n"  flow expired
 *                      "E <packets> <bytes> <kernel_packets> <kernel_drops> <flows> <overflow>\n"
 *
 * Build:  cc -O2 -Wall -o iq-capture iq-capture.c                       (host)
 *         aarch64-linux-musl-gcc -O2 -static -o iq-capture iq-capture.c (router, static)
 * Usage:  iq-capture -i br-lan -n 192.168.1.0/24 -n fd00::/64 [-t 30]
 *         iq-capture -e /usr/lib/intentqos/iq-flow.bpf.o -w eth0 -i br-lan -n 192.168.1.0/24   (kernel engine)
 * Licence: MIT (same as IntentQoS).
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/filter.h>
#include <linux/if_ether.h>
#include <linux/if_packet.h>
#include <net/if.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <linux/bpf.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/vfs.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "iq-flow.h"

#define SNAPLEN 96
#define EARLY_N 16
#define TABLE_SIZE 8192            /* power of two */
#define MAX_FLOWS 4096
#define RING_BLOCK (1 << 18)       /* 256 KiB */
#define BLOCK_NR 8                 /* 2 MiB ring */
#define FRAME_SIZE 2048
#define VID_MIN 200                /* bytes: packets this large can carry video */
#define VID_GAP 0.004              /* s: a gap this long between video-size packets starts a new frame */
#define TS_MAX_AGE 3.0             /* s: an unanswered timestamp sample is dropped after this */
#define TS_SLOTS 16                /* outstanding timestamp samples kept per direction */
#define TS_GAP_MIN 0.002           /* s: samples are spaced at least this far apart (ePPing rate limit) ... */
#define TS_GAP_MAX 0.064           /* ... and at most this: 16 slots then cover a 1 s round trip */

struct key {
    uint8_t fam, proto;
    uint16_t lport, rport;
    uint8_t lan[16], rem[16];
};

struct win {
    uint64_t pk_up, by_up, sq_up, mx_up, pk_dn, by_dn, sq_dn, mx_dn, small, large, iat_n_up, iat_n_dn;
    double iat_s_up, iat_q_up, iat_s_dn, iat_q_dn;
    uint64_t ect0, ect1, ce, retx_up, retx_dn, rw_n, rl_n, fr_up, fr_dn, sp_n;
    double rw_s, rw_min, rw_max, rl_s, rl_min, rl_max, sp_s;
    unsigned dscp_up;              /* 255 = none seen this window */
};

struct flow {
    uint8_t used;                  /* 0 empty, 1 live, 2 tombstone */
    struct key k;
    double first, last, last_up, last_dn;
    uint64_t pk_up, pk_dn, by_up, by_dn;
    struct win w;
    float e_t[EARLY_N];
    uint16_t e_size[EARLY_N];
    uint8_t e_dir[EARLY_N], early_n, early_sent, syn, fin;
    uint32_t seq_hi_up, seq_hi_dn, tsu_v[TS_SLOTS], tsd_v[TS_SLOTS], last_tsv_up, last_tsv_dn;
    uint8_t seq_ok_up, seq_ok_dn, spin_seen, spin_last;
    double tsu_t[TS_SLOTS], tsd_t[TS_SLOTS], spin_t, vid_up, vid_dn;
    double ts_next_up, ts_next_dn, ts_gap_up, ts_gap_dn;   /* sample spacing: about a sixteenth of the RTT */
    uint32_t seen;                 /* kernel mode: the roll that last found this flow in the kernel map */
};

struct net { int fam; uint8_t addr[16]; int plen; };

static struct flow *tab;
static int nflows, ntomb;
static uint64_t packets, bytes_total, overflow, kpk, kdrop;
static struct net nets[32];
static int nnets;
static double idle_timeout = 30.0;
static volatile sig_atomic_t stop;

static double mono(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; }
static double realt(void) { struct timespec t; clock_gettime(CLOCK_REALTIME, &t); return t.tv_sec + t.tv_nsec / 1e9; }

static int parse_net(const char *s, struct net *n) {
    char buf[64]; const char *slash = strchr(s, '/');
    if (!slash || (size_t)(slash - s) >= sizeof buf) return -1;
    memcpy(buf, s, slash - s); buf[slash - s] = 0;
    n->plen = atoi(slash + 1);
    memset(n->addr, 0, 16);
    if (inet_pton(AF_INET, buf, n->addr) == 1) { n->fam = 4; return n->plen <= 32 ? 0 : -1; }
    if (inet_pton(AF_INET6, buf, n->addr) == 1) { n->fam = 6; return n->plen <= 128 ? 0 : -1; }
    return -1;
}

static int in_net(int fam, const uint8_t *a) {
    for (int i = 0; i < nnets; i++) {
        if (nets[i].fam != fam) continue;
        int full = nets[i].plen / 8, rem = nets[i].plen % 8;
        if (memcmp(a, nets[i].addr, full)) continue;
        if (rem) { uint8_t m = (uint8_t)(0xFF << (8 - rem)); if ((a[full] & m) != (nets[i].addr[full] & m)) continue; }
        return 1;
    }
    return 0;
}

static uint32_t hash_key(const struct key *k) {
    const uint8_t *p = (const uint8_t *)k; uint32_t h = 2166136261u;
    for (size_t i = 0; i < sizeof *k; i++) { h ^= p[i]; h *= 16777619u; }
    return h;
}

static void rebuild(void) {
    struct flow *old = tab;
    tab = calloc(TABLE_SIZE, sizeof *tab);
    if (!tab) { perror("calloc"); exit(1); }
    ntomb = 0;
    for (int i = 0; i < TABLE_SIZE; i++) {
        if (old[i].used != 1) continue;
        uint32_t h = hash_key(&old[i].k) & (TABLE_SIZE - 1);
        while (tab[h].used) h = (h + 1) & (TABLE_SIZE - 1);
        tab[h] = old[i];
    }
    free(old);
}

static struct flow *lookup(const struct key *k, double now) {
    uint32_t h = hash_key(k) & (TABLE_SIZE - 1);
    int tomb = -1;
    for (int n = 0; n < TABLE_SIZE; n++) {
        struct flow *f = &tab[h];
        if (f->used == 0) break;
        if (f->used == 2) { if (tomb < 0) tomb = (int)h; }
        else if (!memcmp(&f->k, k, sizeof *k)) return f;
        h = (h + 1) & (TABLE_SIZE - 1);
    }
    if (nflows >= MAX_FLOWS) { overflow++; return NULL; }
    struct flow *f = tomb >= 0 ? &tab[tomb] : &tab[h];
    if (tomb >= 0) ntomb--;
    memset(f, 0, sizeof *f);
    f->used = 1; f->k = *k; f->first = f->last = now; f->w.dscp_up = 255;
    nflows++;
    return f;
}

static void account(const uint8_t *b, unsigned caplen, unsigned wirelen, double now) {
    (void)wirelen;
    if (caplen < 14) return;
    unsigned off = 14, etype = (b[12] << 8) | b[13];
    while ((etype == 0x8100 || etype == 0x88A8) && caplen >= off + 4) { etype = (b[off + 2] << 8) | b[off + 3]; off += 4; }
    uint8_t src[16] = {0}, dst[16] = {0}; int fam; unsigned proto, l4, iplen, tos; int frag = 0;
    if (etype == 0x0800) {
        if (caplen < off + 20) return;
        unsigned ihl = (b[off] & 0x0F) * 4;
        tos = b[off + 1];
        iplen = (b[off + 2] << 8) | b[off + 3];
        frag = ((b[off + 6] & 0x1F) << 8) | b[off + 7];
        proto = b[off + 9];
        memcpy(src, b + off + 12, 4); memcpy(dst, b + off + 16, 4);
        l4 = off + ihl; fam = 4;
    } else if (etype == 0x86DD) {
        if (caplen < off + 40) return;
        iplen = ((b[off + 4] << 8) | b[off + 5]) + 40;
        tos = ((b[off] & 0x0F) << 4) | (b[off + 1] >> 4);
        proto = b[off + 6];
        memcpy(src, b + off + 8, 16); memcpy(dst, b + off + 24, 16);
        l4 = off + 40; fam = 6;
        for (int hops = 0; (proto == 0 || proto == 43 || proto == 60) && caplen >= l4 + 2 && hops < 4; hops++) {
            proto = b[l4]; l4 += (b[l4 + 1] + 1) * 8;
        }
    } else return;
    unsigned sport = 0, dport = 0, flags = 0;
    uint32_t seq = 0, tsval = 0, tsecr = 0; int paylen = -1, spin = -1;
    if (!frag && (proto == 6 || proto == 17) && caplen >= l4 + 4) {
        sport = (b[l4] << 8) | b[l4 + 1]; dport = (b[l4 + 2] << 8) | b[l4 + 3];
        if (proto == 6 && caplen >= l4 + 14) flags = b[l4 + 13];
        if (proto == 6 && caplen >= l4 + 13) {
            unsigned doff = (b[l4 + 12] >> 4) * 4;
            seq = ((uint32_t)b[l4 + 4] << 24) | ((uint32_t)b[l4 + 5] << 16) | ((uint32_t)b[l4 + 6] << 8) | b[l4 + 7];
            paylen = (int)iplen - (int)(l4 - off) - (int)doff;
            unsigned i = l4 + 20, end = l4 + doff;
            if (end > caplen) end = caplen;
            while (doff >= 20 && i < end) {               /* TCP options: find the timestamp option (kind 8) */
                unsigned kind = b[i];
                if (kind == 0) break;
                if (kind == 1) { i++; continue; }
                if (i + 1 >= end) break;
                unsigned len = b[i + 1];
                if (len < 2) break;
                if (kind == 8 && len == 10 && i + 10 <= end) {
                    tsval = ((uint32_t)b[i + 2] << 24) | ((uint32_t)b[i + 3] << 16) | ((uint32_t)b[i + 4] << 8) | b[i + 5];
                    tsecr = ((uint32_t)b[i + 6] << 24) | ((uint32_t)b[i + 7] << 16) | ((uint32_t)b[i + 8] << 8) | b[i + 9];
                    break;
                }
                i += len;
            }
        }
        if (proto == 17 && (sport == 443 || dport == 443) && caplen >= l4 + 9) {
            unsigned q = b[l4 + 8];                       /* QUIC public flags: short header + fixed bit */
            if (!(q & 0x80) && (q & 0x40)) spin = (q >> 5) & 1;
        }
    }
    int s_lan = in_net(fam, src), d_lan = in_net(fam, dst);
    if (s_lan == d_lan) return;                          /* LAN<->LAN or transit */
    int up = s_lan;
    struct key k; memset(&k, 0, sizeof k);
    k.fam = (uint8_t)fam; k.proto = (uint8_t)proto;
    memcpy(k.lan, up ? src : dst, 16); memcpy(k.rem, up ? dst : src, 16);
    k.lport = (uint16_t)(up ? sport : dport); k.rport = (uint16_t)(up ? dport : sport);
    struct flow *f = lookup(&k, now);
    packets++; bytes_total += iplen;
    if (!f) return;
    f->last = now;
    struct win *w = &f->w;
    uint64_t sz = iplen;
    if (up) {
        f->pk_up++; f->by_up += sz; w->pk_up++; w->by_up += sz; w->sq_up += sz * sz; if (sz > w->mx_up) w->mx_up = sz;
        if (f->last_up > 0) { double d = now - f->last_up; w->iat_n_up++; w->iat_s_up += d; w->iat_q_up += d * d; }
        f->last_up = now;
    } else {
        f->pk_dn++; f->by_dn += sz; w->pk_dn++; w->by_dn += sz; w->sq_dn += sz * sz; if (sz > w->mx_dn) w->mx_dn = sz;
        if (f->last_dn > 0) { double d = now - f->last_dn; w->iat_n_dn++; w->iat_s_dn += d; w->iat_q_dn += d * d; }
        f->last_dn = now;
    }
    if (sz < 300) w->small++; else if (sz > 1200) w->large++;
    unsigned ecn = tos & 3;
    if (ecn == 2) w->ect0++; else if (ecn == 1) w->ect1++; else if (ecn == 3) w->ce++;
    if (up && (w->dscp_up == 255 || (tos >> 2) > w->dscp_up)) w->dscp_up = tos >> 2;   /* highest DSCP the device sent */
    if (sz >= VID_MIN) {                                  /* video frames = bursts of large packets */
        double *lv = up ? &f->vid_up : &f->vid_dn;
        if (*lv == 0 || now - *lv > VID_GAP) { if (up) w->fr_up++; else w->fr_dn++; }
        *lv = now;
    }
    if (paylen > 0) {                                     /* retransmission (or reordering): no new data */
        uint32_t e = seq + (uint32_t)paylen;
        uint32_t *hi = up ? &f->seq_hi_up : &f->seq_hi_dn; uint8_t *ok = up ? &f->seq_ok_up : &f->seq_ok_dn;
        if (*ok && (int32_t)(e - *hi) <= 0) { if (up) w->retx_up++; else w->retx_dn++; }
        else { *hi = e; *ok = 1; }
    }
    if (tsval) {                                          /* passive RTT from TCP timestamps (ePPing) */
        uint32_t *mine_v = up ? f->tsu_v : f->tsd_v, *their_v = up ? f->tsd_v : f->tsu_v;
        double *mine_t = up ? f->tsu_t : f->tsd_t, *their_t = up ? f->tsd_t : f->tsu_t;
        double *their_gap = up ? &f->ts_gap_dn : &f->ts_gap_up;
        /* 1. does this packet echo a stamp the other side sent? (up: device echoes server = LAN RTT) */
        for (int i = 0; i < TS_SLOTS; i++) {
            if (!their_v[i]) continue;
            if (now - their_t[i] > TS_MAX_AGE) { their_v[i] = 0; continue; }
            if (their_v[i] != tsecr) continue;
            double t0 = their_t[i], ms = (now - t0) * 1000.0;
            if (up) { w->rl_n++; w->rl_s += ms; if (w->rl_n == 1 || ms < w->rl_min) w->rl_min = ms; if (ms > w->rl_max) w->rl_max = ms; }
            else    { w->rw_n++; w->rw_s += ms; if (w->rw_n == 1 || ms < w->rw_min) w->rw_min = ms; if (ms > w->rw_max) w->rw_max = ms; }
            for (int j = 0; j < TS_SLOTS; j++) if (their_v[j] && their_t[j] <= t0) their_v[j] = 0;   /* and older ones */
            double g = ms / 1000.0 / TS_SLOTS;              /* space samples so the slots span two round trips */
            *their_gap = g < TS_GAP_MIN ? TS_GAP_MIN : g > TS_GAP_MAX ? TS_GAP_MAX : g;
            break;
        }
        /* 2. remember the first packet carrying a new stamp of this side, at most one per gap */
        uint32_t *last = up ? &f->last_tsv_up : &f->last_tsv_dn;
        double *next = up ? &f->ts_next_up : &f->ts_next_dn, *gap = up ? &f->ts_gap_up : &f->ts_gap_dn;
        if (*gap == 0) *gap = TS_GAP_MIN;
        if (tsval != *last && now >= *next) {
            int slot = -1, oldest = 0;
            for (int i = 0; i < TS_SLOTS; i++) {
                if (!mine_v[i]) { slot = i; break; }
                if (mine_t[i] < mine_t[oldest]) oldest = i;
            }
            if (slot < 0) {                                 /* all slots busy: the RTT is longer than they span */
                slot = oldest;
                *gap = *gap * 2 > TS_GAP_MAX ? TS_GAP_MAX : *gap * 2;
            }
            mine_v[slot] = tsval; mine_t[slot] = now;
            *last = tsval; *next = now + *gap;
        }
    }
    if (spin >= 0 && !up) {                               /* QUIC spin bit edges, server -> device */
        if (f->spin_seen && spin != f->spin_last) {
            double ms = (now - f->spin_t) * 1000.0;
            if (ms >= 1.0 && ms <= 2000.0) { w->sp_n++; w->sp_s += ms; }
        }
        if (!f->spin_seen || spin != f->spin_last) { f->spin_t = now; f->spin_last = (uint8_t)spin; f->spin_seen = 1; }
    }
    if (f->early_n < EARLY_N) {
        f->e_t[f->early_n] = (float)(now - f->first); f->e_size[f->early_n] = (uint16_t)(sz > 65535 ? 65535 : sz);
        f->e_dir[f->early_n] = (uint8_t)up; f->early_n++;
    }
    if (flags & 0x02) f->syn = 1;
    if (flags & 0x05) f->fin = 1;
}

static void addr_str(int fam, const uint8_t *a, char *out) { inet_ntop(fam == 4 ? AF_INET : AF_INET6, a, out, INET6_ADDRSTRLEN); }

static void emit_flow(FILE *o, struct flow *f, const char *la, const char *ra) {
    struct win *w = &f->w;
    fprintf(o, "W %u %s %s %u %u %u %.6f %.6f %llu %llu %llu %llu "
               "%llu %llu %llu %llu %llu %llu %llu %llu %llu %llu %llu %.9g %.9g %llu %.9g %.9g %u %u",
            f->k.fam, la, ra, f->k.proto, f->k.lport, f->k.rport, f->first, f->last,
            (unsigned long long)f->pk_up, (unsigned long long)f->pk_dn, (unsigned long long)f->by_up, (unsigned long long)f->by_dn,
            (unsigned long long)w->pk_up, (unsigned long long)w->by_up, (unsigned long long)w->sq_up, (unsigned long long)w->mx_up,
            (unsigned long long)w->pk_dn, (unsigned long long)w->by_dn, (unsigned long long)w->sq_dn, (unsigned long long)w->mx_dn,
            (unsigned long long)w->small, (unsigned long long)w->large,
            (unsigned long long)w->iat_n_up, w->iat_s_up, w->iat_q_up, (unsigned long long)w->iat_n_dn, w->iat_s_dn, w->iat_q_dn,
            f->syn, f->fin);
    fprintf(o, " %llu %llu %llu %u %llu %llu %llu %.3f %.3f %.3f %llu %.3f %.3f %.3f %llu %llu %llu %.3f",
            (unsigned long long)w->ect0, (unsigned long long)w->ect1, (unsigned long long)w->ce, w->dscp_up,
            (unsigned long long)w->retx_up, (unsigned long long)w->retx_dn,
            (unsigned long long)w->rw_n, w->rw_s, w->rw_min, w->rw_max,
            (unsigned long long)w->rl_n, w->rl_s, w->rl_min, w->rl_max,
            (unsigned long long)w->fr_up, (unsigned long long)w->fr_dn, (unsigned long long)w->sp_n, w->sp_s);
    if (f->early_n != f->early_sent) {
        fprintf(o, " %u", f->early_n);
        for (int j = 0; j < f->early_n; j++) fprintf(o, " %.6f %u %u", f->e_t[j], f->e_size[j], f->e_dir[j]);
        f->early_sent = f->early_n;
    } else fputs(" -", o);
    fputc('\n', o);
    memset(w, 0, sizeof *w);
    w->dscp_up = 255;
}

static void roll(FILE *o, double now) {
    char la[INET6_ADDRSTRLEN], ra[INET6_ADDRSTRLEN];
    for (int i = 0; i < TABLE_SIZE; i++) {
        struct flow *f = &tab[i];
        if (f->used != 1) continue;
        addr_str(f->k.fam, f->k.lan, la); addr_str(f->k.fam, f->k.rem, ra);
        emit_flow(o, f, la, ra);
        double idle = now - f->last;
        if (idle > idle_timeout || (f->fin && idle > 3.0)) {
            fprintf(o, "X %u %s %s %u %u %u\n", f->k.fam, la, ra, f->k.proto, f->k.lport, f->k.rport);
            f->used = 2; nflows--; ntomb++;
        }
    }
    if (ntomb > TABLE_SIZE / 4) rebuild();
    fprintf(o, "E %llu %llu %llu %llu %d %llu\n", (unsigned long long)packets, (unsigned long long)bytes_total,
            (unsigned long long)kpk, (unsigned long long)kdrop, nflows, (unsigned long long)overflow);
    fflush(o);
}

/* ------------------------------------------------------------------------------------ kernel flow engine */
static const char *bpf_obj, *wan_if, *lan_if;
static int fd_flows = -1, fd_verdict = -1, fd_cfg = -1, fd_stats = -1;
static struct iq_cfg kcfg;
static uint32_t rolls;
static int ncpu;                                        /* possible CPUs (per-CPU map values) */
#define PIN_DIR "/sys/fs/bpf/tc/globals"
static const char *const PINS[] = { "iq_flows", "iq_verdict", "iq_cfg", "iq_stats", "iq_zero" };

static long sys_bpf(int cmd, union bpf_attr *a) { return syscall(__NR_bpf, cmd, a, sizeof *a); }
static int bpf_get(const char *path) {
    union bpf_attr a; memset(&a, 0, sizeof a); a.pathname = (uint64_t)(uintptr_t)path;
    return (int)sys_bpf(BPF_OBJ_GET, &a);
}
static int bpf_elem(int cmd, int fd, const void *k, void *v, uint64_t flags) {
    union bpf_attr a; memset(&a, 0, sizeof a);
    a.map_fd = (uint32_t)fd; a.key = (uint64_t)(uintptr_t)k; a.value = (uint64_t)(uintptr_t)v; a.flags = flags;
    return (int)sys_bpf(cmd, &a);
}
static int bpf_next(int fd, const void *k, void *next) {
    union bpf_attr a; memset(&a, 0, sizeof a);
    a.map_fd = (uint32_t)fd; a.key = (uint64_t)(uintptr_t)k; a.next_key = (uint64_t)(uintptr_t)next;
    return (int)sys_bpf(BPF_MAP_GET_NEXT_KEY, &a);
}

static int run_tc(const char *const *argv) {          /* argv[0] = "tc", NULL-terminated; no shell */
    pid_t p = fork();
    if (p < 0) return -1;
    if (p == 0) {
        int dn = open("/dev/null", O_WRONLY); if (dn >= 0) { dup2(dn, 1); dup2(dn, 2); }
        execvp("tc", (char *const *)argv);
        _exit(127);
    }
    int st; if (waitpid(p, &st, 0) < 0) return -1;
    return WIFEXITED(st) ? WEXITSTATUS(st) : -1;
}

static char ports[16][IF_NAMESIZE];
static int nports;

static void find_ports(void) {                         /* members of a LAN bridge, if lan_if is one */
    char path[128]; snprintf(path, sizeof path, "/sys/class/net/%s/brif", lan_if);
    nports = 0;
    DIR *d = opendir(path); if (!d) return;
    struct dirent *e;
    while ((e = readdir(d)) && nports < 16) {
        if (e->d_name[0] == '.' || strlen(e->d_name) >= IF_NAMESIZE) continue;
        memcpy(ports[nports++], e->d_name, strlen(e->d_name) + 1);
    }
    closedir(d);
}

static void kernel_detach(void) {
    if (!bpf_obj) return;
    const char *a[] = { "tc", "filter", "del", "dev", lan_if, "ingress", "prio", "7", NULL };
    run_tc(a);
    a[5] = "egress"; run_tc(a);
    for (int i = 0; i < nports; i++) { a[4] = ports[i]; a[5] = "ingress"; run_tc(a); a[5] = "egress"; run_tc(a); }
    if (wan_if) { a[4] = wan_if; a[5] = "egress"; run_tc(a); }
    char path[128];
    for (size_t i = 0; i < sizeof PINS / sizeof *PINS; i++) { snprintf(path, sizeof path, PIN_DIR "/%s", PINS[i]); unlink(path); }
}

static int kernel_write_cfg(void) { uint32_t z = 0; return bpf_elem(BPF_MAP_UPDATE_ELEM, fd_cfg, &z, &kcfg, BPF_ANY); }

static int set_net(int i4, int i6) {
    for (int i = 0; i < nnets; i++) {
        uint8_t m[16] = {0};
        for (int b = 0; b < nets[i].plen; b++) m[b / 8] |= (uint8_t)(0x80 >> (b % 8));
        if (nets[i].fam == 4 && i4 < 4) {
            uint32_t a4, m4; memcpy(&a4, nets[i].addr, 4); memcpy(&m4, m, 4);
            kcfg.net4[i4][0] = a4 & m4; kcfg.net4[i4][1] = m4; i4++;
        } else if (nets[i].fam == 6 && i6 < 4) {
            uint8_t a[16]; for (int j = 0; j < 16; j++) a[j] = nets[i].addr[j] & m[j];
            memcpy(&kcfg.net6[i6][0], a, 8); memcpy(&kcfg.net6[i6][1], a + 8, 8);
            memcpy(&kcfg.net6[i6][2], m, 8); memcpy(&kcfg.net6[i6][3], m + 8, 8); i6++;
        }
    }
    kcfg.n4 = (uint32_t)i4; kcfg.n6 = (uint32_t)i6;
    return 0;
}

static int kernel_attach(void) {
    struct statfs sf;
    if (statfs("/sys/fs/bpf", &sf) != 0 || (unsigned long)sf.f_type != 0xcafe4a11UL)
        if (mount("bpf", "/sys/fs/bpf", "bpf", 0, NULL) != 0) { perror("mount bpffs"); return -1; }
    find_ports();
    kernel_detach();                                    /* stale filters and pins from an earlier run */
    const char *q[] = { "tc", "qdisc", "add", "dev", lan_if, "clsact", NULL };
    if (run_tc(q) == 127) { fprintf(stderr, "tc not found\n"); return -1; }
    const char *f[] = { "tc", "filter", "replace", "dev", lan_if, "ingress", "prio", "7", "handle", "1", "bpf", "da",
                        "obj", bpf_obj, "sec", "tc_up", NULL };
    if (nports) {                                       /* bridge: mark before its shaper, account on the ports */
        f[5] = "egress"; f[15] = "tc_mark";
        if (run_tc(f)) { fprintf(stderr, "could not attach %s on %s egress (tc needs BPF support built with libbpf)\n", bpf_obj, lan_if); return -1; }
        for (int i = 0; i < nports; i++) {
            q[4] = ports[i]; run_tc(q);
            f[4] = ports[i]; f[5] = "ingress"; f[15] = "tc_up";
            if (run_tc(f)) { fprintf(stderr, "could not attach on %s ingress\n", ports[i]); kernel_detach(); return -1; }
            f[5] = "egress"; f[15] = "tc_down_acct";
            if (run_tc(f)) { fprintf(stderr, "could not attach on %s egress\n", ports[i]); kernel_detach(); return -1; }
        }
    } else {
        if (run_tc(f)) { fprintf(stderr, "could not attach %s on %s ingress (tc needs BPF support built with libbpf)\n", bpf_obj, lan_if); return -1; }
        f[5] = "egress"; f[15] = "tc_down";
        if (run_tc(f)) { fprintf(stderr, "could not attach %s on %s egress\n", bpf_obj, lan_if); kernel_detach(); return -1; }
    }
    fprintf(stderr, "kernel flow engine on %s%s%s\n", lan_if, nports ? " (bridge ports: accounting after the shaper)" :
            " (single interface: LAN round trips include this interface's queue)", wan_if ? " + WAN marks" : "");
    if (wan_if) {
        q[4] = wan_if; run_tc(q);
        f[4] = wan_if; f[5] = "egress"; f[15] = "tc_wan";
        if (run_tc(f)) { fprintf(stderr, "could not attach %s on %s egress\n", bpf_obj, wan_if); kernel_detach(); return -1; }
    }
    fd_flows = bpf_get(PIN_DIR "/iq_flows"); fd_verdict = bpf_get(PIN_DIR "/iq_verdict");
    fd_cfg = bpf_get(PIN_DIR "/iq_cfg"); fd_stats = bpf_get(PIN_DIR "/iq_stats");
    if (fd_flows < 0 || fd_verdict < 0 || fd_cfg < 0 || fd_stats < 0) {
        fprintf(stderr, "pinned maps not found in " PIN_DIR ": %s\n", strerror(errno)); kernel_detach(); return -1;
    }
    FILE *pf = fopen("/sys/devices/system/cpu/possible", "r");
    if (pf) { int a = 0, b = 0; char sep = 0; int n = fscanf(pf, "%d%c%d", &a, &sep, &b); ncpu = n == 3 && sep == '-' ? b + 1 : a + 1; fclose(pf); }
    memset(&kcfg, 0, sizeof kcfg);
    set_net(0, 0);
    for (int t = 0; t < 8; t++) kcfg.dscp[t] = 255;
    if (wan_if) {
        char path[96]; snprintf(path, sizeof path, "/sys/class/net/%s/type", wan_if);
        FILE *tf = fopen(path, "r"); int type = 1;
        if (tf) { if (fscanf(tf, "%d", &type) != 1) type = 1; fclose(tf); }
        kcfg.wan_l2 = type == 1;                        /* ARPHRD_ETHER; PPPoE/WireGuard have no L2 header */
    }
    if (kernel_write_cfg()) { perror("write iq_cfg"); kernel_detach(); return -1; }
    return 0;
}

static void kernel_roll(FILE *o, double now) {
    kcfg.gen++;                                         /* new packets go to the other half from now on */
    kernel_write_cfg();
    struct timespec pause = { 0, 2000000 }; nanosleep(&pause, NULL);   /* let in-flight programs finish */
    uint32_t old = kcfg.gen - 1;
    rolls++;
    static struct iq_key dead[IQ_MAX_FLOWS];
    int ndead = 0;
    struct iq_key cur, next; int first = 1;
    char la[INET6_ADDRSTRLEN], ra[INET6_ADDRSTRLEN];
    static struct iq_val v;
    while (bpf_next(fd_flows, first ? NULL : &cur, &next) == 0) {
        first = 0; cur = next;
        if (bpf_elem(BPF_MAP_LOOKUP_ELEM, fd_flows, &cur, &v, 0)) continue;
        struct key k; memcpy(&k, &cur, sizeof k);
        struct flow *f = lookup(&k, now);
        if (!f) continue;
        f->seen = rolls;
        f->first = v.first / 1e9; f->last = v.last / 1e9;
        f->pk_up = v.pk_up; f->pk_dn = v.pk_dn; f->by_up = v.by_up; f->by_dn = v.by_dn;
        f->syn = v.syn; f->fin = v.fin;
        f->early_n = v.early_n > EARLY_N ? EARLY_N : v.early_n;
        for (int j = 0; j < f->early_n; j++) { f->e_t[j] = v.e_t[j] / 1e6f; f->e_size[j] = v.e_size[j]; f->e_dir[j] = v.e_dir[j]; }
        struct iq_half *h = &v.w[old & 1];
        struct win *w = &f->w;
        memset(w, 0, sizeof *w); w->dscp_up = 255;
        if (h->gen == old) {
            w->pk_up = h->pk_up; w->by_up = h->by_up; w->sq_up = h->sq_up; w->mx_up = h->mx_up;
            w->pk_dn = h->pk_dn; w->by_dn = h->by_dn; w->sq_dn = h->sq_dn; w->mx_dn = h->mx_dn;
            w->small = h->small; w->large = h->large;
            w->iat_n_up = h->iat_n_up; w->iat_s_up = h->iat_s_up / 1e6; w->iat_q_up = h->iat_q_up / 1e12;
            w->iat_n_dn = h->iat_n_dn; w->iat_s_dn = h->iat_s_dn / 1e6; w->iat_q_dn = h->iat_q_dn / 1e12;
            w->ect0 = h->ect0; w->ect1 = h->ect1; w->ce = h->ce; w->dscp_up = h->dscp_up;
            w->retx_up = h->retx_up; w->retx_dn = h->retx_dn; w->fr_up = h->fr_up; w->fr_dn = h->fr_dn;
            w->rw_n = h->rw_n; w->rw_s = h->rw_s / 1e3; w->rw_min = h->rw_min / 1e3; w->rw_max = h->rw_max / 1e3;
            w->rl_n = h->rl_n; w->rl_s = h->rl_s / 1e3; w->rl_min = h->rl_min / 1e3; w->rl_max = h->rl_max / 1e3;
            w->sp_n = h->sp_n; w->sp_s = h->sp_s / 1e3;
        }
        addr_str(f->k.fam, f->k.lan, la); addr_str(f->k.fam, f->k.rem, ra);
        emit_flow(o, f, la, ra);
        double idle = now - f->last;
        if (idle > idle_timeout || (f->fin && idle > 3.0)) {
            fprintf(o, "X %u %s %s %u %u %u\n", f->k.fam, la, ra, f->k.proto, f->k.lport, f->k.rport);
            f->used = 2; nflows--; ntomb++;
            if (ndead < IQ_MAX_FLOWS) dead[ndead++] = cur;
        }
    }
    for (int i = 0; i < ndead; i++) {                   /* delete after the walk: deleting resets the iteration */
        bpf_elem(BPF_MAP_DELETE_ELEM, fd_flows, &dead[i], NULL, 0);
        bpf_elem(BPF_MAP_DELETE_ELEM, fd_verdict, &dead[i], NULL, 0);
    }
    for (int i = 0; i < TABLE_SIZE; i++) {             /* evicted by the kernel's LRU: tell the engine */
        struct flow *f = &tab[i];
        if (f->used != 1 || f->seen == rolls) continue;
        addr_str(f->k.fam, f->k.lan, la); addr_str(f->k.fam, f->k.rem, ra);
        fprintf(o, "X %u %s %s %u %u %u\n", f->k.fam, la, ra, f->k.proto, f->k.lport, f->k.rport);
        f->used = 2; nflows--; ntomb++;
    }
    if (ntomb > TABLE_SIZE / 4) rebuild();
    uint32_t z = 0;
    static struct iq_stats pst[512];                    /* per-CPU array: one value per possible CPU */
    if (ncpu > 0 && ncpu <= 512 && bpf_elem(BPF_MAP_LOOKUP_ELEM, fd_stats, &z, pst, 0) == 0) {
        packets = bytes_total = kdrop = 0;
        for (int c = 0; c < ncpu; c++) { packets += pst[c].packets; bytes_total += pst[c].bytes; kdrop += pst[c].insert_failed; }
        kpk = packets;
    }
    fprintf(o, "E %llu %llu %llu %llu %d %llu\n", (unsigned long long)packets, (unsigned long long)bytes_total,
            (unsigned long long)kpk, (unsigned long long)kdrop, nflows, (unsigned long long)overflow);
    fflush(o);
}

static void kernel_command(const char *line) {
    if (line[0] == 'M') {
        unsigned fam, proto, lp, rp, tin; char la[64], ra[64];
        if (sscanf(line + 1, "%u %63s %63s %u %u %u %u", &fam, la, ra, &proto, &lp, &rp, &tin) != 7) return;
        struct iq_key k; memset(&k, 0, sizeof k);
        k.fam = (uint8_t)fam; k.proto = (uint8_t)proto; k.lport = (uint16_t)lp; k.rport = (uint16_t)rp;
        int af = fam == 4 ? AF_INET : AF_INET6;
        if (inet_pton(af, la, k.lan) != 1 || inet_pton(af, ra, k.rem) != 1) return;
        if (tin >= IQ_TINS) bpf_elem(BPF_MAP_DELETE_ELEM, fd_verdict, &k, NULL, 0);
        else { uint32_t t = tin; bpf_elem(BPF_MAP_UPDATE_ELEM, fd_verdict, &k, &t, BPF_ANY); }
    } else if (line[0] == 'C') {                        /* clear every verdict (engine paused or bypassed) */
        struct iq_key k, n;
        while (bpf_next(fd_verdict, NULL, &n) == 0) { k = n; if (bpf_elem(BPF_MAP_DELETE_ELEM, fd_verdict, &k, NULL, 0)) break; }
    } else if (line[0] == 'P') {
        unsigned md, pr[IQ_TINS], ds[IQ_TINS];
        if (sscanf(line + 1, "%u %u %u %u %u %u %u %u %u %u %u", &md, &pr[0], &ds[0], &pr[1], &ds[1], &pr[2], &ds[2],
                   &pr[3], &ds[3], &pr[4], &ds[4]) != 11) return;
        kcfg.mark_dscp = md;
        for (int t = 0; t < IQ_TINS; t++) { kcfg.prio[t] = pr[t]; kcfg.dscp[t] = (uint8_t)(ds[t] > 63 ? 255 : ds[t]); }
        kernel_write_cfg();
    }
}

static int kernel_main(void) {
    if (kernel_attach()) return 3;
    struct pollfd pf = { .fd = 0, .events = POLLIN };
    char line[256];
    while (!stop) {
        int r = poll(&pf, 1, 200);
        if (r < 0) { if (errno == EINTR) continue; break; }
        if (r == 0) continue;
        if (pf.revents & (POLLHUP | POLLERR) && !(pf.revents & POLLIN)) break;
        if (!fgets(line, sizeof line, stdin)) break;
        if (line[0] == 'R') kernel_roll(stdout, mono());
        else if (line[0] == 'M' || line[0] == 'P' || line[0] == 'C') kernel_command(line);
        else if (line[0] == 'Q') break;
    }
    kernel_detach();
    return 0;
}

static void on_sig(int s) { (void)s; stop = 1; }

int main(int argc, char **argv) {
    const char *iface = NULL; int opt;
    while ((opt = getopt(argc, argv, "i:n:t:e:w:")) != -1) {
        if (opt == 'i') iface = optarg;
        else if (opt == 'e') bpf_obj = optarg;
        else if (opt == 'w') wan_if = optarg;
        else if (opt == 'n') { if (nnets >= 32 || parse_net(optarg, &nets[nnets]) < 0) { fprintf(stderr, "bad subnet %s\n", optarg); return 2; } nnets++; }
        else if (opt == 't') idle_timeout = atof(optarg);
        else { fprintf(stderr, "usage: %s -i IFACE -n SUBNET [-n SUBNET...] [-t IDLE_S] [-e BPF_OBJ [-w WAN]]\n", argv[0]); return 2; }
    }
    if (!iface || !nnets) { fprintf(stderr, "usage: %s -i IFACE -n SUBNET [-n SUBNET...]\n", argv[0]); return 2; }
    signal(SIGTERM, on_sig); signal(SIGINT, on_sig); signal(SIGPIPE, on_sig);
    tab = calloc(TABLE_SIZE, sizeof *tab);
    if (!tab) { perror("calloc"); return 1; }
    if (bpf_obj) { lan_if = iface; return kernel_main(); }

    int fd = socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL));
    if (fd < 0) { perror("socket"); return 1; }
    struct sock_filter code[] = { BPF_STMT(BPF_RET | BPF_K, SNAPLEN) };   /* kernel-side truncation */
    struct sock_fprog prog = { 1, code };
    if (setsockopt(fd, SOL_SOCKET, SO_ATTACH_FILTER, &prog, sizeof prog) < 0) { perror("SO_ATTACH_FILTER"); return 1; }
    int ver = TPACKET_V3;
    if (setsockopt(fd, SOL_PACKET, PACKET_VERSION, &ver, sizeof ver) < 0) { perror("PACKET_VERSION"); return 1; }
    struct tpacket_req3 req; memset(&req, 0, sizeof req);
    req.tp_block_size = RING_BLOCK; req.tp_block_nr = BLOCK_NR; req.tp_frame_size = FRAME_SIZE;
    req.tp_frame_nr = (RING_BLOCK * BLOCK_NR) / FRAME_SIZE; req.tp_retire_blk_tov = 50;
    if (setsockopt(fd, SOL_PACKET, PACKET_RX_RING, &req, sizeof req) < 0) { perror("PACKET_RX_RING"); return 1; }
    uint8_t *ring = mmap(NULL, (size_t)RING_BLOCK * BLOCK_NR, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_LOCKED, fd, 0);
    if (ring == MAP_FAILED) ring = mmap(NULL, (size_t)RING_BLOCK * BLOCK_NR, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (ring == MAP_FAILED) { perror("mmap"); return 1; }
    struct sockaddr_ll ll; memset(&ll, 0, sizeof ll);
    ll.sll_family = AF_PACKET; ll.sll_protocol = htons(ETH_P_ALL); ll.sll_ifindex = (int)if_nametoindex(iface);
    if (!ll.sll_ifindex) { fprintf(stderr, "no such interface %s\n", iface); return 1; }
    if (bind(fd, (struct sockaddr *)&ll, sizeof ll) < 0) { perror("bind"); return 1; }

    struct pollfd pf[2] = { { .fd = fd, .events = POLLIN | POLLERR }, { .fd = 0, .events = POLLIN } };
    unsigned blk = 0; char line[64];
    while (!stop) {
        struct tpacket_block_desc *bd = (struct tpacket_block_desc *)(ring + (size_t)blk * RING_BLOCK);
        if (!(bd->hdr.bh1.block_status & TP_STATUS_USER)) {
            if (poll(pf, 2, 100) < 0 && errno != EINTR) break;
            if (pf[1].revents & (POLLHUP | POLLERR)) break;
            if (pf[1].revents & POLLIN) {
                if (!fgets(line, sizeof line, stdin)) break;
                if (line[0] == 'R') {
                    struct tpacket_stats_v3 st; socklen_t l = sizeof st;
                    if (getsockopt(fd, SOL_PACKET, PACKET_STATISTICS, &st, &l) == 0) { kpk += st.tp_packets; kdrop += st.tp_drops; }
                    roll(stdout, mono());
                } else if (line[0] == 'Q') break;
            }
            continue;
        }
        double off = mono() - realt();                 /* packet timestamps are wall clock */
        struct tpacket3_hdr *ph = (struct tpacket3_hdr *)((uint8_t *)bd + bd->hdr.bh1.offset_to_first_pkt);
        for (unsigned i = 0; i < bd->hdr.bh1.num_pkts; i++) {
            double ts = ph->tp_sec + ph->tp_nsec / 1e9 + off;
            account((uint8_t *)ph + ph->tp_mac, ph->tp_snaplen, ph->tp_len, ts);
            ph = (struct tpacket3_hdr *)((uint8_t *)ph + ph->tp_next_offset);
        }
        bd->hdr.bh1.block_status = TP_STATUS_KERNEL;
        blk = (blk + 1) % BLOCK_NR;
    }
    munmap(ring, (size_t)RING_BLOCK * BLOCK_NR);
    close(fd);
    return 0;
}
