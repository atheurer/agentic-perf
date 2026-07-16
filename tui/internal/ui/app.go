// Package ui implements the bubbletea terminal UI.
package ui

import (
	"fmt"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/textinput"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"

	"github.com/atheurer/agentic-perf/tui/internal/api"
	"github.com/atheurer/agentic-perf/tui/internal/events"
	"github.com/atheurer/agentic-perf/tui/internal/stream"
)

type Mode int

const (
	ModeNormal Mode = iota
	ModeHITL
	ModeApproval
	ModeInterject
)

type connState int

const (
	connConnecting connState = iota
	connConnected
	connDisconnected
)

type Model struct {
	client      *api.Client
	source      stream.Source
	viewport    viewport.Model
	input       textinput.Model
	width       int
	height      int
	lines       []events.Line
	mode        Mode
	conn        connState
	ticketID    string
	verbose     bool
	quitting    bool
	confirmQuit bool
	err         error
}

type tickMsg time.Time

type eventMsg events.Line

type connMsg connState

func tickCmd() tea.Cmd {
	return tea.Tick(time.Second, func(t time.Time) tea.Msg {
		return tickMsg(t)
	})
}

func New(client *api.Client, ticketID string) Model {
	vp := viewport.New(80, 20)
	vp.SetContent("")

	ti := textinput.New()
	ti.Placeholder = "Type / for commands, Esc to interject..."
	ti.CharLimit = 4096

	return Model{
		client:   client,
		viewport: vp,
		input:    ti,
		ticketID: ticketID,
		conn:     connConnecting,
	}
}

func (m Model) Init() tea.Cmd {
	return tea.Batch(
		tea.EnterAltScreen,
		tickCmd(),
		m.connectCmd(),
	)
}

func (m Model) connectCmd() tea.Cmd {
	return func() tea.Msg {
		if m.client == nil {
			return connMsg(connDisconnected)
		}
		err := m.client.Health()
		if err != nil {
			return connMsg(connDisconnected)
		}
		return connMsg(connConnected)
	}
}

func (m Model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	var cmds []tea.Cmd

	switch msg := msg.(type) {
	case tea.KeyMsg:
		return m.handleKey(msg)

	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		m.updateLayout()

	case tickMsg:
		cmds = append(cmds, tickCmd())
		cmds = append(cmds, m.drainEvents())

	case eventMsg:
		line := events.Line(msg)
		m.lines = append(m.lines, line)
		m.updateViewportContent()

	case connMsg:
		m.conn = connState(msg)
		if m.conn == connConnected && m.source == nil {
			m.source = stream.NewSSE(m.client, m.ticketID)
		}

	case errMsg:
		m.err = msg.err
	}

	return m, tea.Batch(cmds...)
}

type errMsg struct{ err error }

func (m *Model) handleKey(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch msg.String() {
	case "ctrl+c":
		if m.confirmQuit {
			m.quitting = true
			if m.source != nil {
				m.source.Close()
			}
			return m, tea.Quit
		}
		m.confirmQuit = true
		return m, nil

	case "q":
		if m.mode == ModeNormal && !m.input.Focused() {
			m.quitting = true
			if m.source != nil {
				m.source.Close()
			}
			return m, tea.Quit
		}

	case "esc":
		if m.mode == ModeInterject || m.mode == ModeHITL {
			m.mode = ModeNormal
			m.input.Blur()
			return m, nil
		}
		if m.mode == ModeNormal && m.conn == connConnected {
			m.mode = ModeInterject
			m.input.Placeholder = "Interjection message (Enter to send, Esc to cancel)..."
			m.input.Focus()
			return m, nil
		}

	case "enter":
		if m.mode == ModeInterject {
			text := m.input.Value()
			if text != "" {
				cmd := m.sendInterject(text)
				m.input.SetValue("")
				m.mode = ModeNormal
				m.input.Blur()
				m.input.Placeholder = "Type / for commands, Esc to interject..."
				return m, cmd
			}
			return m, nil
		}
		if m.input.Focused() {
			text := m.input.Value()
			if strings.HasPrefix(text, "/") {
				cmd := m.handleCommand(text)
				m.input.SetValue("")
				return m, cmd
			}
		}

	case "/":
		if m.mode == ModeNormal && !m.input.Focused() {
			m.input.Focus()
			m.input.SetValue("/")
			return m, nil
		}

	case "v":
		if m.mode == ModeNormal && !m.input.Focused() {
			m.verbose = !m.verbose
			m.updateViewportContent()
			return m, nil
		}

	case "G":
		if m.mode == ModeNormal && !m.input.Focused() {
			m.viewport.GotoBottom()
			return m, nil
		}

	case "g":
		if m.mode == ModeNormal && !m.input.Focused() {
			m.viewport.GotoTop()
			return m, nil
		}
	}

	m.confirmQuit = false

	if m.input.Focused() {
		var cmd tea.Cmd
		m.input, cmd = m.input.Update(msg)
		return m, cmd
	}

	var cmd tea.Cmd
	m.viewport, cmd = m.viewport.Update(msg)
	return m, cmd
}

