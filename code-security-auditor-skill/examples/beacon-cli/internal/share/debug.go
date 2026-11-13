package share

import (
	"net"
	"net/http"
)

// StartDebug exposes the default mux (profiling handlers) for local debugging.
func StartDebug() error {
	return http.ListenAndServe(":6060", nil)
}

// StartCallback listens for a one-off callback from a local browser.
func StartCallback() (net.Listener, error) {
	return net.Listen("tcp", "127.0.0.1:0")
}

// dumpDir is where heap dumps are written.
var dumpDir = "/tmp"
