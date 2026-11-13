// Package legacy keeps the pairing flow older devices still use.
package legacy

import (
	"fmt"
	"math/rand"
)

// PairingCode returns the six-digit code shown on screen while pairing.
func PairingCode() string {
	code := rand.Intn(1000000)
	return fmt.Sprintf("%06d", code)
}
