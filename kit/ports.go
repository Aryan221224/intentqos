package main

import (
	"fmt"
	"net"
	"os"
	"path/filepath"
)

// busyPorts returns the TCP ports in tcp and UDP ports in udp that another program already holds.
func busyPorts(tcp, udp []int) []int {
	var out []int
	for _, p := range tcp {
		ln, err := net.Listen("tcp", fmt.Sprintf(":%d", p))
		if err != nil {
			out = append(out, p)
			continue
		}
		ln.Close()
	}
	for _, p := range udp {
		c, err := net.ListenPacket("udp", fmt.Sprintf(":%d", p))
		if err != nil {
			out = append(out, p)
			continue
		}
		c.Close()
	}
	return out
}

// unblockSiblings removes Windows' "downloaded from the internet" mark from the other kit programs in this
// program's folder, so SmartScreen asks once per laptop (for the first program opened), not once per program.
func unblockSiblings() {
	exe, err := os.Executable()
	if err != nil {
		return
	}
	unblockIn(filepath.Dir(exe))
}

func unblockIn(dir string) {
	files, _ := filepath.Glob(filepath.Join(dir, "IntentQoS *.exe"))
	n := 0
	for _, f := range files {
		if removeZoneMark(f) {
			n++
		}
	}
	if n > 0 {
		logf("removed the downloaded-file mark from %d kit program(s) in %s", n, dir)
	}
}
