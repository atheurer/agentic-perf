package ui

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"

	"github.com/atheurer/agentic-perf/tui/internal/api"
	"github.com/atheurer/agentic-perf/tui/internal/config"
)

type wizardStep int

const (
	wizardURL wizardStep = iota
	wizardToken
	wizardDone
)

func (m *Model) startWizard() {
	m.wizardStep = wizardURL
	m.input.Placeholder = "State store URL [http://localhost:8090]"
	m.input.Focus()
	m.addSystemLine("First-run setup — configure your agentic-perf connection.")
	m.addSystemLine("Enter the state store URL (or press Enter for default):")
}

func (m *Model) handleWizardInput() tea.Cmd {
	text := strings.TrimSpace(m.input.Value())
	m.input.SetValue("")

	switch m.wizardStep {
	case wizardURL:
		if text == "" {
			text = "http://localhost:8090"
		}
		m.wizardURL = text
		m.addSystemLine(fmt.Sprintf("  URL: %s", text))
		m.wizardStep = wizardToken
		m.input.Placeholder = "API token (paste bearer token):"
		return nil

	case wizardToken:
		if text == "" {
			m.addSystemLine("  Token is required. Paste the value from ~/.agentic-perf/secrets/api-token")
			return nil
		}
		m.wizardToken = text
		m.addSystemLine("  Token: ****")
		m.wizardStep = wizardDone

		cfg := config.Config{URL: m.wizardURL, Token: m.wizardToken}
		if err := config.Write(cfg); err != nil {
			m.addSystemLine(fmt.Sprintf("  Warning: could not save config: %v", err))
		} else {
			m.addSystemLine(fmt.Sprintf("  Config saved to %s", config.Path()))
		}

		m.client = api.New(cfg.URL, cfg.Token)
		m.conn = connConnecting
		m.input.Blur()
		m.input.Placeholder = "Type / for commands, Esc to interject..."
		return connectCmd(m.client)
	}

	return nil
}
