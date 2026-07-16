package ui

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"

	"github.com/atheurer/agentic-perf/tui/internal/api"
	"github.com/atheurer/agentic-perf/tui/internal/config"
)

func (m *Model) dispatchCommand(text string) tea.Cmd {
	parts := strings.Fields(text)
	if len(parts) == 0 {
		return nil
	}
	cmd := parts[0]
	args := parts[1:]

	switch cmd {
	case "/quit", "/q":
		m.quitting = true
		if m.source != nil {
			m.source.Close()
		}
		return tea.Quit

	case "/verbose":
		m.verbose = !m.verbose
		m.updateViewportContent()

	case "/help":
		m.addSystemLine("Commands: /submit /tickets /ticket ID /follow ID /abort ID /retry ID /stop ID /logs ID /usage [ID] /connect /config [key val] /verbose /quit /help")

	case "/tickets":
		filter := ""
		if len(args) > 0 {
			filter = args[0]
		}
		return m.cmdListTickets(filter)

	case "/ticket":
		if len(args) == 0 {
			if m.ticketID != "" {
				return m.cmdShowTicket(m.ticketID)
			}
			m.addSystemLine("Usage: /ticket <id>")
			return nil
		}
		return m.cmdShowTicket(args[0])

	case "/follow":
		return m.cmdFollow(args)

	case "/submit":
		return m.cmdSubmit(args)

	case "/abort":
		id := m.ticketID
		if len(args) > 0 {
			id = args[0]
		}
		if id == "" {
			m.addSystemLine("Usage: /abort [ticket_id]")
			return nil
		}
		return m.cmdAbort(id)

	case "/retry":
		id := m.ticketID
		if len(args) > 0 {
			id = args[0]
		}
		if id == "" {
			m.addSystemLine("Usage: /retry [ticket_id]")
			return nil
		}
		return m.cmdRetry(id)

	case "/stop":
		return m.cmdStop(args)

	case "/logs":
		id := m.ticketID
		agent := ""
		if len(args) > 0 {
			id = args[0]
		}
		if len(args) > 1 {
			agent = args[1]
		}
		if id == "" {
			m.addSystemLine("Usage: /logs [ticket_id] [agent]")
			return nil
		}
		return m.cmdLogs(id, agent)

	case "/usage":
		if len(args) > 0 {
			return m.cmdUsageTicket(args[0])
		}
		return m.cmdUsageSummary()

	case "/connect":
		return m.cmdConnect()

	case "/config":
		return m.cmdConfig(args)

	default:
		m.addSystemLine(fmt.Sprintf("Unknown command: %s — type /help for a list", cmd))
	}

	return nil
}

func (m *Model) cmdListTickets(filter string) tea.Cmd {
	return func() tea.Msg {
		tickets, err := m.client.ListTickets("")
		if err != nil {
			return errMsg{err}
		}
		var lines []string
		for _, t := range tickets {
			if filter == "active" && (t.Status == "closed") {
				continue
			}
			if filter != "" && filter != "active" && t.Status != filter {
				continue
			}
			badge := ""
			if cf := t.CustomFields; cf != nil {
				if _, ok := cf["pending_approval"]; ok {
					badge = " ⚠"
				}
			}
			lines = append(lines, fmt.Sprintf(
				"  %s  %-30s  %s%s",
				t.ID, t.Status, t.Summary, badge,
			))
		}
		if len(lines) == 0 {
			return sysMsg("No tickets found")
		}
		return sysMsg("Tickets:\n" + strings.Join(lines, "\n"))
	}
}

func (m *Model) cmdShowTicket(id string) tea.Cmd {
	return func() tea.Msg {
		t, err := m.client.GetTicket(id)
		if err != nil {
			return errMsg{err}
		}
		var sb strings.Builder
		sb.WriteString(fmt.Sprintf("Ticket %s [%s]\n", t.ID, t.Status))
		sb.WriteString(fmt.Sprintf("  Summary: %s\n", t.Summary))
		if t.Description != "" {
			sb.WriteString(fmt.Sprintf("  Description: %s\n", t.Description))
		}
		if t.PrevStatus != nil {
			sb.WriteString(fmt.Sprintf("  Previous status: %s\n", *t.PrevStatus))
		}
		if len(t.Comments) > 0 {
			sb.WriteString(fmt.Sprintf("  Comments: %d\n", len(t.Comments)))
			last := t.Comments[len(t.Comments)-1]
			sb.WriteString(fmt.Sprintf("  Last: [%s] %s", last.Author, truncate(last.Body, 120)))
		}
		return sysMsg(sb.String())
	}
}

