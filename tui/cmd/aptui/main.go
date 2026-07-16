package main

import (
	"flag"
	"fmt"
	"os"

	tea "github.com/charmbracelet/bubbletea"

	"github.com/atheurer/agentic-perf/tui/internal/api"
	"github.com/atheurer/agentic-perf/tui/internal/config"
	"github.com/atheurer/agentic-perf/tui/internal/ui"
)

func main() {
	var (
		flagURL      string
		flagToken    string
		flagTicketID string
	)

	flag.StringVar(&flagURL, "url", "", "State store URL (default http://localhost:8090)")
	flag.StringVar(&flagToken, "token", "", "API bearer token")
	flag.StringVar(&flagTicketID, "ticket", "", "Ticket ID to follow")
	flag.Parse()

	if flag.NArg() > 0 && flagTicketID == "" {
		flagTicketID = flag.Arg(0)
	}

	cfg := config.Load(flagURL, flagToken)
	client := api.New(cfg.URL, cfg.Token)
	model := ui.New(client, flagTicketID)

	p := tea.NewProgram(model, tea.WithAltScreen())
	if _, err := p.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "aptui: %v\n", err)
		os.Exit(1)
	}
}
