// In-home measurement endpoints served by the Operator Console laptop, and the matching Test Kit clients.
//
// RPM endpoints (draft-ietf-ippm-responsiveness-09, Section 7): /.well-known/nq returns the config, and
// /rpm/small (1 byte), /rpm/large (8 GB, streamed zeros) and /rpm/upload (discards a POST) let the Test Kit
// measure responsiveness across the home network alone, over HTTPS with HTTP/2, with a self-signed
// certificate made at start-up (the Test Kit pins its SHA-256 fingerprint, published by the console).
//
// LAN capacity: a UDP load test following the RFC 9097 method (the sender adjusts its rate from receiver
// feedback every 50 ms, raising it while there is no loss and the delay range stays under 30 ms, lowering it
// on loss or a range over 90 ms; the result is the maximum of the 1-second sub-interval rates). The messages
// are IntentQoS's own, NOT the RFC 9946 (UDPST) wire protocol, so this does not interoperate with UDPST
// servers and its results should be read as "RFC 9097 method", not as an RFC 9946 measurement.
package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"math/big"
	"net"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	labHTTPSPort = 8446
	labUDPPort   = 8447
	capMagic     = "IQCT"
	capStatus    = "IQCS"
	capHdr       = 24
	capFeedback  = 50 * time.Millisecond
)

// ------------------------------------------------------------------ console side

type labServer struct {
	Fingerprint string // SHA-256 of the certificate (hex), for pinning
	srv         *http.Server
	udp         *net.UDPConn
}

func selfSigned() (tls.Certificate, string, error) {
	k, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	sn, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 62))
	tpl := &x509.Certificate{SerialNumber: sn, Subject: pkix.Name{CommonName: "IntentQoS lab endpoints"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(30 * 24 * time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	der, err := x509.CreateCertificate(rand.Reader, tpl, tpl, &k.PublicKey, k)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	sum := sha256.Sum256(der)
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: k}, hex.EncodeToString(sum[:]), nil
}

type zeroBody struct{ left int64 }

func (z *zeroBody) Read(p []byte) (int, error) {
	if z.left <= 0 {
		return 0, io.EOF
	}
	n := int64(len(p))
	if n > z.left {
		n = z.left
	}
	clear(p[:n])
	z.left -= n
	return int(n), nil
}

func startLab(ctx context.Context, httpsPort, udpPort int) (*labServer, error) {
	cert, fp, err := selfSigned()
	if err != nil {
		return nil, err
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/.well-known/nq", func(w http.ResponseWriter, r *http.Request) {
		base := "https://" + r.Host
		writeJSON(w, 200, map[string]any{"version": 1, "urls": map[string]string{
			"large_download_url": base + "/rpm/large", "small_download_url": base + "/rpm/small", "upload_url": base + "/rpm/upload"}})
	})
	mux.HandleFunc("/rpm/small", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		_, _ = w.Write([]byte{'x'})
	})
	mux.HandleFunc("/rpm/large", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/octet-stream")
		w.Header().Set("Cache-Control", "no-store")
		_, _ = io.CopyBuffer(w, &zeroBody{left: 8 << 30}, make([]byte, 256<<10))
	})
	mux.HandleFunc("/rpm/upload", func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		w.WriteHeader(200)
	})
	ls := &labServer{Fingerprint: fp}
	ls.srv = &http.Server{Addr: fmt.Sprintf(":%d", httpsPort), Handler: mux,
		TLSConfig: &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12, NextProtos: []string{"h2", "http/1.1"}}}
	ln, err := tls.Listen("tcp", ls.srv.Addr, ls.srv.TLSConfig)
	if err != nil {
		return nil, err
	}
	go func() { _ = ls.srv.Serve(ln) }()
	ua, _ := net.ResolveUDPAddr("udp", fmt.Sprintf(":%d", udpPort))
	if ls.udp, err = net.ListenUDP("udp", ua); err != nil {
		_ = ls.srv.Close()
		return nil, err
	}
	go capacitySink(ctx, ls.udp)
	go func() { <-ctx.Done(); _ = ls.srv.Close(); _ = ls.udp.Close() }()
	return ls, nil
}