func (m *Model) cmdSubmit(args []string) tea.Cmd {
	if len(args) == 0 {
		m.addSystemLine("Usage: /submit <summary> [-- description]")
		return nil
	}

	text := strings.Join(args, " ")
	summary := text
	description := ""
	if idx := strings.Index(text, " -- "); idx >= 0 {
		summary = text[:idx]
		description = text[idx+4:]
	}

	return func() tea.Msg {
		t, err := m.client.CreateTicket(summary, description, nil)
		if err != nil {
			return errMsg{err}
		}
		_, transErr := m.client.TransitionTicket(t.ID, "triage_pending", "Submitted via TUI")
		if transErr != nil {
			return sysMsg(fmt.Sprintf("Created %s but transition failed: %v", t.ID, transErr))
		}
		return sysMsg(fmt.Sprintf("Created %s → triage_pending", t.ID))
	}
}

func (m *Model) cmdAbort(id string) tea.Cmd {
	return func() tea.Msg {
		_, err := m.client.AddComment(id, "user", "Aborting ticket via TUI")
		if err != nil {
			return errMsg{err}
		}
		_, err = m.client.TransitionTicket(id, "awaiting_teardown", "User abort via TUI")
		if err != nil {
			return errMsg{fmt.Errorf("abort transition failed: %w", err)}
		}
		return sysMsg(fmt.Sprintf("%s → awaiting_teardown (aborted)", id))
	}
}

func (m *Model) cmdRetry(id string) tea.Cmd {
	return func() tea.Msg {
		ti, err := m.client.GetTransitions(id)
		if err != nil {
			if api.IsNotFound(err) {
				return sysMsg(fmt.Sprintf("Transitions endpoint not available — retry %s manually", id))
			}
			return errMsg{err}
		}

		if len(ti.Valid) == 0 {
			return sysMsg(fmt.Sprintf("%s [%s]: no valid transitions", id, ti.Current))
		}

		if len(ti.Valid) == 1 {
			target := ti.Valid[0]
			_, err := m.client.TransitionTicket(id, target, "Retry via TUI")
			if err != nil {
				return errMsg{err}
			}
			return sysMsg(fmt.Sprintf("%s → %s (retried)", id, target))
		}

		var lines []string
		lines = append(lines, fmt.Sprintf("Retry %s — choose target status:", id))
		for i, s := range ti.Valid {
			lines = append(lines, fmt.Sprintf("  %d. %s", i+1, s))
		}
		lines = append(lines, "Use: /retry-to <ticket_id> <status>")
		return sysMsg(strings.Join(lines, "\n"))
	}
}

func (m *Model) cmdStop(args []string) tea.Cmd {
	id := m.ticketID
	mode := "graceful"

	for _, a := range args {
		if a == "--hard" {
			mode = "hard"
		} else {
			id = a
		}
	}
	if id == "" {
		m.addSystemLine("Usage: /stop [ticket_id] [--hard]")
		return nil
	}

	return func() tea.Msg {
		err := m.client.StopTicket(id, mode)
		if err != nil {
			return errMsg{err}
		}
		return sysMsg(fmt.Sprintf("Stop requested for %s (mode=%s)", id, mode))
	}
}

func (m *Model) cmdLogs(id, agent string) tea.Cmd {
	return func() tea.Msg {
		evts, _, err := m.client.GetEvents(id, 0, 200)
		if err != nil {
			return errMsg{err}
		}
		var lines []string
		for _, e := range evts {
			if agent != "" && e.Agent != agent {
				continue
			}
			lines = append(lines, fmt.Sprintf(
				"  [%d] %s %s: %s",
				e.Seq, e.Timestamp[:19], e.Agent, e.EventType,
			))
		}
		if len(lines) == 0 {
			return sysMsg(fmt.Sprintf("No events for %s", id))
		}
		header := fmt.Sprintf("Events for %s (%d):", id, len(lines))
		return sysMsg(header + "\n" + strings.Join(lines, "\n"))
	}
}

