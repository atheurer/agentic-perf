package ui

import (
	"fmt"

	tea "github.com/charmbracelet/bubbletea"
)

type pendingApproval struct {
	Agent   string `json:"agent"`
	Host    string `json:"host"`
	Binary  string `json:"binary"`
	Command string `json:"command"`
	Status  string `json:"status"`
}

func (m *Model) enterApproval(pa pendingApproval) {
	m.mode = ModeApproval
	m.pendingApproval = &pa
	m.addSystemLine(fmt.Sprintf(
		"⚠ Command approval needed:\n  Agent: %s\n  Host: %s\n  Command: %s %s\n  [a]pprove once  [t]icket-wide  [d]eny",
		pa.Agent, pa.Host, pa.Binary, pa.Command,
	))
}

func (m *Model) handleApprovalKey(key string) tea.Cmd {
	if m.pendingApproval == nil {
		m.mode = ModeNormal
		return nil
	}

	pa := m.pendingApproval
	var status string
	var label string

	switch key {
	case "a":
		status = "approved_once"
		label = "approved (once)"
	case "t":
		status = "approved_ticket"
		label = "approved (ticket-wide)"
	case "d":
		status = "denied"
		label = "denied"
	default:
		return nil
	}

	m.mode = ModeNormal
	m.pendingApproval = nil

	return func() tea.Msg {
		fields := map[string]interface{}{
			"pending_approval": map[string]interface{}{
				"agent":   pa.Agent,
				"host":    pa.Host,
				"binary":  pa.Binary,
				"command": pa.Command,
				"status":  status,
			},
		}

		if status == "approved_ticket" {
			fields["command_approvals"] = []map[string]interface{}{
				{
					"binary":  pa.Binary,
					"command": pa.Command,
					"host":    pa.Host,
				},
			}
		}

		_, err := m.client.UpdateFields(m.ticketID, fields)
		if err != nil {
			return errMsg{fmt.Errorf("approval update failed: %w", err)}
		}
		return sysMsg(fmt.Sprintf("Command %s: %s %s", label, pa.Binary, pa.Command))
	}
}

func (m *Model) checkApprovalTrigger(cf map[string]interface{}) {
	paRaw, ok := cf["pending_approval"]
	if !ok {
		return
	}
	paMap, ok := paRaw.(map[string]interface{})
	if !ok {
		return
	}
	status, _ := paMap["status"].(string)
	if status != "pending" {
		return
	}
	if m.mode == ModeApproval {
		return
	}

	pa := pendingApproval{
		Agent:   strFromMap(paMap, "agent"),
		Host:    strFromMap(paMap, "host"),
		Binary:  strFromMap(paMap, "binary"),
		Command: strFromMap(paMap, "command"),
		Status:  status,
	}
	m.enterApproval(pa)
}

func (m *Model) clearApprovalIfResolved(eventType string) {
	if m.mode != ModeApproval {
		return
	}
	// A transition or new tool call means the approval was
	// handled externally (web UI, cli.py) and the agent moved on.
	if eventType == "transition" || eventType == "tool_called" {
		m.mode = ModeNormal
		m.pendingApproval = nil
		m.addSystemLine("Approval resolved externally")
	}
}

func strFromMap(m map[string]interface{}, key string) string {
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}
