package ui

import "fmt"

func bell() string {
	return "\a"
}

func (m *Model) notifyHITL(ticketID string) {
	m.addSystemLine(fmt.Sprintf("%s🔔 %s needs input", bell(), ticketID))
}
