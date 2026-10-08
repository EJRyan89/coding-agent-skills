package inventory

import "testing"

func TestLabel(t *testing.T) {
	if got := Label("A-1", "Bolt"); got != "A-1  Bolt" {
		t.Fatalf("Label = %q, want %q", got, "A-1  Bolt")
	}
}

func TestPaddedLabel(t *testing.T) {
	cases := []struct {
		sku, name string
		width     int
		want      string
	}{
		{"A-1", "Bolt", 12, "A-1  Bolt   "},
		{"A-1", "Bolt", 9, "A-1  Bolt"},
		{"A-1", "Bolt", 4, "A-1  Bolt"},
		{"A-1", "Bolt", -1, "A-1  Bolt"},
		{"B-2", "Écrou", 12, "B-2  Écrou  "},
	}
	for _, c := range cases {
		if got := PaddedLabel(c.sku, c.name, c.width); got != c.want {
			t.Errorf("PaddedLabel(%q, %q, %d) = %q, want %q", c.sku, c.name, c.width, got, c.want)
		}
	}
}
