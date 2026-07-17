package ui

import (
	"fmt"
	"os"
	"strings"

	"github.com/charmbracelet/lipgloss"

	"github.com/atheurer/agentic-perf/tui/internal/events"
)

var noColor = os.Getenv("NO_COLOR") != "" || os.Getenv("TERM") == "dumb"

var (
	agentStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("14")).
			Bold(true)

	toolCallStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("12"))

	toolOKStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("10"))

	toolErrStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("9"))

	transitionStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("11")).
			Bold(true)

	dimStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("240"))

	systemStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("13"))

	userStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("10")).
			Bold(true)

	errorStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("9"))

	progressStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("6"))
)

func RenderLine(l events.Line, verbose bool) string {
	if noColor {
		return renderPlain(l, verbose)
	}
	return renderStyled(l, verbose)
}

func renderStyled(l events.Line, verbose bool) string {
	switch l.Type {
	case "agent_started":
		return agentStyle.Render(fmt.Sprintf("[%s]", l.Agent)) + " started"

	case "agent_finished":
		return agentStyle.Render(fmt.Sprintf("[%s]", l.Agent)) + " finished"

	case "agent_stopped":
		mode := strData(l.Data, "mode")
		return agentStyle.Render(fmt.Sprintf("[%s]", l.Agent)) +
			errorStyle.Render(fmt.Sprintf(" stopped (%s)", mode))

	case "agent_error":
		reason := strData(l.Data, "reason")
		return agentStyle.Render(fmt.Sprintf("[%s]", l.Agent)) +
			errorStyle.Render(fmt.Sprintf(" error: %s", reason))

	case "tool_called":
		tool := strData(l.Data, "tool")
		input := truncateInput(l.Data)
		if input != "" {
			return toolCallStyle.Render(fmt.Sprintf("→ %s(%s)", tool, input))
		}
		return toolCallStyle.Render(fmt.Sprintf("→ %s()", tool))

	case "tool_result":
		tool := strData(l.Data, "tool")
		isErr := l.Data["is_error"]
		if isErr == true {
			return toolErrStyle.Render(fmt.Sprintf("✗ %s", tool))
		}
		return toolOKStyle.Render(fmt.Sprintf("✓ %s", tool))

	case "tool_skipped":
		tool := strData(l.Data, "tool")
		reason := strData(l.Data, "reason")
		return dimStyle.Render(fmt.Sprintf("⊘ %s: %s", tool, reason))

	case "tool_progress":
		msg := strData(l.Data, "message")
		if msg == "" {
			msg = strData(l.Data, "body")
		}
		return progressStyle.Render(fmt.Sprintf("⏳ %s", msg))

	case "transition":
		to := strData(l.Data, "to")
		comment := strData(l.Data, "comment")
		divider := strings.Repeat("─", 3)
		if comment != "" {
			return transitionStyle.Render(fmt.Sprintf("%s %s (%s)", divider, to, comment))
		}
		return transitionStyle.Render(fmt.Sprintf("%s %s", divider, to))

	case "comment":
		body := strData(l.Data, "body")
		if len(body) > 200 {
			body = body[:197] + "..."
		}
		return dimStyle.Render(body)

	case "user_interjection":
		return userStyle.Render("[user] ") + strData(l.Data, "message")

	case "escalation":
		reason := strData(l.Data, "reason")
		return agentStyle.Render(fmt.Sprintf("[%s]", l.Agent)) +
			errorStyle.Render(fmt.Sprintf(" escalation: %s", reason))

	case "llm_request":
		if !verbose {
			return ""
		}
		iter := l.Data["iteration"]
		return dimStyle.Render(fmt.Sprintf("  llm request (iter %v)", iter))

	case "llm_response":
		if !verbose {
			return ""
		}
		return dimStyle.Render("  llm response")

	case "llm_usage":
		if !verbose {
			return ""
		}
		return dimStyle.Render("  llm usage recorded")

	case "system":
		return systemStyle.Render(l.Text)

	default:
		return dimStyle.Render(l.Type)
	}
}

func renderPlain(l events.Line, verbose bool) string {
	switch l.Type {
	case "llm_request", "llm_response", "llm_usage":
		if !verbose {
			return ""
		}
	}
	return l.Text
}

func strData(m map[string]interface{}, key string) string {
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

func truncateInput(data map[string]interface{}) string {
	input, ok := data["input"]
	if !ok {
		return ""
	}
	s := fmt.Sprintf("%v", input)
	if len(s) > 100 {
		s = s[:97] + "..."
	}
	return s
}
