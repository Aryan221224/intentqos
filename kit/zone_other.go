//go:build !windows

package main

func removeZoneMark(path string) bool { return false }
