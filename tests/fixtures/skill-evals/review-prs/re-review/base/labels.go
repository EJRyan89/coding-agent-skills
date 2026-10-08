package inventory

import "fmt"

// Label is the text printed on a shelf label: the SKU, two spaces, and the item's name.
func Label(sku, name string) string {
	return fmt.Sprintf("%s  %s", sku, name)
}
