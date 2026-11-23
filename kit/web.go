package main

import (
	"embed"
	"strconv"
)

//go:embed web
var webFS embed.FS

func webFile(name string) []byte {
	b, err := webFS.ReadFile("web/" + name)
	if err != nil {
		return nil
	}
	return b
}

func itoa(i int) string { return strconv.Itoa(i) }
