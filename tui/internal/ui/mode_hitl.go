package ui

import (
	"fmt"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
)

func (m *Model) enterHITL(ticketID string) {
	m.mode = ModeHITL
	m.input.Placeholder = "Reply to agent (Enter to send, Esc to cancel)..."
	m.input.Focus()
	m.notifyHITL(ticketID)
}

func (m *Model) sendHITLReply(text string) tea.Cmd {
	id := m.ticketID
	return func() tea.Msg {
		if id == "" {
			return errMsg{fmt.Errorf("no ticket for HITL reply")}
		}

		_, err := m.client.AddComment(id, "user", text)
		if err != nil {
			return errMsg{fmt.Errorf("HITL comment failed: %w", err)}
		}

		t, err := m.client.GetTicket(id)
		if err != nil {
			return errMsg{fmt.Errorf("HITL get ticket failed: %w", err)}
		}

		target := ""
		if t.PrevStatus != nil {
			target = *t.PrevStatus
		}
		if target == "" {
			return errMsg{fmt.Errorf("no previous status to resume to")}
		}

		_, err = m.client.TransitionTicket(id, target, "User replied via TUI")
		if err != nil {
			return errMsg{fmt.Errorf("HITL transition failed: %w", err)}
		}

		return sysMsg(fmt.Sprintf("Replied to %s → %s", id, target))
	}
}

func (m *Model) checkHITLTrigger(ticketID, status string) {
	if ticketID != m.ticketID {
		return
	}
	if status == "awaiting_customer_guidance" {
		if m.mode != ModeHITL {
			m.enterHITL(ticketID)
		}
		return
	}
	// Ticket left guidance — someone else replied (web UI, cli.py).
	if m.mode == ModeHITL {
		m.mode = ModeNormal
		m.input.Blur()
		m.input.Placeholder = "Type / for commands, Esc to interject..."
		m.addSystemLine(fmt.Sprintf("HITL resolved externally → %s", status))
	}
}

func (m *Model) checkHITLFromEvent(line string) {
	if strings.Contains(line, "**Input needed:**") && m.mode != ModeHITL {
		m.enterHITL(m.ticketID)
	}
}
