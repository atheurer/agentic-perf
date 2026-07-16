package ui

import (
	"os"
	"strings"
	"testing"

	"github.com/atheurer/agentic-perf/tui/internal/events"
)

func TestRenderToolCalled(t *testing.T) {
	l := events.Line{
		Type:  "tool_called",
		Agent: "benchmark",
		Data:  map[string]interface{}{"tool": "run_command"},
	}
	out := RenderLine(l, false)
	if !strings.Contains(out, "run_command") {
		t.Errorf("expected tool name, got %q", out)
	}
	if !strings.Contains(out, "→") {
		t.Errorf("expected arrow, got %q", out)
	}
}

func TestRenderToolResultOK(t *testing.T) {
	l := events.Line{
		Type: "tool_result",
		Data: map[string]interface{}{"tool": "list_benchmarks", "is_error": false},
	}
	out := RenderLine(l, false)
	if !strings.Contains(out, "✓") {
		t.Errorf("expected check mark, got %q", out)
	}
}

func TestRenderToolResultError(t *testing.T) {
	l := events.Line{
		Type: "tool_result",
		Data: map[string]interface{}{"tool": "run_command", "is_error": true},
	}
	out := RenderLine(l, false)
	if !strings.Contains(out, "✗") {
		t.Errorf("expected X mark, got %q", out)
	}
}

func TestRenderTransition(t *testing.T) {
	l := events.Line{
		Type: "transition",
		Data: map[string]interface{}{"to": "awaiting_review", "comment": "done"},
	}
	out := RenderLine(l, false)
	if !strings.Contains(out, "awaiting_review") {
		t.Errorf("expected status, got %q", out)
	}
	if !strings.Contains(out, "─") {
		t.Errorf("expected divider, got %q", out)
	}
}

func TestRenderLLMHiddenByDefault(t *testing.T) {
	l := events.Line{
		Type: "llm_request",
		Data: map[string]interface{}{"iteration": float64(1)},
	}
	out := RenderLine(l, false)
	if out != "" {
		t.Errorf("llm_request should be empty without verbose, got %q", out)
	}
}

func TestRenderLLMVisibleVerbose(t *testing.T) {
	l := events.Line{
		Type: "llm_request",
		Data: map[string]interface{}{"iteration": float64(1)},
	}
	out := RenderLine(l, true)
	if out == "" {
		t.Error("llm_request should be visible in verbose mode")
	}
}

func TestRenderUserInterjection(t *testing.T) {
	l := events.Line{
		Type: "user_interjection",
		Data: map[string]interface{}{"message": "focus on latency"},
	}
	out := RenderLine(l, false)
	if !strings.Contains(out, "[user]") {
		t.Errorf("expected [user] prefix, got %q", out)
	}
	if !strings.Contains(out, "focus on latency") {
		t.Errorf("expected message, got %q", out)
	}
}

func TestRenderNoColor(t *testing.T) {
	old := noColor
	noColor = true
	defer func() { noColor = old }()

	l := events.Line{
		Type:  "agent_started",
		Agent: "triage",
		Text:  "[triage] started",
		Data:  map[string]interface{}{},
	}
	out := RenderLine(l, false)
	if strings.Contains(out, "\x1b[") {
		t.Errorf("NO_COLOR output should not contain ANSI codes: %q", out)
	}
}

func TestRenderSystem(t *testing.T) {
	l := events.Line{
		Type: "system",
		Text: "Following PERF-001",
	}
	out := RenderLine(l, false)
	if !strings.Contains(out, "Following PERF-001") {
		t.Errorf("expected system text, got %q", out)
	}
}

func TestRenderPlainFallback(t *testing.T) {
	os.Setenv("NO_COLOR", "")
	l := events.Line{
		Type: "unknown_event",
		Text: "unknown_event",
		Data: map[string]interface{}{},
	}
	out := RenderLine(l, false)
	if !strings.Contains(out, "unknown_event") {
		t.Errorf("expected type text, got %q", out)
	}
}
