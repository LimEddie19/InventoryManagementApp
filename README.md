# Stalk Inventory Management System

Stalk is a desktop inventory management application built with **Python**, **Tkinter**, and **SQLite**. It is designed for wholesalers and distributors to manage inventory, shipments, invoices, customer accounts, and business reporting from a single application.

---

## Features

### Inventory Management

* Track inventory with SKU, weight, pricing, and stock levels.
* Support for both **box** and **individual unit** inventory.
* Automatic inventory valuation.
* Intelligent **Days on Hand (DOH)** inventory monitoring.
* Temporary product surcharges for tariffs, freight, or demurrage costs.

### Shipment Management

* Multi-line shipment entry.
* Receive inventory as boxes or individual units.
* Automatic inventory updates.
* Shipment history tracking.

### Invoice Management

* Create professional printable invoices.
* Multi-line invoice support.
* Automatic inventory deduction.
* Customer-specific pricing.
* Discounts, customer credits, and invoice notes.
* Box and unit sales.

### Customer Management

* Customer database with autocomplete.
* Store contact information.
* Customer account credits.
* Customer-specific product pricing.
* Purchase history.

### Transactions

* Automatic transaction records for every invoice.
* Open, Closed, and Void statuses.
* Payment method tracking:

  * Cash
  * Check
  * Zelle
  * Credit
  * Other

### Refund Management

* Refund inventory back into stock.
* Issue refunds as:

  * Store Credit
  * Cash
  * Original Payment Method
* Maintain complete refund history.

### Reporting

* Monthly business statements.
* Yearly summaries.
* CSV export for inventory, customers, and transactions.

### Application Updates

* Built-in version tracking.
* Manual **Check for Updates**.
* Background update notifications.
* GitHub release integration.

---

## Technology Stack

* Python 3
* Tkinter
* SQLite
* PyInstaller
* Git & GitHub

---

## Installation

### Windows

Run the provided executable.

### Linux / Chromebook

Run the compiled Linux executable:

```
./Stalk
```

or launch it from the desktop shortcut after installation.

---

## Building from Source

Install dependencies and build using PyInstaller:

```
pip install pyinstaller
```

Change depending on source file name (Placeholder: stalk.py)

```
pyinstaller --onefile --windowed --name Stalk stalk.py
```

The executable will be created in the `dist/` directory.

---

## Versioning

Stalk follows **Semantic Versioning**.

* **Major** – Breaking changes
* **Minor** – New features
* **Patch** – Bug fixes

Current Release: **v1.1.0**

---

## License

This project is intended for internal business use unless otherwise specified.
