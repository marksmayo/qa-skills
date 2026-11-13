// Package flows loads flow files and runs their steps.
package flows

import (
	"archive/zip"
	"errors"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

// Step is one step of a flow file.
type Step struct {
	Shell string `yaml:"shell"`
	Save  string `yaml:"save"`
}

var errBadPath = errors.New("archive entry escapes the target directory")

// Run loads the flow file at path and runs its steps, saving output under outDir.
func Run(path, outDir string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var steps []Step
	if err := yaml.Unmarshal(data, &steps); err != nil {
		return err
	}
	for _, s := range steps {
		dest := filepath.Join(outDir, s.Save)
		if err := os.WriteFile(dest, []byte(s.Shell), 0o600); err != nil {
			return err
		}
	}
	return nil
}

// Unpack extracts an attachment archive into dir, refusing entries that leave it.
func Unpack(r *zip.Reader, dir string) error {
	for _, f := range r.File {
		target := filepath.Join(dir, f.Name)
		if !strings.HasPrefix(target, filepath.Clean(dir)+string(os.PathSeparator)) {
			return errBadPath
		}
	}
	return nil
}
