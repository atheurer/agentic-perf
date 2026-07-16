package stream

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestSSEParser(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		w.Write([]byte("id: PERF-001:1\nevent: agent_started\ndata: {\"seq\":1}\n\n"))
		w.Write([]byte(": keepalive\n\n"))
		w.Write([]byte("id: PERF-001:2\nevent: tool_called\ndata: {\"seq\":2,\"tool\":\"run\"}\n\n"))
	}))
	defer srv.Close()

	var events []struct {
		id, eventType, data string
	}

	err := ConnectSSE(srv.URL, "", func(id, eventType, data string) bool {
		events = append(events, struct{ id, eventType, data string }{id, eventType, data})
		return true
	})
	if err != nil {
		t.Fatalf("ConnectSSE: %v", err)
	}

	if len(events) != 2 {
		t.Fatalf("expected 2 events, got %d", len(events))
	}

	if events[0].id != "PERF-001:1" {
		t.Errorf("event 0 id: %q", events[0].id)
	}
	if events[0].eventType != "agent_started" {
		t.Errorf("event 0 type: %q", events[0].eventType)
	}

	if events[1].id != "PERF-001:2" {
		t.Errorf("event 1 id: %q", events[1].id)
	}
}

func TestSSEParserStopOnFalse(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		w.Write([]byte("data: {\"seq\":1}\n\n"))
		w.Write([]byte("data: {\"seq\":2}\n\n"))
		w.Write([]byte("data: {\"seq\":3}\n\n"))
	}))
	defer srv.Close()

	count := 0
	ConnectSSE(srv.URL, "", func(_, _, _ string) bool {
		count++
		return count < 2
	})

	if count != 2 {
		t.Errorf("expected 2 events before stop, got %d", count)
	}
}

func TestSSE404(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(404)
	}))
	defer srv.Close()

	err := ConnectSSE(srv.URL, "", func(_, _, _ string) bool { return true })
	if err == nil || err.Error() != "SSE 404" {
		t.Errorf("expected SSE 404 error, got: %v", err)
	}
}

func TestSSEAuthHeader(t *testing.T) {
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		w.WriteHeader(200)
	}))
	defer srv.Close()

	ConnectSSE(srv.URL, "Bearer test-token", func(_, _, _ string) bool { return true })
	if gotAuth != "Bearer test-token" {
		t.Errorf("auth header: got %q", gotAuth)
	}
}

func TestSSEMultiLineData(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		w.Write([]byte("data: line1\ndata: line2\n\n"))
	}))
	defer srv.Close()

	var data string
	ConnectSSE(srv.URL, "", func(_, _, d string) bool {
		data = d
		return true
	})

	if data != "line1\nline2" {
		t.Errorf("multi-line data: got %q", data)
	}
}
