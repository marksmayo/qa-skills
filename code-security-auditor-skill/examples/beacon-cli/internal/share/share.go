// Package share serves the device tree to other tools on the same machine.
package share

import (
	"encoding/json"
	"net"
	"net/http"
	"os"

	"github.com/urfave/cli/v2"
)

const socketPath = "/tmp/beacon-share.sock"

// Start serves the tree on a unix socket until the process exits.
func Start(c *cli.Context) error {
	os.Remove(socketPath)
	ln, err := net.Listen("unix", socketPath)
	if err != nil {
		return err
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/tree", treeHandler)
	mux.HandleFunc("GET /devices/{id}", deviceHandler)
	return http.Serve(ln, mux)
}

func treeHandler(w http.ResponseWriter, r *http.Request) {
	json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

func deviceHandler(w http.ResponseWriter, r *http.Request) {
	json.NewEncoder(w).Encode(map[string]string{"id": r.PathValue("id")})
}
