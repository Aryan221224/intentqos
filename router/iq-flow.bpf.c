// SPDX-License-Identifier: MIT OR GPL-2.0
/*
 * iq-flow.bpf.c -- IntentQoS kernel flow engine (tc / cls_bpf, direct action).
 *
 * The same per-flow aggregates as iq-capture's packet loop, computed in the kernel as packets pass the
 * LAN interface, so no packet (not even a 96-byte header) is copied to user space. User space reads
 * the flow map once per window. The program also applies the engine's decisions in the same pass:
 * a flow with a verdict gets skb->mark (0x51000 + tin), skb->priority (the shaper class) and, if
 * enabled, a DSCP rewrite (which also drives the Wi-Fi access category through the QoS Map Set).
 *
 *   section tc_up        LAN ingress (device -> internet): account as upload, set the mark
 *   section tc_down      LAN egress  (internet -> device): account as download, mark + priority + DSCP
 *   section tc_mark      bridge egress, before the download shaper: mark + priority + DSCP only
 *   section tc_down_acct bridge member port egress, after the shaper: account as download only
 *   section tc_wan       WAN egress: priority + DSCP from the mark set at LAN ingress (upload shaper)
 *
 * On a plain LAN interface tc_down runs before that interface's shaper, so its download timestamps are
 * taken before the router's own download queue: the LAN round trip then includes that queue. With a LAN
 * bridge (the usual router layout) iq-capture attaches tc_mark to the bridge and tc_up/tc_down_acct to its
 * member ports, which avoids this.
 *
 * Maps are BTF-defined and pinned by name (tc pins them in /sys/fs/bpf/tc/globals), so the three
 * programs and iq-capture share them. No libbpf headers are needed to build: clang and the kernel UAPI
 * headers only. Loading needs tc built with libbpf (iproute2 5.13+; OpenWrt package tc-bpf or tc-full).
 *
 * Build:  clang -O2 -g -target bpfel -c iq-flow.bpf.c -o iq-flow.bpf.o   (little-endian routers: ARM, x86, mipsel)
 *         clang -O2 -g -target bpfeb -c iq-flow.bpf.c -o iq-flow-eb.bpf.o (big-endian MIPS)
 * Needs:  Linux 5.3+ (bounded loops), cls_bpf and clsact; tc built with BPF support (OpenWrt: tc-full or tc-bpf).
 * Licence: MIT OR GPL-2.0 (dual). The kernel treats "Dual MIT/GPL" as GPL-compatible; no GPL-only helper is used.
 */
#include <linux/bpf.h>
#include <linux/pkt_cls.h>
#include "iq-flow.h"

#define SEC(n) __attribute__((section(n), used))
#define INLINE static inline __attribute__((always_inline))

static void *(*map_lookup)(void *map, const void *key) = (void *)BPF_FUNC_map_lookup_elem;
static long (*map_update)(void *map, const void *key, const void *val, __u64 flags) = (void *)BPF_FUNC_map_update_elem;
static __u64 (*ktime_ns)(void) = (void *)BPF_FUNC_ktime_get_ns;
static long (*load_bytes)(const struct __sk_buff *skb, __u32 off, void *to, __u32 len) = (void *)BPF_FUNC_skb_load_bytes;
static long (*store_bytes)(struct __sk_buff *skb, __u32 off, const void *from, __u32 len, __u64 flags) =
    (void *)BPF_FUNC_skb_store_bytes;
static long (*l3_csum_replace)(struct __sk_buff *skb, __u32 off, __u64 from, __u64 to, __u64 size) =
    (void *)BPF_FUNC_l3_csum_replace;

/* BTF-defined maps (libbpf style), pinned by name so the three programs and iq-capture share them.
 * The macros are libbpf's, written out here so no libbpf headers are needed to build. */
#define __uint(name, val) int (*name)[val]
#define __type(name, val) typeof(val) *name
#define PIN_BY_NAME 1

struct { __uint(type, BPF_MAP_TYPE_LRU_HASH); __uint(max_entries, IQ_MAX_FLOWS); __type(key, struct iq_key);
         __type(value, struct iq_val); __uint(pinning, PIN_BY_NAME); } iq_flows SEC(".maps");
