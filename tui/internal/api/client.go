// Package api provides a typed REST client for the agentic-perf
// state store API.
package api

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"time"
)

type Client struct {
	baseURL    string
	token      string
	httpClient *http.Client
	maxRetries int
}

func New(baseURL, token string) *Client {
	return &Client{
		baseURL: baseURL,
		token:   token,
		httpClient: &http.Client{
			Timeout: 30 * time.Second,
		},
		maxRetries: 3,
	}
}

type APIError struct {
	StatusCode int
	Detail     string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("API error %d: %s", e.StatusCode, e.Detail)
}

func IsNotFound(err error) bool {
	if ae, ok := err.(*APIError); ok {
		return ae.StatusCode == 404
	}
	return false
}

func IsConflict(err error) bool {
	if ae, ok := err.(*APIError); ok {
		return ae.StatusCode == 409
	}
	return false
}

func IsUnauthorized(err error) bool {
	if ae, ok := err.(*APIError); ok {
		return ae.StatusCode == 401
	}
	return false
}

type Ticket struct {
	ID           string                 `json:"id"`
	Summary      string                 `json:"summary"`
	Description  string                 `json:"description"`
	Status       string                 `json:"status"`
	CustomFields map[string]interface{} `json:"custom_fields"`
	Comments     []Comment              `json:"comments"`
	CreatedAt    string                 `json:"created_at"`
	UpdatedAt    string                 `json:"updated_at"`
	PrevStatus   *string                `json:"previous_status"`
}

type Comment struct {
	ID        string `json:"id"`
	Author    string `json:"author"`
	Body      string `json:"body"`
	CreatedAt string `json:"created_at"`
}

type EventData struct {
	Seq       int                    `json:"seq"`
	Timestamp string                 `json:"timestamp"`
	TicketID  string                 `json:"ticket_id"`
	Agent     string                 `json:"agent"`
	EventType string                 `json:"event_type"`
	Data      map[string]interface{} `json:"data"`
}

type TransitionsInfo struct {
	Current string   `json:"current"`
	Valid   []string `json:"valid"`
}

type UsageInfo struct {
	TicketID         string                 `json:"ticket_id"`
	Usage            map[string]interface{} `json:"usage"`
	EstimatedCostUSD float64                `json:"estimated_cost_usd"`
	ByAgent          map[string]interface{} `json:"by_agent"`
}

type UsageSummary struct {
	Global   map[string]interface{}            `json:"global"`
	ByTicket map[string]map[string]interface{} `json:"by_ticket"`
}

func (c *Client) Health() error {
	_, err := c.doRequest("GET", "/api/v1/health", nil)
	return err
}

func (c *Client) CreateTicket(summary, description string, customFields map[string]interface{}) (*Ticket, error) {
	body := map[string]interface{}{
		"summary":       summary,
		"description":   description,
		"custom_fields": customFields,
	}
	data, err := c.doRequest("POST", "/api/v1/tickets", body)
	if err != nil {
		return nil, err
	}
	var t Ticket
	if err := json.Unmarshal(data, &t); err != nil {
		return nil, err
	}
	return &t, nil
}

func (c *Client) GetTicket(id string) (*Ticket, error) {
	data, err := c.doRequest("GET", "/api/v1/tickets/"+id, nil)
	if err != nil {
		return nil, err
	}
	var t Ticket
	if err := json.Unmarshal(data, &t); err != nil {
		return nil, err
	}
	return &t, nil
}

func (c *Client) ListTickets(status string) ([]Ticket, error) {
	path := "/api/v1/tickets"
	if status != "" {
		path += "?status=" + status
	}
	data, err := c.doRequest("GET", path, nil)
	if err != nil {
		return nil, err
	}
	var tickets []Ticket
	if err := json.Unmarshal(data, &tickets); err != nil {
		return nil, err
	}
	return tickets, nil
}

func (c *Client) TransitionTicket(id, status, comment string) (*Ticket, error) {
	body := map[string]interface{}{"status": status}
	if comment != "" {
		body["comment"] = comment
	}
	data, err := c.doRequest("POST", "/api/v1/tickets/"+id+"/transition", body)
	if err != nil {
		return nil, err
	}
	var t Ticket
	if err := json.Unmarshal(data, &t); err != nil {
		return nil, err
	}
	return &t, nil
}

func (c *Client) UpdateFields(id string, fields map[string]interface{}) (*Ticket, error) {
	body := map[string]interface{}{"fields": fields}
	data, err := c.doRequest("PATCH", "/api/v1/tickets/"+id+"/fields", body)
	if err != nil {
		return nil, err
	}
	var t Ticket
	if err := json.Unmarshal(data, &t); err != nil {
		return nil, err
	}
	return &t, nil
}