// capacitySink counts each sender's datagrams and reports back every 50 ms: bytes, packets, losses
// (sequence gaps) and the range of one-way delay (receive time minus the sender's clock, relative to its
// minimum, so the two clocks need not agree).
func capacitySink(ctx context.Context, c *net.UDPConn) {
	type peer struct {
		addr                 *net.UDPAddr
		next                 uint32
		bytes, pkts, lost    uint64
		minOff, maxOff, base int64
		haveBase             bool
		last, lastReport     time.Time
	}
	peers := map[string]*peer{}
	buf := make([]byte, 65536)
	for ctx.Err() == nil {
		_ = c.SetReadDeadline(time.Now().Add(200 * time.Millisecond))
		n, ra, err := c.ReadFromUDP(buf)
		now := time.Now()
		if err == nil && n >= capHdr && string(buf[:4]) == capMagic {
			p := peers[ra.String()]
			if p == nil || now.Sub(p.last) > 5*time.Second {
				p = &peer{addr: ra, lastReport: now}
				peers[ra.String()] = p
			}
			p.last = now
			seq := binary.BigEndian.Uint32(buf[4:8])
			sent := int64(binary.BigEndian.Uint64(buf[8:16]))
			if seq >= p.next {
				p.lost += uint64(seq - p.next)
				p.next = seq + 1
			}
			p.bytes += uint64(n) + 28 // IP + UDP headers: capacity is an IP-layer metric (RFC 9097)
			p.pkts++
			off := now.UnixNano() - sent
			if !p.haveBase {
				p.base, p.haveBase = off, true
			}
			off -= p.base
			if p.pkts == 1 || off < p.minOff {
				p.minOff = off
			}
			if p.pkts == 1 || off > p.maxOff {
				p.maxOff = off
			}
		}
		for k, p := range peers {
			if now.Sub(p.lastReport) >= capFeedback && p.pkts > 0 {
				st := make([]byte, 40)
				copy(st, capStatus)
				binary.BigEndian.PutUint32(st[4:8], p.next)
				binary.BigEndian.PutUint64(st[8:16], p.bytes)
				binary.BigEndian.PutUint64(st[16:24], p.pkts)
				binary.BigEndian.PutUint64(st[24:32], p.lost)
				binary.BigEndian.PutUint32(st[32:36], uint32((p.maxOff-p.minOff)/1000))
				binary.BigEndian.PutUint32(st[36:40], uint32(now.Sub(p.lastReport).Microseconds()))
				_, _ = c.WriteToUDP(st, p.addr)
				p.bytes, p.pkts, p.lost, p.lastReport = 0, 0, 0, now
				p.minOff, p.maxOff = 0, 0
			}
			if now.Sub(p.last) > 10*time.Second {
				delete(peers, k)
			}
		}
	}
}

// ------------------------------------------------------------------ Test Kit side

type lanCapacity struct {
	MaxMbps     float64   `json:"max_ip_capacity_mbps"` // maximum of the 1-second sub-interval rates
	SubMbps     []float64 `json:"sub_interval_mbps"`
	LossPct     float64   `json:"loss_pct"`
	RangeMsP95  float64   `json:"delay_range_ms_p95"`
	Seconds     int       `json:"seconds"`
	Method      string    `json:"method"`
	Server      string    `json:"server"`
	FinalRateMb float64   `json:"final_send_rate_mbps"`
}

