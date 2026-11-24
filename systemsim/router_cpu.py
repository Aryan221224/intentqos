"""Per-process CPU on the router over 10 s (copied to the router and run there with python3)."""
import os, time
def snap():
    d = {}
    for p in os.listdir('/proc'):
        if p.isdigit():
            try:
                s = open(f'/proc/{p}/stat').read(); name = s[s.index('(')+1:s.rindex(')')]
                f = s[s.rindex(')')+2:].split(); d[p] = (name, int(f[11]) + int(f[12]), open(f'/proc/{p}/cmdline','rb').read().replace(b'\0',b' ')[:70].decode())
            except Exception: pass
    return d
def tot():
    f = open('/proc/stat').readline().split()[1:]; v = list(map(int, f)); return sum(v), v[3] + v[4], v[5] + v[6]
a, ta = snap(), tot(); time.sleep(10); b, tb = snap(), tot()
T = tb[0] - ta[0]
print(f"total busy {100*(T-(tb[1]-ta[1]))/T:.1f}% of 2 cores, irq+softirq {100*(tb[2]-ta[2])/T:.1f}%")
rows = sorted(((b[p][1] - a[p][1], b[p][0], b[p][2]) for p in b if p in a), reverse=True)[:12]
for t, n, c in rows: print(f"{t/10:5.1f}% of one core  {n:16s} {c}")