func (c *Client) AddComment(id, author, text string) (*Comment, error) {
	body := map[string]interface{}{"author": author, "body": text}
	data, err := c.doRequest("POST", "/api/v1/tickets/"+id+"/comments", body)
	if err != nil {
		return nil, err
	}
	var cm Comment
	if err := json.Unmarshal(data, &cm); err != nil {
		return nil, err
	}
	return &cm, nil
}

func (c *Client) GetComments(id string) ([]Comment, error) {
	data, err := c.doRequest("GET", "/api/v1/tickets/"+id+"/comments", nil)
	if err != nil {
		return nil, err
	}
	var comments []Comment
	if err := json.Unmarshal(data, &comments); err != nil {
		return nil, err
	}
	return comments, nil
}

func (c *Client) GetEvents(id string, since, limit int) ([]EventData, int, error) {
	path := fmt.Sprintf("/api/v1/tickets/%s/events?since=%d&limit=%d", id, since, limit)
	data, err := c.doRequest("GET", path, nil)
	if err != nil {
		return nil, 0, err
	}
	var result struct {
		Events    []EventData `json:"events"`
		LatestSeq int         `json:"latest_seq"`
	}
	if err := json.Unmarshal(data, &result); err != nil {
		return nil, 0, err
	}
	return result.Events, result.LatestSeq, nil
}

func (c *Client) Interject(id, message string) error {
	body := map[string]interface{}{"message": message}
	_, err := c.doRequest("POST", "/api/v1/tickets/"+id+"/interject", body)
	return err
}

func (c *Client) GetTransitions(id string) (*TransitionsInfo, error) {
	data, err := c.doRequest("GET", "/api/v1/tickets/"+id+"/transitions", nil)
	if err != nil {
		return nil, err
	}
	var ti TransitionsInfo
	if err := json.Unmarshal(data, &ti); err != nil {
		return nil, err
	}
	return &ti, nil
}

func (c *Client) StopTicket(id, mode string) error {
	body := map[string]interface{}{"mode": mode}
	_, err := c.doRequest("POST", "/api/v1/tickets/"+id+"/stop", body)
	return err
}

func (c *Client) GetUsage(id string) (*UsageInfo, error) {
	data, err := c.doRequest("GET", "/api/v1/tickets/"+id+"/usage", nil)
	if err != nil {
		return nil, err
	}
	var u UsageInfo
	if err := json.Unmarshal(data, &u); err != nil {
		return nil, err
	}
	return &u, nil
}

func (c *Client) GetUsageSummary() (*UsageSummary, error) {
	data, err := c.doRequest("GET", "/api/v1/usage/summary", nil)
	if err != nil {
		return nil, err
	}
	var u UsageSummary
	if err := json.Unmarshal(data, &u); err != nil {
		return nil, err
	}
	return &u, nil
}

func (c *Client) BaseURL() string {
	return c.baseURL
}

func (c *Client) StreamURL(ticketID, eventType string, since int) string {
	path := c.baseURL + "/api/v1/events/stream"
	sep := "?"
	if ticketID != "" {
		path += sep + "ticket_id=" + ticketID
		sep = "&"
	}
	if eventType != "" {
		path += sep + "event_type=" + eventType
		sep = "&"
	}
	if since > 0 {
		path += fmt.Sprintf("%ssince=%d", sep, since)
	}
	return path
}

func (c *Client) AuthHeader() string {
	if c.token == "" {
		return ""
	}
	return "Bearer " + c.token
}

func (c *Client) doRequest(method, path string, body interface{}) ([]byte, error) {
	var lastErr error
	for attempt := 0; attempt <= c.maxRetries; attempt++ {
		if attempt > 0 {
			backoff := time.Duration(math.Pow(2, float64(attempt-1))) * time.Second
			time.Sleep(backoff)
		}

		var reqBody io.Reader
		if body != nil {
			b, err := json.Marshal(body)
			if err != nil {
				return nil, err
			}
			reqBody = bytes.NewReader(b)
		}

		req, err := http.NewRequest(method, c.baseURL+path, reqBody)
		if err != nil {
			return nil, err
		}
		if body != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		if c.token != "" {
			req.Header.Set("Authorization", "Bearer "+c.token)
		}

		resp, err := c.httpClient.Do(req)
		if err != nil {
			lastErr = err
			continue
		}

		data, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			lastErr = err
			continue
		}

		if resp.StatusCode >= 500 {
			lastErr = &APIError{StatusCode: resp.StatusCode, Detail: string(data)}
			continue
		}

		if resp.StatusCode >= 400 {
			detail := string(data)
			var errResp struct {
				Detail string `json:"detail"`
			}
			if json.Unmarshal(data, &errResp) == nil && errResp.Detail != "" {
				detail = errResp.Detail
			}
			return nil, &APIError{StatusCode: resp.StatusCode, Detail: detail}
		}

		return data, nil
	}
	return nil, fmt.Errorf("request failed after %d retries: %w", c.maxRetries, lastErr)
}
