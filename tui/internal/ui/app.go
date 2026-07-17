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
	client          *api.Client
	source          stream.Source
	viewport        viewport.Model
	input           textinput.Model
	width           int
	height          int
	lines           []events.Line
	mode            Mode
	conn            connState
	ticketID        string
	verbose         bool
	plain           bool
	quitting        bool
	confirmQuit     bool
	pendingApproval *pendingApproval
	wizardStep      wizardStep
	wizardURL       string
	wizardToken     string
	needsWizard     bool
	reconnectTicks  int
	err             error
}

type tickMsg time.Time

type eventMsg events.Line

type connMsg connState

func tickCmd() tea.Cmd {
	return tea.Tick(time.Second, func(t time.Time) tea.Msg {
		return tickMsg(t)
	})
}

type Options struct {
	Client      *api.Client
	TicketID    string
	Plain       bool
	NeedsWizard bool
}

func New(opts Options) Model {
	vp := viewport.New(80, 20)
	vp.SetContent("")

	ti := textinput.New()
	ti.Placeholder = "Type / for commands, Esc to interject..."
	ti.CharLimit = 4096

	if opts.Plain {
		noColor = true
	}

	wStep := wizardDone
	if opts.NeedsWizard {
		wStep = wizardURL
	}

	return Model{
		client:      opts.Client,
		viewport:    vp,
		input:       ti,
		ticketID:    opts.TicketID,
		plain:       opts.Plain,
		needsWizard: opts.NeedsWizard,
		wizardStep:  wStep,
		conn:        connConnecting,
	}
}

type startWizardMsg struct{}

func (m Model) Init() tea.Cmd {
	cmds := []tea.Cmd{
		tea.EnterAltScreen,
		tickCmd(),
	}
	if m.needsWizard {
		cmds = append(cmds, func() tea.Msg { return startWizardMsg{} })
	} else {
		cmds = append(cmds, connectCmd(m.client))
	}
	return tea.Batch(cmds...)
}

func connectCmd(client *api.Client) tea.Cmd {
	return func() tea.Msg {
		if client == nil {
			return connMsg(connDisconnected)
		}
		err := client.Health()
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

	case startWizardMsg:
		m.startWizard()

	case tickMsg:
		cmds = append(cmds, tickCmd())
		cmds = append(cmds, m.drainEvents())
		if m.conn == connDisconnected {
			m.reconnectTicks++
			if m.reconnectTicks >= 5 {
				m.reconnectTicks = 0
				cmds = append(cmds, connectCmd(m.client))
			}
		}

	case eventMsg:
		line := events.Line(msg)
		m.appendEvent(line)

	case batchEventsMsg:
		for _, line := range msg {
			m.appendEvent(line)
		}

	case connMsg:
		prev := m.conn
		m.conn = connState(msg)
		if m.conn == connConnected {
			if prev == connDisconnected {
				m.addSystemLine("Connection restored")
			}
			m.reconnectTicks = 0
			if m.source == nil {
				if m.ticketID == "" {
					cmds = append(cmds, m.autoFollowCmd())
				} else {
					m.source = stream.NewSSE(m.client, m.ticketID)
				}
			}
		}
		if m.conn == connDisconnected && prev != connDisconnected {
			if prev == connConnected {
				m.addSystemLine("⚠ Connection lost — reconnecting...")
			} else {
				m.addSystemLine("Connection failed — retrying...")
			}
			if m.source != nil {
				m.source.Close()
				m.source = nil
			}
		}

	case errMsg:
		m.err = msg.err

	case sysMsg:
		m.addSystemLine(string(msg))

	case followMsg:
		m.switchFollow(string(msg))
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

	case "a", "t", "d":
		if m.mode == ModeApproval {
			cmd := m.handleApprovalKey(msg.String())
			return m, cmd
		}

	case "enter":
		if m.wizardStep < wizardDone {
			cmd := m.handleWizardInput()
			return m, cmd
		}
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
		if m.mode == ModeHITL {
			text := m.input.Value()
			if text != "" {
				cmd := m.sendHITLReply(text)
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
				cmd := m.dispatchCommand(text)
				m.input.SetValue("")
				m.input.Blur()
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

func (m *Model) autoFollowCmd() tea.Cmd {
	return func() tea.Msg {
		tickets, err := m.client.ListTickets("")
		if err != nil {
			return sysMsg("Connected — use /submit or /follow to get started")
		}
		var latest api.Ticket
		for _, t := range tickets {
			if t.Status == "closed" {
				continue
			}
			if latest.ID == "" || t.UpdatedAt > latest.UpdatedAt {
				latest = t
			}
		}
		if latest.ID != "" {
			return followMsg(latest.ID)
		}
		return sysMsg("Connected — no active tickets. Use /submit to create one")
	}
}

func (m *Model) appendEvent(line events.Line) {
	m.lines = append(m.lines, line)
	m.updateViewportContent()
	if line.Type == "transition" {
		to := strData(line.Data, "to")
		m.checkHITLTrigger(line.TicketID, to)
	}
	if line.Type == "comment" {
		m.checkHITLFromEvent(strData(line.Data, "body"))
	}
	m.clearApprovalIfResolved(line.Type)
}

func (m *Model) addSystemLine(text string) {
	m.lines = append(m.lines, events.Line{
		Type: "system",
		Text: text,
	})
	m.updateViewportContent()
}

type batchEventsMsg []events.Line

func (m *Model) drainEvents() tea.Cmd {
	if m.source == nil {
		return nil
	}
	return func() tea.Msg {
		ch := m.source.Events()
		var batch []events.Line
		for {
			select {
			case evt, ok := <-ch:
				if !ok {
					if len(batch) > 0 {
						return batchEventsMsg(batch)
					}
					return connMsg(connDisconnected)
				}
				batch = append(batch, events.Normalize(evt))
			default:
				if len(batch) > 0 {
					return batchEventsMsg(batch)
				}
				return nil
			}
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
