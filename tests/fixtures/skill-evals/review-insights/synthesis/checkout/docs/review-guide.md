# Inventory review guide

Review every change to the inventory service against these rules.

- Every public method must have an XML documentation comment, including the methods in the `tools/` folder.
- Flag a database call made inside a loop as SHOULD_FIX.
- A change to a stock quantity must be covered by a unit test.
