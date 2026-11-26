// IntentQoS home-kit programs. One code base, four programs (chosen at build time with a build tag):
//
//	console  IntentQoS Operator Console   runs the fleet controller, operator console and phone-app server
//	router   IntentQoS Router Setup       installs IntentQoS on the OpenWrt router over SSH and pairs it
//	testkit  IntentQoS Test Kit           measures the line with QoS off and on, writes a report
//	bridge   IntentQoS 4G Bridge          shares a phone's USB-tethered 4G over this laptop's Ethernet port
//
// Build them with kit/build.py. Each program opens its own window (a local page in an Edge/Chrome app
// window) and also has a command-line mode for scripted use and tests (run with --help).
package main

import "os"

func main() {
	unblockSiblings() // before anything else: later kit programs on this laptop then open without SmartScreen
	runPart(os.Args[1:])
}
