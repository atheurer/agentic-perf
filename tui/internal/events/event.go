// Package events normalizes raw API events into renderable lines.
package events

import "fmt"

type Line struct {
	TicketID  string
	Seq       int
	Timestamp string
	Agent     string
	Type      string
	Text      string
	Data      map[string]interface{}
}

func Normalize(raw map[string]interface{}) Line {
	l := Line{
		TicketID:  str(raw, "ticket_id"),
		Seq:       intVal(raw, "seq"),
		Timestamp: str(raw, "timestamp"),
		Agent:     str(raw, "agent"),
		Type:      str(raw, "event_type"),
		Data:      mapVal(raw, "data"),
	}
	l.Text = renderText(l)
	return l
}

func renderText(l Line) string {
	switch l.Type {
	case "agent_started":
		return fmt.Sprintf("[%s] started", l.Agent)
	case "agent_finished":
		return fmt.Sprintf("[%s] finished", l.Agent)
	case "agent_stopped":
		mode := str(l.Data, "mode")
		return fmt.Sprintf("[%s] stopped (%s)", l.Agent, mode)
	case "agent_error":
		reason := str(l.Data, "reason")
		return fmt.Sprintf("[%s] error: %s", l.Agent, reason)
	case "tool_called":
		tool := str(l.Data, "tool")
		return fmt.Sprintf("→ %s()", tool)
	case "tool_result":
		tool := str(l.Data, "tool")
		isErr := l.Data["is_error"]
		if isErr == true {
			return fmt.Sprintf("✗ %s", tool)
		}
		return fmt.Sprintf("✓ %s", tool)
	case "tool_skipped":
		tool := str(l.Data, "tool")
		reason := str(l.Data, "reason")
		return fmt.Sprintf("⊘ %s: %s", tool, reason)
	case "tool_progress":
		msg := str(l.Data, "message")
		return fmt.Sprintf("⏳ %s", msg)
	case "transition":
		to := str(l.Data, "to")
		comment := str(l.Data, "comment")
		if comment != "" {
			return fmt.Sprintf("── %s (%s)", to, comment)
		}
		return fmt.Sprintf("── %s", to)
	case "comment":
		body := str(l.Data, "body")
		if len(body) > 120 {
			body = body[:117] + "..."
		}
		return body
	case "user_interjection":
		msg := str(l.Data, "message")
		return fmt.Sprintf("[user] %s", msg)
	case "llm_request", "llm_response":
		return ""
	case "llm_usage":
		return ""
	case "escalation":
		reason := str(l.Data, "reason")
		return fmt.Sprintf("[%s] escalation: %s", l.Agent, reason)
	default:
		return l.Type
	}
}

func str(m map[string]interface{}, key string) string {
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

func intVal(m map[string]interface{}, key string) int {
	if v, ok := m[key]; ok {
		switch n := v.(type) {
		case float64:
			return int(n)
		case int:
			return n
		}
	}
	return 0
}

func mapVal(m map[string]interface{}, key string) map[string]interface{} {
	if v, ok := m[key]; ok {
		if d, ok := v.(map[string]interface{}); ok {
			return d
		}
	}
	return map[string]interface{}{}
}