// capacityTest runs the RFC 9097-method UDP load test to the console's sink for d.
func capacityTest(ctx context.Context, host string, port int, d time.Duration, onRate func(float64)) (lanCapacity, error) {
	res := lanCapacity{Method: "RFC 9097 method (load-rate adjustment from 50 ms feedback, maximum of 1 s sub-intervals); " +
		"IntentQoS messages, not the RFC 9946 UDPST protocol", Server: net.JoinHostPort(host, fmt.Sprint(port)), Seconds: int(d.Seconds())}
	c, err := net.Dial("udp", res.Server)
	if err != nil {
		return res, err
	}
	defer c.Close()
	const size = 1222 // datagram payload, as UDPST's default
	pkt := make([]byte, size)
	copy(pkt, capMagic)
	rate := 1.0 // Mbit/s
	fast := true
	var mu sync.Mutex
	var seq uint32
	var subBytes float64
	var subs []float64
	var ranges []float64
	var rx, lost uint64
	ctx, cancel := context.WithTimeout(ctx, d)
	defer cancel()
	go func() { // feedback: adjust the rate
		buf := make([]byte, 64)
		for ctx.Err() == nil {
			_ = c.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
			n, err := c.Read(buf)
			if err != nil || n < 40 || string(buf[:4]) != capStatus {
				continue
			}
			by := float64(binary.BigEndian.Uint64(buf[8:16]))
			pk := binary.BigEndian.Uint64(buf[16:24])
			ls := binary.BigEndian.Uint64(buf[24:32])
			rng := float64(binary.BigEndian.Uint32(buf[32:36])) / 1000
			mu.Lock()
			subBytes += by
			rx += pk
			lost += ls
			ranges = append(ranges, rng)
			switch {
			case ls > 0 || rng > 90:
				rate = math.Max(1, rate*0.85)
				fast = false
			case rng < 30 && fast:
				rate *= 1.25
			case rng < 30:
				rate += math.Max(1, rate*0.02)
			}
			mu.Unlock()
		}
	}()
	tick := time.NewTicker(time.Millisecond)
	defer tick.Stop()
	start, secStart := time.Now(), time.Now()
	var debt float64
	for ctx.Err() == nil {
		<-tick.C
		mu.Lock()
		r := rate
		mu.Unlock()
		debt += r * 1e6 / 8 / 1000 // bytes due this millisecond
		for debt >= size+28 {
			seq++
			binary.BigEndian.PutUint32(pkt[4:8], seq)
			binary.BigEndian.PutUint64(pkt[8:16], uint64(time.Now().UnixNano()))
			binary.BigEndian.PutUint32(pkt[16:20], uint32(r*1000))
			if _, err := c.Write(pkt); err != nil {
				debt = 0
				break
			}
			debt -= size + 28
		}
		if time.Since(secStart) >= time.Second {
			mu.Lock()
			subs = append(subs, math.Round(subBytes*8/1e6/time.Since(secStart).Seconds()*10)/10)
			subBytes = 0
			mu.Unlock()
			secStart = time.Now()
			if onRate != nil {
				onRate(subs[len(subs)-1])
			}
		}
	}
	_ = start
	mu.Lock()
	defer mu.Unlock()
	if len(subs) == 0 || rx == 0 {
		return res, errors.New("no feedback from the Operator Console's capacity endpoint (is it running, and port 8447/udp open?)")
	}
	res.SubMbps = subs
	for _, s := range subs[1:] { // the first second is the ramp
		res.MaxMbps = math.Max(res.MaxMbps, s)
	}
	if len(subs) == 1 {
		res.MaxMbps = subs[0]
	}
	res.LossPct = math.Round(float64(lost)/float64(rx+lost)*1000) / 10
	sort.Float64s(ranges)
	res.RangeMsP95 = math.Round(ranges[int(float64(len(ranges)-1)*0.95)]*10) / 10
	res.FinalRateMb = math.Round(rate*10) / 10
	return res, nil
}

// pinnedClient trusts exactly one certificate (by SHA-256), for the console's lab endpoints.
func pinnedClient(fp string, conns int) []*http.Client {
	var out []*http.Client
	for i := 0; i < conns; i++ {
		tr := &http.Transport{Proxy: nil, ForceAttemptHTTP2: true, MaxConnsPerHost: 1,
			TLSClientConfig: &tls.Config{InsecureSkipVerify: true, VerifyConnection: func(cs tls.ConnectionState) error {
				if len(cs.PeerCertificates) == 0 {
					return errors.New("no certificate")
				}
				sum := sha256.Sum256(cs.PeerCertificates[0].Raw)
				if !strings.EqualFold(hex.EncodeToString(sum[:]), fp) {
					return errors.New("lab endpoint certificate does not match the console's fingerprint")
				}
				return nil
			}}}
		out = append(out, &http.Client{Transport: tr})
	}
	return out
}

// labConfig fetches the console's /.well-known/nq (RPM endpoints).
func labConfig(c *http.Client, host string) (map[string]string, error) {
	return labConfigAt(c, net.JoinHostPort(host, fmt.Sprint(labHTTPSPort)))
}

func labConfigAt(c *http.Client, hostport string) (map[string]string, error) {
	var doc struct {
		Version int               `json:"version"`
		URLs    map[string]string `json:"urls"`
	}
	resp, err := c.Get("https://" + hostport + "/.well-known/nq")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if err := json.NewDecoder(resp.Body).Decode(&doc); err != nil || doc.Version != 1 {
		return nil, errors.New("not an RPM configuration")
	}
	return doc.URLs, nil
}
