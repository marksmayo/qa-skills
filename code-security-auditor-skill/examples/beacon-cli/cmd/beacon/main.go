// Command beacon collects device logs and ships them to a log server.
package main

import (
	"fmt"
	"os"

	"github.com/urfave/cli/v2"

	"example.test/beacon/internal/flows"
	"example.test/beacon/internal/share"
)

func main() {
	app := &cli.App{
		Name:  "beacon",
		Usage: "collect and ship device logs",
		Flags: []cli.Flag{
			&cli.StringFlag{Name: "server", EnvVars: []string{"BEACON_SERVER"}},
			&cli.StringFlag{Name: "api-key", EnvVars: []string{"BEACON_API_KEY"}},
			&cli.StringFlag{Name: "out-dir", Value: "reports"},
		},
		Commands: []*cli.Command{
			{
				Name:   "run",
				Usage:  "run the steps in a flow file",
				Action: runFlow,
			},
			{
				Name:   "share",
				Usage:  "share the device tree with local tools",
				Action: share.Start,
			},
		},
	}
	if err := app.Run(os.Args); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func runFlow(c *cli.Context) error {
	return flows.Run(c.Args().First(), c.String("out-dir"))
}
