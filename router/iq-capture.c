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
 * Protocol on stdin/stdout (driven by intentqos/capture.py: HelperCapture):
 *   engine -> helper   "R\n"                      close the current 0.5 s window
 *   helper -> engine   "W <flow fields...>\n"    one line per live flow (its closed window)
 *                      "X <fam> <lan> <remote> <proto> <lport> <rport>\n"  flow expired
 *                      "E <packets> <bytes> <kernel_packets> <kernel_drops> <flows> <overflow>\n"
 *
 * Build:  cc -O2 -Wall -o iq-capture iq-capture.c                       (host)
 *         aarch64-linux-musl-gcc -O2 -static -o iq-capture iq-capture.c (router, static)
 * Usage:  iq-capture -i br-lan -n 192.168.1.0/24 -n fd00::/64 [-t 30]
 * Licence: MIT (same as IntentQoS).
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
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
#include <sys/mman.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#define SNAPLEN 96
#define EARLY_N 16
#define TABLE_SIZE 8192            /* power of two */
#define MAX_FLOWS 4096
#define BLOCK_SIZE (1 << 18)       /* 256 KiB */
#define BLOCK_NR 8                 /* 2 MiB ring */
#define FRAME_SIZE 2048

struct key {
    uint8_t fam, proto;
    uint16_t lport, rport;
    uint8_t lan[16], rem[16];
};

struct win {
    uint64_t pk_up, by_up, sq_up, mx_up, pk_dn, by_dn, sq_dn, mx_dn, small, large, iat_n_up, iat_n_dn;
    double iat_s_up, iat_q_up, iat_s_dn, iat_q_dn;
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
    f->used = 1; f->k = *k; f->first = f->last = now;
    nflows++;
    return f;
}

