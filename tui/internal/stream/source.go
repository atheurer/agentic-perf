// Package stream implements SSE and polling event sources with
// automatic reconnection and transparent fallback.
package stream

import (
	"encoding/json"
	"fmt"
	"sync"
	"time"

	"github.com/atheurer/agentic-perf/tui/internal/api"
)

type Event = map[string]interface{}

type Source interface {
	Events() <-chan Event
	Close()
	Transport() string
}

type sseSource struct {
	client   *api.Client
	ticketID string
	events   chan Event
	done     chan struct{}
	once     sync.Once
	seen     map[string]bool
}

func NewSSE(client *api.Client, ticketID string) Source {
	s := &sseSource{
		client:   client,
		ticketID: ticketID,
		events:   make(chan Event, 100),
		done:     make(chan struct{}),
		seen:     make(map[string]bool),
	}
	go s.loop()
	return s
}

func (s *sseSource) Events() <-chan Event { return s.events }
func (s *sseSource) Transport() string    { return "sse" }

func (s *sseSource) Close() {
	s.once.Do(func() { close(s.done) })
}

func (s *sseSource) loop() {
	defer close(s.events)
	backoff := time.Second

	for {
		select {
		case <-s.done:
			return
		default:
		}

		url := s.client.StreamURL(s.ticketID, "", 0)
		err := ConnectSSE(url, s.client.AuthHeader(), func(id, eventType, data string) bool {
			select {
			case <-s.done:
				return false
			default:
			}

			if s.seen[id] {
				return true
			}
			s.seen[id] = true

			var evt Event
			if err := json.Unmarshal([]byte(data), &evt); err != nil {
				return true
			}

			select {
			case s.events <- evt:
			case <-s.done:
				return false
			}
			backoff = time.Second
			return true
		})

		if err != nil {
			select {
			case <-s.done:
				return
			case <-time.After(backoff):
				if backoff < 30*time.Second {
					backoff *= 2
				}
			}
		}
	}
}

type pollSource struct {
	client   *api.Client
	ticketID string
	events   chan Event
	done     chan struct{}
	once     sync.Once
	interval time.Duration
}

func NewPoll(client *api.Client, ticketID string, interval time.Duration) Source {
	s := &pollSource{
		client:   client,
		ticketID: ticketID,
		events:   make(chan Event, 100),
		done:     make(chan struct{}),
		interval: interval,
	}
	go s.loop()
	return s
}

func (s *pollSource) Events() <-chan Event { return s.events }
func (s *pollSource) Transport() string    { return "poll" }

func (s *pollSource) Close() {
	s.once.Do(func() { close(s.done) })
}

func (s *pollSource) loop() {
	defer close(s.events)
	cursor := 0
	for {
		select {
		case <-s.done:
			return
		case <-time.After(s.interval):
		}

		events, latestSeq, err := s.client.GetEvents(s.ticketID, cursor, 200)
		if err != nil {
			continue
		}

		for _, e := range events {
			raw := map[string]interface{}{
				"seq":        float64(e.Seq),
				"timestamp":  e.Timestamp,
				"ticket_id":  e.TicketID,
				"agent":      e.Agent,
				"event_type": e.EventType,
				"data":       e.Data,
			}
			select {
			case s.events <- raw:
			case <-s.done:
				return
			}
		}
		if latestSeq > cursor {
			cursor = latestSeq
		}
	}
}

// NewAutoSource tries SSE first, falls back to polling if the
// SSE endpoint returns 404.
func NewAutoSource(client *api.Client, ticketID string) Source {
	url := client.StreamURL(ticketID, "", 0)
	err := ConnectSSE(url, client.AuthHeader(), func(_, _, _ string) bool {
		return false
	})

	if err != nil && isNotFoundErr(err) {
		return NewPoll(client, ticketID, 2*time.Second)
	}
	return NewSSE(client, ticketID)
}

func isNotFoundErr(err error) bool {
	return fmt.Sprintf("%v", err) == "SSE 404"
}
