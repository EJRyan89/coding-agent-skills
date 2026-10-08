package inventory

import (
	"fmt"
	"strings"
	"unicode/utf8"
)

// Label is the text printed on a shelf label: the SKU, two spaces, and the item's name.
func Label(sku, name string) string {
	return fmt.Sprintf("%s  %s", sku, name)
}

// PaddedLabel is Label padded with spaces to at least width characters, for fixed-width label printers. A label
// already that wide is returned unchanged.
func PaddedLabel(sku, name string, width int) string {
	label := Label(sku, name)
	if pad := width - utf8.RuneCountInString(label); pad > 0 {
		label += strings.Repeat(" ", pad)
	}
	return label
}
