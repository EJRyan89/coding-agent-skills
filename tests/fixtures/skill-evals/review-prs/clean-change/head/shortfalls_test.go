package inventory

import (
	"slices"
	"testing"
)

func TestShortfalls(t *testing.T) {
	cases := []struct {
		name   string
		levels []Level
		want   []Shortfall
	}{
		{"none", nil, []Shortfall{}},
		{"at or above minimum", []Level{{"A-1", 5, 5}, {"A-2", 9, 5}}, []Shortfall{}},
		{
			"largest first, then by SKU",
			[]Level{{"C-3", 0, 2}, {"A-1", 1, 5}, {"B-2", 3, 5}, {"D-4", 2, 6}},
			[]Shortfall{{"A-1", 4}, {"D-4", 4}, {"B-2", 2}, {"C-3", 2}},
		},
		{"negative stock", []Level{{"E-5", -3, 2}}, []Shortfall{{"E-5", 5}}},
	}
	for _, c := range cases {
		if got := Shortfalls(c.levels); !slices.Equal(got, c.want) {
			t.Errorf("%s: Shortfalls = %v, want %v", c.name, got, c.want)
		}
	}
}
