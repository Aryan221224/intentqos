//go:build windows

package main

import "os"

// removeZoneMark deletes the Zone.Identifier stream (Mark of the Web) of a file; true if one was removed.
func removeZoneMark(path string) bool {
	return os.Remove(path+":Zone.Identifier") == nil
}