func (m Model) sendInterject(message string) tea.Cmd {
	return func() tea.Msg {
		if m.ticketID == "" {
			return errMsg{fmt.Errorf("no ticket to interject")}
		}
		err := m.client.Interject(m.ticketID, message)
		if err != nil {
			return errMsg{err}
		}
		return nil
	}
}

func (m *Model) handleCommand(text string) tea.Cmd {
	parts := strings.Fields(text)
	if len(parts) == 0 {
		return nil
	}

	switch parts[0] {
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
		m.addSystemLine("Commands: /quit /verbose /tickets /ticket ID /follow ID /help")

	case "/tickets":
		return m.listTicketsCmd()

	case "/ticket":
		if len(parts) > 1 {
			return m.showTicketCmd(parts[1])
		}
		m.addSystemLine("Usage: /ticket <id>")

	case "/follow":
		if len(parts) > 1 {
			m.ticketID = parts[1]
			if m.source != nil {
				m.source.Close()
			}
			m.source = stream.NewSSE(m.client, m.ticketID)
			m.addSystemLine(fmt.Sprintf("Following %s", m.ticketID))
		} else {
			m.addSystemLine("Usage: /follow <ticket_id>")
		}

	default:
		m.addSystemLine(fmt.Sprintf("Unknown command: %s", parts[0]))
	}

	return nil
}

func (m *Model) addSystemLine(text string) {
	m.lines = append(m.lines, events.Line{
		Type: "system",
		Text: text,
	})
	m.updateViewportContent()
}

func (m Model) listTicketsCmd() tea.Cmd {
	return func() tea.Msg {
		tickets, err := m.client.ListTickets("")
		if err != nil {
			return errMsg{err}
		}
		var lines []string
		for _, t := range tickets {
			lines = append(lines, fmt.Sprintf("  %s  %-30s  %s", t.ID, t.Status, t.Summary))
		}
		if len(lines) == 0 {
			return eventMsg(events.Line{Type: "system", Text: "No tickets found"})
		}
		text := "Tickets:\n" + strings.Join(lines, "\n")
		return eventMsg(events.Line{Type: "system", Text: text})
	}
}

func (m Model) showTicketCmd(id string) tea.Cmd {
	return func() tea.Msg {
		t, err := m.client.GetTicket(id)
		if err != nil {
			return errMsg{err}
		}
		text := fmt.Sprintf("Ticket %s [%s]\n  %s\n  %s", t.ID, t.Status, t.Summary, t.Description)
		return eventMsg(events.Line{Type: "system", Text: text})
	}
}

func (m *Model) drainEvents() tea.Cmd {
	if m.source == nil {
		return nil
	}
	return func() tea.Msg {
		select {
		case evt, ok := <-m.source.Events():
			if !ok {
				return connMsg(connDisconnected)
			}
			return eventMsg(events.Normalize(evt))
		default:
			return nil
		}
	}
}

func (m *Model) updateLayout() {
	headerHeight := 1
	inputHeight := 1
	statusHeight := 1
	vpHeight := m.height - headerHeight - inputHeight - statusHeight
	if vpHeight < 1 {
		vpHeight = 1
	}
	m.viewport.Width = m.width
	m.viewport.Height = vpHeight
	m.input.Width = m.width - 2
	m.updateViewportContent()
}

func (m *Model) updateViewportContent() {
	var sb strings.Builder
	for _, l := range m.lines {
		text := RenderLine(l, m.verbose)
		if text == "" {
			continue
		}
		sb.WriteString(text)
		sb.WriteString("\n")
	}
	m.viewport.SetContent(sb.String())
	m.viewport.GotoBottom()
}

func (m Model) View() string {
	if m.quitting {
		return ""
	}

	header := m.renderHeader()
	status := m.renderStatusBar()
	content := m.viewport.View()
	input := m.input.View()

	return lipgloss.JoinVertical(lipgloss.Left,
		header,
		content,
		input,
		status,
	)
}

func (m Model) renderHeader() string {
	style := lipgloss.NewStyle().
		Bold(true).
		Foreground(lipgloss.Color("12"))

	title := "aptui"
	if m.ticketID != "" {
		title += " — " + m.ticketID
	}
	return style.Render(title)
}

func (m Model) renderStatusBar() string {
	style := lipgloss.NewStyle().
		Background(lipgloss.Color("235")).
		Foreground(lipgloss.Color("252")).
		Width(m.width)

	var parts []string

	switch m.conn {
	case connConnecting:
		parts = append(parts, "⟳ connecting")
	case connConnected:
		transport := "poll"
		if m.source != nil {
			transport = m.source.Transport()
		}
		parts = append(parts, fmt.Sprintf("● %s", transport))
	case connDisconnected:
		parts = append(parts, "○ disconnected")
	}

	switch m.mode {
	case ModeInterject:
		parts = append(parts, "[INTERJECT]")
	case ModeHITL:
		parts = append(parts, "🔔 [HITL]")
	case ModeApproval:
		parts = append(parts, "[APPROVAL]")
	}

	if m.verbose {
		parts = append(parts, "verbose")
	}

	if m.confirmQuit {
		parts = append(parts, "Press Ctrl+C again to quit")
	}

	if m.err != nil {
		parts = append(parts, fmt.Sprintf("err: %s", m.err))
	}

	return style.Render(" " + strings.Join(parts, " │ "))
}
