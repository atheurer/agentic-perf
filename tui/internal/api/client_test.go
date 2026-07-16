package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHealthOK(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/health" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		w.WriteHeader(200)
		w.Write([]byte(`{"status":"ok"}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "test-token")
	if err := c.Health(); err != nil {
		t.Fatalf("Health() failed: %v", err)
	}
}

func TestAuthHeaderSent(t *testing.T) {
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		w.WriteHeader(200)
		w.Write([]byte(`{"status":"ok"}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "my-secret-token")
	c.Health()
	if gotAuth != "Bearer my-secret-token" {
		t.Errorf("expected bearer auth, got %q", gotAuth)
	}
}

func TestGetTicket(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]interface{}{
			"id":            "PERF-001",
			"summary":       "test ticket",
			"status":        "new",
			"custom_fields": map[string]interface{}{},
		})
	}))
	defer srv.Close()

	c := New(srv.URL, "tok")
	ticket, err := c.GetTicket("PERF-001")
	if err != nil {
		t.Fatalf("GetTicket: %v", err)
	}
	if ticket.ID != "PERF-001" {
		t.Errorf("got ID %q", ticket.ID)
	}
	if ticket.Status != "new" {
		t.Errorf("got status %q", ticket.Status)
	}
}

func TestListTickets(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		status := r.URL.Query().Get("status")
		tickets := []map[string]interface{}{
			{"id": "PERF-001", "summary": "t1", "status": "new"},
		}
		if status == "new" {
			json.NewEncoder(w).Encode(tickets)
		} else {
			json.NewEncoder(w).Encode([]map[string]interface{}{})
		}
	}))
	defer srv.Close()

	c := New(srv.URL, "tok")
	tickets, err := c.ListTickets("new")
	if err != nil {
		t.Fatalf("ListTickets: %v", err)
	}
	if len(tickets) != 1 {
		t.Errorf("expected 1 ticket, got %d", len(tickets))
	}
}

func TestErrorDiscrimination(t *testing.T) {
	tests := []struct {
		code  int
		check func(error) bool
		name  string
	}{
		{401, IsUnauthorized, "unauthorized"},
		{404, IsNotFound, "not found"},
		{409, IsConflict, "conflict"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(tc.code)
				w.Write([]byte(`{"detail":"test error"}`))
			}))
			defer srv.Close()

			c := New(srv.URL, "tok")
			_, err := c.GetTicket("PERF-001")
			if err == nil {
				t.Fatal("expected error")
			}
			if !tc.check(err) {
				t.Errorf("error check failed for %d: %v", tc.code, err)
			}
		})
	}
}

func TestRetryOn5xx(t *testing.T) {
	attempts := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		attempts++
		if attempts < 3 {
			w.WriteHeader(500)
			w.Write([]byte(`{"detail":"server error"}`))
			return
		}
		w.WriteHeader(200)
		w.Write([]byte(`{"status":"ok"}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "tok")
	err := c.Health()
	if err != nil {
		t.Fatalf("expected retry to succeed: %v", err)
	}
	if attempts != 3 {
		t.Errorf("expected 3 attempts, got %d", attempts)
	}
}

func TestInterject(t *testing.T) {
	var gotBody map[string]interface{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewDecoder(r.Body).Decode(&gotBody)
		w.WriteHeader(200)
		w.Write([]byte(`{"status":"queued"}`))
	}))
	defer srv.Close()

	c := New(srv.URL, "tok")
	err := c.Interject("PERF-001", "try latency focus")
	if err != nil {
		t.Fatalf("Interject: %v", err)
	}
	if gotBody["message"] != "try latency focus" {
		t.Errorf("unexpected body: %v", gotBody)
	}
}

func TestGetTransitions(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]interface{}{
			"current": "executing_benchmark",
			"valid":   []string{"awaiting_review", "awaiting_customer_guidance"},
		})
	}))
	defer srv.Close()

	c := New(srv.URL, "tok")
	ti, err := c.GetTransitions("PERF-001")
	if err != nil {
		t.Fatalf("GetTransitions: %v", err)
	}
	if ti.Current != "executing_benchmark" {
		t.Errorf("got current %q", ti.Current)
	}
	if len(ti.Valid) != 2 {
		t.Errorf("expected 2 valid, got %d", len(ti.Valid))
	}
}

func TestStreamURL(t *testing.T) {
	c := New("http://localhost:8090", "tok")

	url := c.StreamURL("PERF-001", "", 0)
	if url != "http://localhost:8090/api/v1/events/stream?ticket_id=PERF-001" {
		t.Errorf("unexpected URL: %s", url)
	}

	url = c.StreamURL("PERF-001", "tool_called", 5)
	expected := "http://localhost:8090/api/v1/events/stream?ticket_id=PERF-001&event_type=tool_called&since=5"
	if url != expected {
		t.Errorf("got %s, want %s", url, expected)
	}

	url = c.StreamURL("", "", 0)
	if url != "http://localhost:8090/api/v1/events/stream" {
		t.Errorf("got %s", url)
	}
}
