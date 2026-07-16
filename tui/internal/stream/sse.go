package stream

import (
	"bufio"
	"fmt"
	"net/http"
	"strings"
)

// ConnectSSE opens an SSE connection and calls handler for each
// event. The handler receives (id, event type, data). Return
// false to stop reading. Returns error on connection failure.
func ConnectSSE(url, authHeader string, handler func(id, eventType, data string) bool) error {
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "text/event-stream")
	if authHeader != "" {
		req.Header.Set("Authorization", authHeader)
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode == 404 {
		return fmt.Errorf("SSE 404")
	}
	if resp.StatusCode != 200 {
		return fmt.Errorf("SSE status %d", resp.StatusCode)
	}

	scanner := bufio.NewScanner(resp.Body)

	var id, eventType string
	var dataLines []string

	for scanner.Scan() {
		line := scanner.Text()

		if line == "" {
			if len(dataLines) > 0 {
				data := strings.Join(dataLines, "\n")
				if !handler(id, eventType, data) {
					return nil
				}
			}
			id = ""
			eventType = ""
			dataLines = nil
			continue
		}

		if strings.HasPrefix(line, ":") {
			continue
		}

		if strings.HasPrefix(line, "id: ") {
			id = line[4:]
		} else if strings.HasPrefix(line, "event: ") {
			eventType = line[7:]
		} else if strings.HasPrefix(line, "data: ") {
			dataLines = append(dataLines, line[6:])
		} else if line == "data" {
			dataLines = append(dataLines, "")
		}
	}

	return scanner.Err()
}