struct { __uint(type, BPF_MAP_TYPE_HASH); __uint(max_entries, IQ_MAX_FLOWS); __type(key, struct iq_key);
         __type(value, __u32); __uint(pinning, PIN_BY_NAME); } iq_verdict SEC(".maps");
struct { __uint(type, BPF_MAP_TYPE_ARRAY); __uint(max_entries, 1); __type(key, __u32); __type(value, struct iq_cfg);
         __uint(pinning, PIN_BY_NAME); } iq_cfg SEC(".maps");
struct { __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY); __uint(max_entries, 1); __type(key, __u32); __type(value, struct iq_stats);
         __uint(pinning, PIN_BY_NAME); } iq_stats SEC(".maps");     /* per CPU: no shared cache line to fight over */
struct { __uint(type, BPF_MAP_TYPE_ARRAY); __uint(max_entries, 1); __type(key, __u32); __type(value, struct iq_val);
         __uint(pinning, PIN_BY_NAME); } iq_zero SEC(".maps");            /* never written: the new-flow template */
struct { __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY); __uint(max_entries, 1); __type(key, __u32);
         __type(value, struct iq_scratch); } iq_scratch SEC(".maps");

/* Plain adds, not atomic ones: a locked add costs ~20 ns and a packet touches about ten counters. Two CPUs
 * updating one flow at the same instant can lose an increment; packets of one flow are normally handled by
 * one CPU at a time (RSS/RPS hash by flow), and these are statistics, not billing. */
#define ADD(p, n) (*(p) += (n))

INLINE __u16 be16(const __u8 *b) { return (__u16)((b[0] << 8) | b[1]); }
INLINE __u32 be32(const __u8 *b) { return ((__u32)b[0] << 24) | ((__u32)b[1] << 16) | ((__u32)b[2] << 8) | b[3]; }

INLINE int in_lan4(const struct iq_cfg *c, const __u8 *a) {
    __u32 v;
    __builtin_memcpy(&v, a, 4);
#pragma unroll
    for (int i = 0; i < 4; i++)
        if ((__u32)i < c->n4 && (v & c->net4[i][1]) == c->net4[i][0]) return 1;
    return 0;
}

INLINE int in_lan6(const struct iq_cfg *c, const __u8 *a) {
    __u64 hi, lo;
    __builtin_memcpy(&hi, a, 8);
    __builtin_memcpy(&lo, a + 8, 8);
#pragma unroll
    for (int i = 0; i < 4; i++)
        if ((__u32)i < c->n6 && (hi & c->net6[i][2]) == c->net6[i][0] && (lo & c->net6[i][3]) == c->net6[i][1]) return 1;
    return 0;
}

/* Rewrite DSCP, keeping the ECN bits (an L4S or classic-ECN flow keeps its codepoint). */
INLINE void set_dscp(struct __sk_buff *skb, __u32 l3, int fam, const __u8 *ip, __u8 dscp) {
    if (dscp > 63) return;
    if (fam == 4) {
        __u8 tos = (__u8)((dscp << 2) | (ip[1] & 3));
        if (tos == ip[1]) return;
        __u16 old, nw; __u8 w[2] = { ip[0], tos };
        __builtin_memcpy(&old, ip, 2);
        __builtin_memcpy(&nw, w, 2);
        l3_csum_replace(skb, l3 + 10, old, nw, 2);
        store_bytes(skb, l3 + 1, &tos, 1, 0);
    } else {
        /* IPv6 traffic class = DSCP(6) ECN(2), spread over the low nibble of byte 0 and the high nibble of byte 1 */
        __u8 tc_old = (__u8)(((ip[0] & 0x0F) << 4) | (ip[1] >> 4));
        __u8 tc_new = (__u8)((dscp << 2) | (tc_old & 3));
        __u8 w[2] = { (__u8)((ip[0] & 0xF0) | (tc_new >> 4)), (__u8)((ip[1] & 0x0F) | ((tc_new & 0x0F) << 4)) };
        if (tc_new == tc_old) return;
        store_bytes(skb, l3, w, 2, 0);
    }
}

