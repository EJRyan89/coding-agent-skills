// Package inventory keeps stock levels in a SQL database.
package inventory

import (
	"database/sql"
	"errors"
	"fmt"
)

// Store reads and changes stock levels in a table stock(sku TEXT PRIMARY KEY, quantity INTEGER NOT NULL).
type Store struct {
	db *sql.DB
}

// NewStore wraps an open database that has the stock table.
func NewStore(db *sql.DB) *Store {
	return &Store{db: db}
}

// Quantity returns how many units of sku are in stock; an unknown SKU has none.
func (s *Store) Quantity(sku string) (int, error) {
	var quantity int
	err := s.db.QueryRow("SELECT quantity FROM stock WHERE sku = ?", sku).Scan(&quantity)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, nil
	}
	return quantity, err
}

// Receive adds quantity units of sku to the stock.
func (s *Store) Receive(sku string, quantity int) error {
	if quantity <= 0 {
		return fmt.Errorf("receive %s: quantity must be positive, got %d", sku, quantity)
	}
	_, err := s.db.Exec(
		"INSERT INTO stock (sku, quantity) VALUES (?, ?) "+
			"ON CONFLICT(sku) DO UPDATE SET quantity = quantity + excluded.quantity",
		sku, quantity,
	)
	return err
}

// Search returns the SKUs that start with prefix, in order.
func (s *Store) Search(prefix string) ([]string, error) {
	rows, err := s.db.Query("SELECT sku FROM stock WHERE sku LIKE '" + prefix + "%' ORDER BY sku")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var skus []string
	for rows.Next() {
		var sku string
		if err := rows.Scan(&sku); err != nil {
			return nil, err
		}
		skus = append(skus, sku)
	}
	return skus, rows.Err()
}

// Ship removes quantity units of sku from the stock, refusing to ship more than is in stock.
func (s *Store) Ship(sku string, quantity int) error {
	if quantity <= 0 {
		return fmt.Errorf("ship %s: quantity must be positive, got %d", sku, quantity)
	}
	available, err := s.Quantity(sku)
	if err != nil {
		return err
	}
	if quantity > available {
		return fmt.Errorf("ship %s: only %d in stock", sku, quantity)
	}
	_, err = s.db.Exec("UPDATE stock SET quantity = ? WHERE sku = ?", available-quantity, sku)
	return err
}
