/*
 * iq-flow.h -- the map layout shared by the kernel flow engine (iq-flow.bpf.c) and iq-capture -e.
 *
 * Every struct lists 64-bit fields first, then 32-, 16- and 8-bit ones, and pads to a multiple of 8, so the
 * layout is the same for the BPF target and for any 32- or 64-bit user space (i386 aligns u64 to 4 inside
 * structs; with this ordering no member's offset depends on that).
 */
#ifndef IQ_FLOW_H
#define IQ_FLOW_H
#include <linux/types.h>

#define IQ_EARLY_N 16
#define IQ_TS_SLOTS 16
#define IQ_TINS 5
#define IQ_MARK_BASE 0x51000          /* same marks as the nftables marker: 0x51000 + tin */
#define IQ_MAX_FLOWS 4096
#define IQ_VID_MIN 200                /* bytes */
#define IQ_VID_GAP_NS 4000000ULL      /* 4 ms */
#define IQ_TS_MAX_AGE_NS 3000000000ULL
#define IQ_TS_GAP_MIN_NS 2000000ULL
#define IQ_TS_GAP_MAX_NS 64000000ULL

struct iq_key {                       /* 38 bytes, identical to struct key in iq-capture.c */
    __u8 fam, proto;
    __u16 lport, rport;               /* host byte order */
    __u8 lan[16], rem[16];            /* network byte order; IPv4 in the first 4 bytes */
};

struct iq_half {                      /* one window; the engine closes a window every 0.5 s */
    __u64 pk_up, by_up, sq_up, mx_up, pk_dn, by_dn, sq_dn, mx_dn, small, large;
    __u64 iat_n_up, iat_s_up, iat_q_up, iat_n_dn, iat_s_dn, iat_q_dn;      /* sums in us and us^2 */
    __u64 ect0, ect1, ce, retx_up, retx_dn, fr_up, fr_dn;
    __u64 rw_n, rw_s, rw_min, rw_max, rl_n, rl_s, rl_min, rl_max, sp_n, sp_s;   /* RTTs in us */
    __u32 gen, dscp_up;               /* gen: which window these counts belong to; dscp_up 255 = none */
};

struct iq_val {
    __u64 first, last, last_up, last_dn, pk_up, pk_dn, by_up, by_dn;     /* CLOCK_MONOTONIC ns */
    __u64 tsu_t[IQ_TS_SLOTS], tsd_t[IQ_TS_SLOTS];
    __u64 ts_next_up, ts_next_dn, ts_gap_up, ts_gap_dn, spin_t, vid_up, vid_dn;
    struct iq_half w[2];              /* double buffer: the kernel writes w[gen & 1], user space reads the other */
    __u32 tsu_v[IQ_TS_SLOTS], tsd_v[IQ_TS_SLOTS];
    __u32 seq_hi_up, seq_hi_dn, last_tsv_up, last_tsv_dn;
    __u32 e_t[IQ_EARLY_N];            /* us since first packet */
    __u16 e_size[IQ_EARLY_N];
    __u8 e_dir[IQ_EARLY_N];
    __u8 seq_ok_up, seq_ok_dn, spin_seen, spin_last, early_n, syn, fin, pad;
};

struct iq_cfg {
    __u64 net6[4][4];                 /* LAN prefixes: addr hi, addr lo, mask hi, mask lo (raw network order) */
    __u32 net4[4][2];                 /* addr, mask (network order) */
    __u32 n4, n6, gen, mark_dscp, wan_l2, pad;
    __u32 prio[8];                    /* skb->priority per tin (tc classid major<<16 | minor) */
    __u8 dscp[8];                     /* DSCP per tin; 255 = leave as is */
};

struct iq_stats { __u64 packets, bytes, skipped, insert_failed; };

struct iq_scratch { __u8 opt[64]; };

#endif