INLINE struct iq_half *half(struct iq_val *v, __u32 gen) {
    struct iq_half *h = &v->w[gen & 1];
    if (h->gen != gen) {              /* first packet of a new window in this half: start it from zero */
        __builtin_memset(h, 0, sizeof(*h));
        h->gen = gen;
        h->dscp_up = 255;
    }
    return h;
}

#define ACCOUNT 1
#define MARK 2

INLINE int handle(struct __sk_buff *skb, int up, int mode) {
    __u32 z = 0;
    struct iq_cfg *cfg = map_lookup(&iq_cfg, &z);
    struct iq_stats *st = map_lookup(&iq_stats, &z);
    if (!cfg || !st) return TC_ACT_OK;

    __u8 eh[2];
    __u32 l3 = 14;
    if (load_bytes(skb, 12, eh, 2)) return TC_ACT_OK;
    __u16 etype = be16(eh);
    if (etype == 0x8100 || etype == 0x88A8) {
        if (load_bytes(skb, 16, eh, 2)) return TC_ACT_OK;
        etype = be16(eh);
        l3 = 18;
    }
    __u8 ip[40];
    struct iq_key k = {};
    __u32 tos, iplen, proto, l4, frag = 0;
    int fam;
    if (etype == 0x0800) {
        if (load_bytes(skb, l3, ip, 20)) return TC_ACT_OK;
        tos = ip[1];
        iplen = be16(ip + 2);
        frag = ((ip[6] & 0x1F) << 8) | ip[7];
        proto = ip[9];
        l4 = l3 + (ip[0] & 0x0F) * 4;
        fam = 4;
        if (cfg->n4 && (up ? (!in_lan4(cfg, ip + 12) || in_lan4(cfg, ip + 16))
                           : (!in_lan4(cfg, ip + 16) || in_lan4(cfg, ip + 12)))) goto skip;
        if (up) { __builtin_memcpy(k.lan, ip + 12, 4); __builtin_memcpy(k.rem, ip + 16, 4); }
        else    { __builtin_memcpy(k.lan, ip + 16, 4); __builtin_memcpy(k.rem, ip + 12, 4); }
    } else if (etype == 0x86DD) {
        if (load_bytes(skb, l3, ip, 40)) return TC_ACT_OK;
        tos = ((ip[0] & 0x0F) << 4) | (ip[1] >> 4);
        iplen = be16(ip + 4) + 40;
        proto = ip[6];
        l4 = l3 + 40;
        fam = 6;
#pragma unroll
        for (int i = 0; i < 4; i++) {
            if (proto != 0 && proto != 43 && proto != 60) break;
            __u8 xh[2];
            if (load_bytes(skb, l4, xh, 2)) return TC_ACT_OK;
            proto = xh[0];
            l4 += (xh[1] + 1) * 8;
        }
        if (cfg->n6 && (up ? (!in_lan6(cfg, ip + 8) || in_lan6(cfg, ip + 24))
                           : (!in_lan6(cfg, ip + 24) || in_lan6(cfg, ip + 8)))) goto skip;
        if (up) { __builtin_memcpy(k.lan, ip + 8, 16); __builtin_memcpy(k.rem, ip + 24, 16); }
        else    { __builtin_memcpy(k.lan, ip + 24, 16); __builtin_memcpy(k.rem, ip + 8, 16); }
    } else {
        return TC_ACT_OK;
    }

    __u32 sport = 0, dport = 0, flags = 0, seq = 0, tsval = 0, tsecr = 0;
    int paylen = -1, spin = -1;
    if (!frag && proto == 6) {
        __u8 th[20];
        if (!load_bytes(skb, l4, th, 20)) {
            sport = be16(th); dport = be16(th + 2); seq = be32(th + 4); flags = th[13];
            __u32 doff = (th[12] >> 4) * 4;
            paylen = (int)iplen - (int)(l4 - l3) - (int)doff;
            if (doff > 20 && doff <= 60) {
                __u32 optlen = doff - 20;
                struct iq_scratch *sc = map_lookup(&iq_scratch, &z);
                if (sc && optlen <= 40 && !load_bytes(skb, l4 + 20, sc->opt, optlen)) {
                    __u8 *o = sc->opt;
                    __u32 i = 0;
                    /* At most 8 options: real stacks put the timestamp within the first four (data: NOP NOP TS;
                     * SYN: MSS SACK-OK TS NOP WS). Unrolled, with i kept small, so the verifier stays linear. */
#pragma unroll
                    for (int n = 0; n < 8; n++) {
                        if (i + 1 >= optlen) break;
                        __u8 kind = o[i & 63];
                        if (kind == 0) break;
                        if (kind == 1) { i = (i + 1) & 63; continue; }
                        __u8 len = o[(i + 1) & 63];
                        if (len < 2) break;
                        if (kind == 8) {
                            if (len == 10 && i + 10 <= optlen) {
                                tsval = ((__u32)o[(i + 2) & 63] << 24) | ((__u32)o[(i + 3) & 63] << 16) |
                                        ((__u32)o[(i + 4) & 63] << 8) | o[(i + 5) & 63];
                                tsecr = ((__u32)o[(i + 6) & 63] << 24) | ((__u32)o[(i + 7) & 63] << 16) |
                                        ((__u32)o[(i + 8) & 63] << 8) | o[(i + 9) & 63];
                            }
                            break;
                        }
                        i = (i + len) & 127;
                    }
                }
            }
        }
    } else if (!frag && proto == 17) {
        __u8 uh[9];
        if (!load_bytes(skb, l4, uh, 9)) {
            sport = be16(uh); dport = be16(uh + 2);
            if ((sport == 443 || dport == 443) && !(uh[8] & 0x80) && (uh[8] & 0x40)) spin = (uh[8] >> 5) & 1;
        } else if (!load_bytes(skb, l4, uh, 4)) {
            sport = be16(uh); dport = be16(uh + 2);
        }
    }
    k.fam = (__u8)fam;
    k.proto = (__u8)proto;
    k.lport = (__u16)(up ? sport : dport);
    k.rport = (__u16)(up ? dport : sport);

    if (!(mode & ACCOUNT)) goto verdict;
    __u64 now = ktime_ns();
    st->packets++;
    st->bytes += iplen;
    struct iq_val *v = map_lookup(&iq_flows, &k);
    if (!v) {
        void *tpl = map_lookup(&iq_zero, &z);
        if (!tpl) goto verdict;
        map_update(&iq_flows, &k, tpl, BPF_NOEXIST);      /* EEXIST if another CPU inserted it first: fine */
        v = map_lookup(&iq_flows, &k);
        if (!v) { st->insert_failed++; goto verdict; }
        if (v->first == 0) v->first = now;
    }
    v->last = now;
    struct iq_half *w = half(v, cfg->gen);
    __u64 sz = iplen;
    if (up) {
        ADD(&v->pk_up, 1); ADD(&v->by_up, sz); ADD(&w->pk_up, 1); ADD(&w->by_up, sz); ADD(&w->sq_up, sz * sz);
        if (sz > w->mx_up) w->mx_up = sz;
        if (v->last_up) { __u64 d = (now - v->last_up) / 1000; ADD(&w->iat_n_up, 1); ADD(&w->iat_s_up, d); ADD(&w->iat_q_up, d * d); }
        v->last_up = now;
    } else {
        ADD(&v->pk_dn, 1); ADD(&v->by_dn, sz); ADD(&w->pk_dn, 1); ADD(&w->by_dn, sz); ADD(&w->sq_dn, sz * sz);
        if (sz > w->mx_dn) w->mx_dn = sz;
        if (v->last_dn) { __u64 d = (now - v->last_dn) / 1000; ADD(&w->iat_n_dn, 1); ADD(&w->iat_s_dn, d); ADD(&w->iat_q_dn, d * d); }
        v->last_dn = now;
    }
    if (sz < 300) ADD(&w->small, 1); else if (sz > 1200) ADD(&w->large, 1);
    __u32 ecn = tos & 3;
    if (ecn == 2) ADD(&w->ect0, 1); else if (ecn == 1) ADD(&w->ect1, 1); else if (ecn == 3) ADD(&w->ce, 1);
    if (up && (w->dscp_up == 255 || (tos >> 2) > w->dscp_up)) w->dscp_up = tos >> 2;
    if (sz >= IQ_VID_MIN) {
        __u64 lv = up ? v->vid_up : v->vid_dn;
        if (lv == 0 || now - lv > IQ_VID_GAP_NS) { if (up) ADD(&w->fr_up, 1); else ADD(&w->fr_dn, 1); }
        if (up) v->vid_up = now; else v->vid_dn = now;
    }
    if (paylen > 0) {                                     /* retransmission (or reordering): no new data */
        __u32 e = seq + (__u32)paylen;
        if (up) {
            if (v->seq_ok_up && (int)(e - v->seq_hi_up) <= 0) ADD(&w->retx_up, 1); else { v->seq_hi_up = e; v->seq_ok_up = 1; }
        } else {
            if (v->seq_ok_dn && (int)(e - v->seq_hi_dn) <= 0) ADD(&w->retx_dn, 1); else { v->seq_hi_dn = e; v->seq_ok_dn = 1; }
        }
    }
    if (tsval) {                                          /* passive RTT from TCP timestamps (ePPing method) */
        __u32 *their_v = up ? v->tsd_v : v->tsu_v, *mine_v = up ? v->tsu_v : v->tsd_v;
        __u64 *their_t = up ? v->tsd_t : v->tsu_t, *mine_t = up ? v->tsu_t : v->tsd_t;
        __u64 t0 = 0;
#pragma unroll
        for (int i = 0; i < IQ_TS_SLOTS; i++) {           /* 1. does this packet echo a stamp the other side sent? */
            if (t0 || !their_v[i]) continue;
            if (now - their_t[i] > IQ_TS_MAX_AGE_NS) { their_v[i] = 0; continue; }
            if (their_v[i] == tsecr) t0 = their_t[i];
        }
        if (t0) {
            __u64 us = (now - t0) / 1000;
            if (up) {
                ADD(&w->rl_n, 1); ADD(&w->rl_s, us);
                if (w->rl_min == 0 || us < w->rl_min) w->rl_min = us;
                if (us > w->rl_max) w->rl_max = us;
            } else {
                ADD(&w->rw_n, 1); ADD(&w->rw_s, us);
                if (w->rw_min == 0 || us < w->rw_min) w->rw_min = us;
                if (us > w->rw_max) w->rw_max = us;
            }
#pragma unroll
            for (int i = 0; i < IQ_TS_SLOTS; i++)          /* this stamp and older ones are answered */
                if (their_v[i] && their_t[i] <= t0) their_v[i] = 0;
            __u64 g = (now - t0) / IQ_TS_SLOTS;
            g = g < IQ_TS_GAP_MIN_NS ? IQ_TS_GAP_MIN_NS : g > IQ_TS_GAP_MAX_NS ? IQ_TS_GAP_MAX_NS : g;
            if (up) v->ts_gap_dn = g; else v->ts_gap_up = g;
        }
        __u32 last = up ? v->last_tsv_up : v->last_tsv_dn;  /* 2. remember a new stamp of this side, one per gap */
        __u64 next = up ? v->ts_next_up : v->ts_next_dn;
        __u64 gap = up ? v->ts_gap_up : v->ts_gap_dn;
        if (gap == 0) gap = IQ_TS_GAP_MIN_NS;
        if (tsval != last && now >= next) {
            int slot = -1, oldest = 0;
            __u64 ot = ~0ULL;
#pragma unroll
            for (int i = 0; i < IQ_TS_SLOTS; i++) {
                if (slot < 0 && !mine_v[i]) slot = i;
                if (mine_t[i] < ot) { ot = mine_t[i]; oldest = i; }
            }
            if (slot < 0) {                                 /* all busy: the RTT is longer than the slots span */
                slot = oldest;
                gap = gap * 2 > IQ_TS_GAP_MAX_NS ? IQ_TS_GAP_MAX_NS : gap * 2;
            }
            slot &= IQ_TS_SLOTS - 1;
            mine_v[slot] = tsval;
            mine_t[slot] = now;
            if (up) { v->last_tsv_up = tsval; v->ts_next_up = now + gap; v->ts_gap_up = gap; }
            else    { v->last_tsv_dn = tsval; v->ts_next_dn = now + gap; v->ts_gap_dn = gap; }
        }
    }
    if (spin >= 0 && !up) {                               /* QUIC spin-bit edges, server -> device */
        if (v->spin_seen && (__u8)spin != v->spin_last) {
            __u64 us = (now - v->spin_t) / 1000;
            if (us >= 1000 && us <= 2000000) { ADD(&w->sp_n, 1); ADD(&w->sp_s, us); }
        }
        if (!v->spin_seen || (__u8)spin != v->spin_last) { v->spin_t = now; v->spin_last = (__u8)spin; v->spin_seen = 1; }
    }
    if (v->early_n < IQ_EARLY_N) {
        __u32 j = v->early_n & (IQ_EARLY_N - 1);
        v->e_t[j] = (__u32)((now - v->first) / 1000);
        v->e_size[j] = (__u16)(sz > 65535 ? 65535 : sz);
        v->e_dir[j] = (__u8)up;
        v->early_n++;
    }
    if (flags & 0x02) v->syn = 1;
    if (flags & 0x05) v->fin = 1;

verdict:;
    if (!(mode & MARK)) return TC_ACT_OK;
    __u32 *tin = map_lookup(&iq_verdict, &k);
    if (tin && *tin < IQ_TINS) {
        __u32 t = *tin & 7;
        skb->mark = IQ_MARK_BASE + t;                    /* survives forwarding: the WAN program reads it */
        if (!up) {
            skb->priority = cfg->prio[t];                 /* LAN egress runs before the download shaper */
            if (cfg->mark_dscp) set_dscp(skb, l3, fam, ip, cfg->dscp[t]);
        }
    }
    return TC_ACT_OK;
skip:
    st->skipped++;
    return TC_ACT_OK;
}

