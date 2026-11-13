// Command beaconctl manages paired devices.
package main

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

var verbose bool

var rootCmd = &cobra.Command{
	Use:   "beaconctl",
	Short: "manage paired devices",
}

var pairCmd = &cobra.Command{
	Use:   "pair [device]",
	Short: "pair a device",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		fmt.Println("pairing", args[0])
		return nil
	},
}

func main() {
	rootCmd.PersistentFlags().BoolVar(&verbose, "verbose", false, "print more")
	rootCmd.AddCommand(pairCmd)
	if err := rootCmd.Execute(); err != nil {
		os.Exit(1)
	}
}
