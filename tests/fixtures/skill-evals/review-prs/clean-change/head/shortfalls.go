package inventory

import (
	"cmp"
	"slices"
)

// Level is one SKU's stock and the minimum it is kept at.
type Level struct {
	SKU      string
	Quantity int
	Minimum  int
}

// Shortfall is how many units bring a SKU back up to its minimum.
type Shortfall struct {
	SKU   string
	Units int
}

// Shortfalls lists every level below its minimum, the largest shortfall first and equal ones by SKU, so a reorder
// list prints in the same order every time. Levels at or above their minimum are left out.
func Shortfalls(levels []Level) []Shortfall {
	shortfalls := []Shortfall{}
	for _, level := range levels {
		if units := level.Minimum - level.Quantity; units > 0 {
			shortfalls = append(shortfalls, Shortfall{SKU: level.SKU, Units: units})
		}
	}
	slices.SortFunc(shortfalls, func(a, b Shortfall) int {
		if c := cmp.Compare(b.Units, a.Units); c != 0 {
			return c
		}
		return cmp.Compare(a.SKU, b.SKU)
	})
	return shortfalls
}
