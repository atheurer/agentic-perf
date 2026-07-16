// Package mockserver provides an httptest-based mock of the
// agentic-perf state store API for testing and demos.
package mockserver

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"time"
)

type Event struct {
	Seq       int                    `json:"seq"`
	Timestamp string                 `json:"timestamp"`
	TicketID  string                 `json:"ticket_id"`
	Agent     string                 `json:"agent"`
	EventType string                 `json:"event_type"`
	Data      map[string]interface{} `json:"data"`
}

type Ticket struct {
	ID           string                 `json:"id"`
	Summary      string                 `json:"summary"`
	Description  string                 `json:"description"`
	Status       string                 `json:"status"`
	CustomFields map[string]interface{} `json:"custom_fields"`
	Comments     []interface{}          `json:"comments"`
	CreatedAt    string                 `json:"created_at"`
	UpdatedAt    string                 `json:"updated_at"`
}

type MockServer struct {
	Server  *httptest.Server
	mu      sync.Mutex
	events  map[string][]Event
	tickets map[string]*Ticket
	sseOn   bool
}

func New() *MockServer {
	m := &MockServer{
		events:  make(map[string][]Event),
		tickets: make(map[string]*Ticket),
		sseOn:   true,
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/health", m.handleHealth)
	mux.HandleFunc("/api/v1/tickets", m.handleTickets)
	mux.HandleFunc("/api/v1/tickets/", m.handleTicketByID)
	mux.HandleFunc("/api/v1/events/stream", m.handleSSE)
	mux.HandleFunc("/api/v1/usage/summary", m.handleUsageSummary)

	m.Server = httptest.NewServer(mux)
	return m
}

func (m *MockServer) Close() {
	m.Server.Close()
}

func (m *MockServer) URL() string {
	return m.Server.URL
}

func (m *MockServer) DisableSSE() {
	m.mu.Lock()
	m.sseOn = false
	m.mu.Unlock()
}

func (m *MockServer) EnableSSE() {
	m.mu.Lock()
	m.sseOn = true
	m.mu.Unlock()
}

func (m *MockServer) AddTicket(t Ticket) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if t.CreatedAt == "" {
		t.CreatedAt = time.Now().UTC().Format(time.RFC3339)
	}
	if t.UpdatedAt == "" {
		t.UpdatedAt = t.CreatedAt
	}
	if t.CustomFields == nil {
		t.CustomFields = map[string]interface{}{}
	}
	m.tickets[t.ID] = &t
}

func (m *MockServer) AddEvent(ticketID string, e Event) {
	m.mu.Lock()
	defer m.mu.Unlock()
	e.TicketID = ticketID
	if e.Timestamp == "" {
		e.Timestamp = time.Now().UTC().Format(time.RFC3339)
	}
	m.events[ticketID] = append(m.events[ticketID], e)
}

func (m *MockServer) handleHealth(w http.ResponseWriter, _ *http.Request) {
	json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

func (m *MockServer) handleTickets(w http.ResponseWriter, r *http.Request) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var tickets []Ticket
	for _, t := range m.tickets {
		tickets = append(tickets, *t)
	}
	json.NewEncoder(w).Encode(tickets)
}

func (m *MockServer) handleTicketByID(w http.ResponseWriter, r *http.Request) {
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/api/v1/tickets/"), "/")
	ticketID := parts[0]

	m.mu.Lock()
	t, ok := m.tickets[ticketID]
	m.mu.Unlock()
	if !ok {
		w.WriteHeader(404)
		json.NewEncoder(w).Encode(map[string]string{"detail": "not found"})
		return
	}

	if len(parts) > 1 {
		switch parts[1] {
		case "events":
			m.handleTicketEvents(w, r, ticketID)
		case "transitions":
			m.handleTicketTransitions(w, ticketID)
		case "interject":
			m.handleTicketInterject(w, r, ticketID)
		default:
			json.NewEncoder(w).Encode(t)
		}
		return
	}

	json.NewEncoder(w).Encode(t)
}

func (m *MockServer) handleTicketEvents(w http.ResponseWriter, _ *http.Request, ticketID string) {
	m.mu.Lock()
	events := m.events[ticketID]
	m.mu.Unlock()
	json.NewEncoder(w).Encode(map[string]interface{}{
		"events":     events,
		"latest_seq": len(events),
	})
}

func (m *MockServer) handleTicketTransitions(w http.ResponseWriter, ticketID string) {
	m.mu.Lock()
	t := m.tickets[ticketID]
	m.mu.Unlock()
	json.NewEncoder(w).Encode(map[string]interface{}{
		"current": t.Status,
		"valid":   []string{},
	})
}

func (m *MockServer) handleTicketInterject(w http.ResponseWriter, r *http.Request, ticketID string) {
	if r.Method != "POST" {
		w.WriteHeader(405)
		return
	}
	var body struct {
		Message string `json:"message"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":    "queued",
		"ticket_id": ticketID,
	})
}

func (m *MockServer) handleSSE(w http.ResponseWriter, r *http.Request) {
	m.mu.Lock()
	sseOn := m.sseOn
	m.mu.Unlock()

	if !sseOn {
		w.WriteHeader(404)
		return
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.WriteHeader(200)

	ticketID := r.URL.Query().Get("ticket_id")

	m.mu.Lock()
	var events []Event
	if ticketID != "" {
		events = m.events[ticketID]
	} else {
		for _, evts := range m.events {
			events = append(events, evts...)
		}
	}
	m.mu.Unlock()

	flusher, _ := w.(http.Flusher)
	for _, e := range events {
		data, _ := json.Marshal(e)
		fmt.Fprintf(w, "id: %s:%d\nevent: %s\ndata: %s\n\n",
			e.TicketID, e.Seq, e.EventType, data)
		if flusher != nil {
			flusher.Flush()
		}
	}
}

func (m *MockServer) handleUsageSummary(w http.ResponseWriter, _ *http.Request) {
	json.NewEncoder(w).Encode(map[string]interface{}{
		"global":    map[string]interface{}{"total_tokens": 0},
		"by_ticket": map[string]interface{}{},
	})
}
