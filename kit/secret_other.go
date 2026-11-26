//go:build !windows

package main

// protect / unprotect: on Linux and macOS the kit profile is a 0600 file in the user's home; no extra layer.
func protect(b []byte) ([]byte, error)   { return append([]byte(nil), b...), nil }
func unprotect(b []byte) ([]byte, error) { return append([]byte(nil), b...), nil }