func (m *Model) cmdUsageTicket(id string) tea.Cmd {
	return func() tea.Msg {
		u, err := m.client.GetUsage(id)
		if err != nil {
			return errMsg{err}
		}
		var sb strings.Builder
		sb.WriteString(fmt.Sprintf("Usage for %s:\n", id))
		sb.WriteString(fmt.Sprintf("  Cost: $%.4f\n", u.EstimatedCostUSD))
		if usage := u.Usage; usage != nil {
			if v, ok := usage["total_tokens"]; ok {
				sb.WriteString(fmt.Sprintf("  Tokens: %v\n", v))
			}
			if v, ok := usage["llm_calls"]; ok {
				sb.WriteString(fmt.Sprintf("  LLM calls: %v\n", v))
			}
		}
		return sysMsg(sb.String())
	}
}

func (m *Model) cmdUsageSummary() tea.Cmd {
	return func() tea.Msg {
		u, err := m.client.GetUsageSummary()
		if err != nil {
			return errMsg{err}
		}
		var sb strings.Builder
		sb.WriteString("Usage summary:\n")
		if g := u.Global; g != nil {
			if v, ok := g["total_tokens"]; ok {
				sb.WriteString(fmt.Sprintf("  Total tokens: %v\n", v))
			}
			if v, ok := g["estimated_cost_usd"]; ok {
				sb.WriteString(fmt.Sprintf("  Total cost: $%v\n", v))
			}
		}
		if len(u.ByTicket) > 0 {
			sb.WriteString(fmt.Sprintf("  Tickets with usage: %d\n", len(u.ByTicket)))
		}
		return sysMsg(sb.String())
	}
}

type sysMsg string

func (m *Model) cmdFollow(args []string) tea.Cmd {
	if len(args) > 0 {
		m.switchFollow(args[0])
		return nil
	}

	return func() tea.Msg {
		tickets, err := m.client.ListTickets("")
		if err != nil {
			return errMsg{err}
		}
		var active []api.Ticket
		for _, t := range tickets {
			if t.Status != "closed" {
				active = append(active, t)
			}
		}
		if len(active) == 0 {
			return sysMsg("No active tickets to follow")
		}
		if len(active) == 1 {
			return followMsg(active[0].ID)
		}
		var lines []string
		lines = append(lines, "Active tickets — use /follow <id> to select:")
		for _, t := range active {
			lines = append(lines, fmt.Sprintf("  %s  %-30s  %s", t.ID, t.Status, t.Summary))
		}
		return sysMsg(strings.Join(lines, "\n"))
	}
}

type followMsg string

func (m *Model) switchFollow(id string) {
	m.ticketID = id
	if m.source != nil {
		m.source.Close()
	}
	m.lines = nil
	m.source = nil
	m.addSystemLine(fmt.Sprintf("Following %s", id))
}

func (m *Model) cmdConnect() tea.Cmd {
	m.addSystemLine("Reconnecting...")
	if m.source != nil {
		m.source.Close()
		m.source = nil
	}
	m.conn = connConnecting
	return connectCmd(m.client)
}

func (m *Model) cmdConfig(args []string) tea.Cmd {
	if len(args) == 0 {
		m.addSystemLine(fmt.Sprintf("Config file: %s", config.Path()))
		m.addSystemLine(fmt.Sprintf("  url: %s", m.client.BaseURL()))
		m.addSystemLine("  token: ****")
		return nil
	}

	if len(args) < 2 {
		m.addSystemLine("Usage: /config <key> <value>")
		m.addSystemLine("  Keys: url, token")
		return nil
	}

	key := args[0]
	value := strings.Join(args[1:], " ")

	cfg := config.Load("", "")
	switch key {
	case "url":
		cfg.URL = value
	case "token":
		cfg.Token = value
	default:
		m.addSystemLine(fmt.Sprintf("Unknown config key: %s (valid: url, token)", key))
		return nil
	}

	if err := config.Write(cfg); err != nil {
		m.addSystemLine(fmt.Sprintf("Failed to save config: %v", err))
		return nil
	}

	m.client = api.New(cfg.URL, cfg.Token)
	m.addSystemLine(fmt.Sprintf("Config updated: %s = %s", key, value))
	m.addSystemLine("Use /connect to apply the new settings")
	return nil
}

func truncate(s string, n int) string {
	if len(s) > n {
		return s[:n-3] + "..."
	}
	return s
}
