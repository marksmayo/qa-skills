// Package runner starts the external tools a flow needs.
package runner

import (
	"context"
	"os/exec"
)

// Simctl runs a simulator control command with a fixed program.
func Simctl(ctx context.Context, args ...string) error {
	return exec.CommandContext(ctx, "xcrun", append([]string{"simctl"}, args...)...).Run()
}

// Shell runs a flow's shell step.
func Shell(script string) ([]byte, error) {
	return exec.Command("sh", "-c", script).CombinedOutput()
}

// Tool runs a helper binary chosen by the caller.
func Tool(ctx context.Context, bin string, args []string) error {
	return exec.CommandContext(ctx, bin, args...).Run()
}
