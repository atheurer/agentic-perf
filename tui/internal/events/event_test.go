package events

import "testing"

func TestNormalizeToolCalled(t *testing.T) {
	raw := map[string]interface{}{
		"seq":        float64(5),
		"ticket_id":  "PERF-001",
		"agent":      "benchmark",
		"event_type": "tool_called",
		"data": map[string]interface{}{
			"tool": "run_command",
		},
	}
	l := Normalize(raw)
	if l.Seq != 5 {
		t.Errorf("seq: got %d", l.Seq)
	}
	if l.Type != "tool_called" {
		t.Errorf("type: got %q", l.Type)
	}
	if l.Text != "→ run_command()" {
		t.Errorf("text: got %q", l.Text)
	}
}

func TestNormalizeToolResult(t *testing.T) {
	raw := map[string]interface{}{
		"event_type": "tool_result",
		"data": map[string]interface{}{
			"tool":     "list_benchmarks",
			"is_error": false,
		},
	}
	l := Normalize(raw)
	if l.Text != "✓ list_benchmarks" {
		t.Errorf("text: got %q", l.Text)
	}
}

func TestNormalizeToolResultError(t *testing.T) {
	raw := map[string]interface{}{
		"event_type": "tool_result",
		"data": map[string]interface{}{
			"tool":     "run_command",
			"is_error": true,
		},
	}
	l := Normalize(raw)
	if l.Text != "✗ run_command" {
		t.Errorf("text: got %q", l.Text)
	}
}

func TestNormalizeTransition(t *testing.T) {
	raw := map[string]interface{}{
		"event_type": "transition",
		"data": map[string]interface{}{
			"to":      "awaiting_review",
			"comment": "benchmark complete",
		},
	}
	l := Normalize(raw)
	if l.Text != "── awaiting_review (benchmark complete)" {
		t.Errorf("text: got %q", l.Text)
	}
}

func TestNormalizeLLMRequestEmpty(t *testing.T) {
	raw := map[string]interface{}{
		"event_type": "llm_request",
		"data":       map[string]interface{}{},
	}
	l := Normalize(raw)
	if l.Text != "" {
		t.Errorf("llm_request should be empty, got %q", l.Text)
	}
}

func TestNormalizeUserInterjection(t *testing.T) {
	raw := map[string]interface{}{
		"event_type": "user_interjection",
		"data": map[string]interface{}{
			"message": "focus on latency",
		},
	}
	l := Normalize(raw)
	if l.Text != "[user] focus on latency" {
		t.Errorf("text: got %q", l.Text)
	}
}

func TestNormalizeAgentStopped(t *testing.T) {
	raw := map[string]interface{}{
		"event_type": "agent_stopped",
		"agent":      "benchmark",
		"data": map[string]interface{}{
			"mode": "graceful",
		},
	}
	l := Normalize(raw)
	if l.Text != "[benchmark] stopped (graceful)" {
		t.Errorf("text: got %q", l.Text)
	}
}

func TestNormalizeMissingData(t *testing.T) {
	raw := map[string]interface{}{
		"event_type": "tool_called",
	}
	l := Normalize(raw)
	if l.Text != "→ ()" {
		t.Errorf("text: got %q", l.Text)
	}
}
