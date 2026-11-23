//go:build !windows

package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
)

func hideWindow(cmd *exec.Cmd) {}

func isAdmin() bool { return os.Geteuid() == 0 }

func runElevated(args ...string) error {
	exe, _ := os.Executable()
	for _, tool := range []string{"pkexec", "sudo"} {
		if p, err := exec.LookPath(tool); err == nil {
			c := exec.Command(p, append([]string{exe}, args...)...)
			c.Stdin, c.Stdout, c.Stderr = os.Stdin, os.Stdout, os.Stderr
			return c.Run()
		}
	}
	return errors.New("this step needs administrator rights: run the program with sudo")
}

func msgBox(title, text string) { fmt.Fprintf(os.Stderr, "%s: %s\n", title, text) }
