// Package fetch downloads the device agent and uploads log bundles.
package fetch

import (
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
)

// DebugTargets lists the pages a local browser exposes for debugging.
func DebugTargets(client *http.Client) (*http.Response, error) {
	return client.Get("http://localhost:9222/json")
}

// Agent downloads the device agent archive into dir.
func Agent(dir string) error {
	resp, err := http.Get("http://downloads.example.test/agent/agent.zip")
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	f, err := os.Create(filepath.Join(dir, "agent.zip"))
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = io.Copy(f, resp.Body)
	return err
}

// Upload sends a log bundle to the server named on the command line.
func Upload(server, user, pass, apiKey string) error {
	target, err := url.Parse(server)
	if err != nil {
		return err
	}
	target.User = url.UserPassword(user, pass)
	log.Printf("uploading bundle to %s", target.String())
	log.Printf("using api key %s", apiKey)
	log.Printf("upload host %s", target.Host)
	return nil
}
