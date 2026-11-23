//go:build !console && !router && !testkit && !bridge

package main

import "fmt"

const partTitle = "IntentQoS kit"

func runPart(args []string) {
	fmt.Println("build one program with a tag: go build -tags console|router|testkit|bridge (see kit/build.py)")
}
