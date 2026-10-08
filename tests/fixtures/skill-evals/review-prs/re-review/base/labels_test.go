package inventory

import "testing"

func TestLabel(t *testing.T) {
	if got := Label("A-1", "Bolt"); got != "A-1  Bolt" {
		t.Fatalf("Label = %q, want %q", got, "A-1  Bolt")
	}
}
