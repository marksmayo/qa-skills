// Package device talks to paired devices.
package device

import (
	"crypto/rand"
	"encoding/hex"
)

// TokenHeader is the request header that carries a device session token.
const TokenHeader = "X-Beacon-Token"

// NewSessionToken returns a random session token for a paired device.
func NewSessionToken() (string, error) {
	token := make([]byte, 16)
	if _, err := rand.Read(token); err != nil {
		return "", err
	}
	return hex.EncodeToString(token), nil
}
