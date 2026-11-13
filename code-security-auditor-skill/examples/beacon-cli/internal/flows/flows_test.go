package flows

import (
	"os"
	"path/filepath"
	"testing"
)

// TestRunKeepsSavedOutputInsideOutDir is the regression test a fix for the save path would add.
func TestRunKeepsSavedOutputInsideOutDir(t *testing.T) {
	t.Run("save path with parent segments", func(t *testing.T) {
		dir := t.TempDir()
		flow := filepath.Join(dir, "flow.yaml")
		os.WriteFile(flow, []byte("- shell: echo\n  save: ../escaped.txt\n"), 0o600)
		out := filepath.Join(dir, "out")
		os.Mkdir(out, 0o700)
		if err := Run(flow, out); err == nil {
			t.Fatal("a save path outside the output directory was accepted")
		}
	})
}
