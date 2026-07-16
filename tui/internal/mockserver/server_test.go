package mockserver

import (
	"encoding/json"
	"net/http"
	"testing"

	"github.com/atheurer/agentic-perf/tui/internal/api"
	"github.com/atheurer/agentic-perf/tui/internal/stream"
)

func TestMockServerHealth(t *testing.T) {
	m := New()
	defer m.Close()

	c := api.New(m.URL(), "")
	if err := c.Health(); err != nil {
		t.Fatalf("Health: %v", err)
	}
}

func TestMockServerTickets(t *testing.T) {
	m := New()
	defer m.Close()

	m.AddTicket(Ticket{
		ID:      "PERF-001",
		Summary: "test",
		Status:  "new",
	})

	c := api.New(m.URL(), "")
	ticket, err := c.GetTicket("PERF-001")
	if err != nil {
		t.Fatalf("GetTicket: %v", err)
	}
	if ticket.ID != "PERF-001" {
		t.Errorf("ID: %q", ticket.ID)
	}
}

func TestMockServerEvents(t *testing.T) {
	m := New()
	defer m.Close()

	m.AddTicket(Ticket{ID: "PERF-001", Summary: "t", Status: "new"})
	m.AddEvent("PERF-001", Event{Seq: 1, Agent: "triage", EventType: "agent_started"})
	m.AddEvent("PERF-001", Event{Seq: 2, Agent: "triage", EventType: "agent_finished"})

	c := api.New(m.URL(), "")
	events, _, err := c.GetEvents("PERF-001", 0, 200)
	if err != nil {
		t.Fatalf("GetEvents: %v", err)
	}
	if len(events) != 2 {
		t.Errorf("expected 2 events, got %d", len(events))
	}
}

func TestMockServerSSE(t *testing.T) {
	m := New()
	defer m.Close()

	m.AddTicket(Ticket{ID: "PERF-001", Summary: "t", Status: "new"})
	m.AddEvent("PERF-001", Event{Seq: 1, Agent: "triage", EventType: "agent_started"})

	var events []struct{ eventType string }
	err := stream.ConnectSSE(m.URL()+"/api/v1/events/stream?ticket_id=PERF-001", "", func(_, eventType, _ string) bool {
		events = append(events, struct{ eventType string }{eventType})
		return true
	})
	if err != nil {
		t.Fatalf("ConnectSSE: %v", err)
	}
	if len(events) != 1 {
		t.Errorf("expected 1 event, got %d", len(events))
	}
}

func TestMockServerSSEDisabled(t *testing.T) {
	m := New()
	defer m.Close()

	m.DisableSSE()

	resp, err := http.Get(m.URL() + "/api/v1/events/stream")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 404 {
		t.Errorf("expected 404, got %d", resp.StatusCode)
	}
}

func TestMockServerInterject(t *testing.T) {
	m := New()
	defer m.Close()

	m.AddTicket(Ticket{ID: "PERF-001", Summary: "t", Status: "executing_benchmark"})

	c := api.New(m.URL(), "")
	err := c.Interject("PERF-001", "focus on latency")
	if err != nil {
		t.Fatalf("Interject: %v", err)
	}
}

func TestMockServerTransitions(t *testing.T) {
	m := New()
	defer m.Close()

	m.AddTicket(Ticket{ID: "PERF-001", Summary: "t", Status: "executing_benchmark"})

	c := api.New(m.URL(), "")
	ti, err := c.GetTransitions("PERF-001")
	if err != nil {
		t.Fatalf("GetTransitions: %v", err)
	}
	if ti.Current != "executing_benchmark" {
		t.Errorf("current: %q", ti.Current)
	}
}

func TestMockServerNotFound(t *testing.T) {
	m := New()
	defer m.Close()

	c := api.New(m.URL(), "")
	_, err := c.GetTicket("PERF-NONEXISTENT")
	if err == nil {
		t.Fatal("expected error")
	}
	if !api.IsNotFound(err) {
		t.Errorf("expected 404, got: %v", err)
	}
}

func TestMockServerUsageSummary(t *testing.T) {
	m := New()
	defer m.Close()

	resp, err := http.Get(m.URL() + "/api/v1/usage/summary")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Errorf("expected 200, got %d", resp.StatusCode)
	}
	var result map[string]interface{}
	json.NewDecoder(resp.Body).Decode(&result)
	if result["global"] == nil {
		t.Error("expected global key")
	}
}