static void account(const uint8_t *b, unsigned caplen, unsigned wirelen, double now) {
    (void)wirelen;
    if (caplen < 14) return;
    unsigned off = 14, etype = (b[12] << 8) | b[13];
    while ((etype == 0x8100 || etype == 0x88A8) && caplen >= off + 4) { etype = (b[off + 2] << 8) | b[off + 3]; off += 4; }
    uint8_t src[16] = {0}, dst[16] = {0}; int fam; unsigned proto, l4, iplen; int frag = 0;
    if (etype == 0x0800) {
        if (caplen < off + 20) return;
        unsigned ihl = (b[off] & 0x0F) * 4;
        iplen = (b[off + 2] << 8) | b[off + 3];
        frag = ((b[off + 6] & 0x1F) << 8) | b[off + 7];
        proto = b[off + 9];
        memcpy(src, b + off + 12, 4); memcpy(dst, b + off + 16, 4);
        l4 = off + ihl; fam = 4;
    } else if (etype == 0x86DD) {
        if (caplen < off + 40) return;
        iplen = ((b[off + 4] << 8) | b[off + 5]) + 40;
        proto = b[off + 6];
        memcpy(src, b + off + 8, 16); memcpy(dst, b + off + 24, 16);
        l4 = off + 40; fam = 6;
        for (int hops = 0; (proto == 0 || proto == 43 || proto == 60) && caplen >= l4 + 2 && hops < 4; hops++) {
            proto = b[l4]; l4 += (b[l4 + 1] + 1) * 8;
        }
    } else return;
    unsigned sport = 0, dport = 0, flags = 0;
    if (!frag && (proto == 6 || proto == 17) && caplen >= l4 + 4) {
        sport = (b[l4] << 8) | b[l4 + 1]; dport = (b[l4 + 2] << 8) | b[l4 + 3];
        if (proto == 6 && caplen >= l4 + 14) flags = b[l4 + 13];
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
    if (f->early_n < EARLY_N) {
        f->e_t[f->early_n] = (float)(now - f->first); f->e_size[f->early_n] = (uint16_t)(sz > 65535 ? 65535 : sz);
        f->e_dir[f->early_n] = (uint8_t)up; f->early_n++;
    }
    if (flags & 0x02) f->syn = 1;
    if (flags & 0x05) f->fin = 1;
}

static void addr_str(int fam, const uint8_t *a, char *out) { inet_ntop(fam == 4 ? AF_INET : AF_INET6, a, out, INET6_ADDRSTRLEN); }

static void roll(FILE *o, double now) {
    char la[INET6_ADDRSTRLEN], ra[INET6_ADDRSTRLEN];
    for (int i = 0; i < TABLE_SIZE; i++) {
        struct flow *f = &tab[i];
        if (f->used != 1) continue;
        addr_str(f->k.fam, f->k.lan, la); addr_str(f->k.fam, f->k.rem, ra);
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
        if (f->early_n != f->early_sent) {
            fprintf(o, " %u", f->early_n);
            for (int j = 0; j < f->early_n; j++) fprintf(o, " %.6f %u %u", f->e_t[j], f->e_size[j], f->e_dir[j]);
            f->early_sent = f->early_n;
        } else fputs(" -", o);
        fputc('\n', o);
        memset(w, 0, sizeof *w);
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

static void on_sig(int s) { (void)s; stop = 1; }

int main(int argc, char **argv) {
    const char *iface = NULL; int opt;
    while ((opt = getopt(argc, argv, "i:n:t:")) != -1) {
        if (opt == 'i') iface = optarg;
        else if (opt == 'n') { if (nnets >= 32 || parse_net(optarg, &nets[nnets]) < 0) { fprintf(stderr, "bad subnet %s\n", optarg); return 2; } nnets++; }
        else if (opt == 't') idle_timeout = atof(optarg);
        else { fprintf(stderr, "usage: %s -i IFACE -n SUBNET [-n SUBNET...] [-t IDLE_S]\n", argv[0]); return 2; }
    }
    if (!iface || !nnets) { fprintf(stderr, "usage: %s -i IFACE -n SUBNET [-n SUBNET...]\n", argv[0]); return 2; }
    signal(SIGTERM, on_sig); signal(SIGINT, on_sig); signal(SIGPIPE, on_sig);
    tab = calloc(TABLE_SIZE, sizeof *tab);
    if (!tab) { perror("calloc"); return 1; }

    int fd = socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL));
    if (fd < 0) { perror("socket"); return 1; }
    struct sock_filter code[] = { BPF_STMT(BPF_RET | BPF_K, SNAPLEN) };   /* kernel-side truncation */
    struct sock_fprog prog = { 1, code };
    if (setsockopt(fd, SOL_SOCKET, SO_ATTACH_FILTER, &prog, sizeof prog) < 0) { perror("SO_ATTACH_FILTER"); return 1; }
    int ver = TPACKET_V3;
    if (setsockopt(fd, SOL_PACKET, PACKET_VERSION, &ver, sizeof ver) < 0) { perror("PACKET_VERSION"); return 1; }
    struct tpacket_req3 req; memset(&req, 0, sizeof req);
    req.tp_block_size = BLOCK_SIZE; req.tp_block_nr = BLOCK_NR; req.tp_frame_size = FRAME_SIZE;
    req.tp_frame_nr = (BLOCK_SIZE * BLOCK_NR) / FRAME_SIZE; req.tp_retire_blk_tov = 50;
    if (setsockopt(fd, SOL_PACKET, PACKET_RX_RING, &req, sizeof req) < 0) { perror("PACKET_RX_RING"); return 1; }
    uint8_t *ring = mmap(NULL, (size_t)BLOCK_SIZE * BLOCK_NR, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_LOCKED, fd, 0);
    if (ring == MAP_FAILED) ring = mmap(NULL, (size_t)BLOCK_SIZE * BLOCK_NR, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (ring == MAP_FAILED) { perror("mmap"); return 1; }
    struct sockaddr_ll ll; memset(&ll, 0, sizeof ll);
    ll.sll_family = AF_PACKET; ll.sll_protocol = htons(ETH_P_ALL); ll.sll_ifindex = (int)if_nametoindex(iface);
    if (!ll.sll_ifindex) { fprintf(stderr, "no such interface %s\n", iface); return 1; }
    if (bind(fd, (struct sockaddr *)&ll, sizeof ll) < 0) { perror("bind"); return 1; }

    struct pollfd pf[2] = { { .fd = fd, .events = POLLIN | POLLERR }, { .fd = 0, .events = POLLIN } };
    unsigned blk = 0; char line[64];
    while (!stop) {
        struct tpacket_block_desc *bd = (struct tpacket_block_desc *)(ring + (size_t)blk * BLOCK_SIZE);
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
    munmap(ring, (size_t)BLOCK_SIZE * BLOCK_NR);
    close(fd);
    return 0;
}