SEC("tc_up") int iq_up(struct __sk_buff *skb) { return handle(skb, 1, ACCOUNT | MARK); }
SEC("tc_down") int iq_down(struct __sk_buff *skb) { return handle(skb, 0, ACCOUNT | MARK); }
/* LAN bridge (OpenWrt br-lan): mark on the bridge's egress, BEFORE its download shaper, and account on each
 * member port's egress, AFTER it, so the LAN round trip is the Wi-Fi/cable alone, as with the packet ring. */
SEC("tc_mark") int iq_mark(struct __sk_buff *skb) { return handle(skb, 0, MARK); }
SEC("tc_down_acct") int iq_down_acct(struct __sk_buff *skb) { return handle(skb, 0, ACCOUNT); }

SEC("tc_wan") int iq_wan(struct __sk_buff *skb) {
    __u32 z = 0, t = skb->mark - IQ_MARK_BASE;
    if (t >= IQ_TINS) return TC_ACT_OK;
    struct iq_cfg *cfg = map_lookup(&iq_cfg, &z);
    if (!cfg) return TC_ACT_OK;
    skb->priority = cfg->prio[t & 7];                     /* upload shaper class */
    if (!cfg->mark_dscp) return TC_ACT_OK;
    __u32 l3 = cfg->wan_l2 ? 14 : 0;
    __u8 ip[2];
    if (load_bytes(skb, l3, ip, 2)) return TC_ACT_OK;
    int fam = (ip[0] >> 4) == 4 ? 4 : (ip[0] >> 4) == 6 ? 6 : 0;
    if (fam) set_dscp(skb, l3, fam, ip, cfg->dscp[t & 7]);
    return TC_ACT_OK;
}

char _license[] SEC("license") = "Dual MIT/GPL";
