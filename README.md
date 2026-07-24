# Inventory Management & Invoicing System

## Overview

This application is a desktop Inventory Management and Invoicing System built with Python, Tkinter, and SQLite. It is designed to manage inventory, shipments, customer information, invoicing, transactions, and financial reporting while providing automatic inventory analysis through Days on Hand (DOH) calculations.

The system combines inventory tracking, customer management, transaction management, monthly/yearly statements, and reporting into a single application.

---

## Features

### Inventory Management

* Add inventory items.
* Edit existing inventory items.
* Delete inventory items (when permitted).
* Search inventory items.
* Track:

  * Item Name
  * SKU
  * Weight
  * Price
  * Current Quantity
  * Inventory Value
  * Days on Hand (DOH)
  * Average Daily Usage
  * Stock Priority
* Automatic inventory value calculations.
* Automatic inventory priority calculations.

### Automatic Inventory Analysis

Inventory priorities are no longer manually assigned.

The application uses a weighted Days on Hand (DOH) metric based upon invoice history.

#### Weighted Usage Calculation

| Period       | Weight |
| ------------ | ------ |
| Last 30 Days | 60%    |
| 31-60 Days   | 30%    |
| 61-90 Days   | 10%    |

The calculation is:

```text
Days on Hand = Current Inventory ÷ Average Daily Usage
```

Inventory statuses are automatically assigned:

| Days Remaining  | Status          |
| --------------- | --------------- |
| > 60            | Healthy         |
| 30 - 60         | Watch           |
| 14 - 29         | Low             |
| < 14            | Critical        |
| 0               | Out of Stock    |
| No Recent Sales | No recent sales |

Items are automatically highlighted based upon their inventory priority.

---

## Shipment Management

The Add Shipment tab supports:

* Multi-line shipments
* Shipment numbers
* Shipment notes
* Inventory increases for multiple items simultaneously

A single shipment may contain any number of inventory items.

Example:

```text
Shipment #245

--------------------------------
Item                 Quantity
--------------------------------
Rice Crackers            40
Pocky                    25
Milk Tea                 15
--------------------------------
```

---

## Invoice Management

The Record Invoice tab supports:

* Multi-line invoices
* Customer autofill
* Automatic customer creation
* Discounts
* Credits
* Order notes
* Automatic inventory reduction
* Printable invoices
* Live invoice calculations

### Customer Information

Invoices contain:

* Store Name
* Address
* Phone Number

Customer information automatically populates when selecting previously saved customers.

### Invoice Calculations

Invoices automatically calculate:

```text
Subtotal

(-) Discount %

(-) Credit

----------------

Final Total
```

Each line item also calculates:

```text
Quantity × Unit Price
```

before calculating the final invoice total.

---

## Customer Management

Customers are automatically created when saving invoices.

Manual customer creation is not required.

Each customer receives an internal numeric ID.

Example:

```text
Customer ID: 41

Store Name:
ABC Hardware

Address:
123 Main Street

Phone:
517-555-1234
```

Invoices are linked using:

```text
customer_id
```

instead of customer names.

This allows:

* Store name changes
* Address changes
* Customer history tracking
* Duplicate prevention
* Improved reporting

### Customer Management Features

* Search customers
* Add customers
* Edit customers
* Delete customers
* View customer invoice history

Deleting a customer will NOT remove historical invoices or transactions.

---

## Transactions

Every saved invoice automatically creates a transaction.

### Transaction Statuses

| Status | Meaning                   |
| ------ | ------------------------- |
| Open   | Invoice has not been paid |
| Closed | Invoice has been paid     |
| Void   | Invoice has been voided   |

Transactions store:

* Invoice number
* Customer
* Invoice information
* Payment status
* Payment method
* Totals
* Notes

### Payment Methods

Supported payment methods include:

* Cash
* Check
* Zelle
* Credit
* Other

Selecting and saving a payment method automatically changes the transaction status to:

```text
OPEN

↓

CLOSED
```

---

## Statements

The Statements tab automatically maintains:

### Monthly Statements

Monthly statements include:

* Revenue
* Discounts
* Credits
* Closed transactions
* Open transactions
* Inventory information

Missing monthly statements are automatically generated when the application opens.

### Yearly Statements

Yearly statements are generated from the saved monthly statements.

This allows historical reporting without recalculating previous periods.

---

## Printable Documents

The application automatically generates printable:

* Invoices
* Monthly statements
* Yearly statements

These may be printed directly or saved as PDF using the system print dialog.

---

## Item Editor

Items may be edited without affecting historical transactions.

Editable fields include:

* Name
* SKU
* Weight
* Price

The following remain unchanged:

* Inventory history
* Shipment history
* Invoice history
* Days on Hand calculations

---

## Export Features

CSV exports are available for:

* Inventory
* Customers
* Transactions
* Statements
* History

Exports are compatible with:

* Microsoft Excel
* Google Sheets
* LibreOffice Calc

Searches and filters are respected during exports.

## Database Information

The application uses SQLite.

Major tables include:

```text
Items

Customers

Transactions

Invoices

Shipments

Monthly Statements

Yearly Statements

Transaction History
```

The customer database uses numeric customer IDs to preserve invoice history even when customer information changes.

---

## Current Workflow

### Receiving Inventory

```text
Add Shipment

↓

Save Shipment

↓

Inventory Updated

↓

Days on Hand Updated
```

### Creating an Invoice

```text
Create Invoice

↓

Select Customer

↓

Add Invoice Items

↓

Apply Discounts/Credits

↓

Save Invoice

↓

Inventory Reduced

↓

Transaction Created

↓

Customer Saved Automatically

↓

Printable Invoice Generated

↓

Transaction Status = OPEN
```

### Receiving Payment

```text
Open Transaction

↓

Select Payment Method

↓

Save Payment Information

↓

Transaction Status = CLOSED
```

## Technologies Used

* Python
* Tkinter
* SQLite3
* CSV Export Support
* HTML Printable Documents

---

## Version Summary

The current version provides:

* Inventory Management
* Automatic Days on Hand calculations
* Shipment Management
* Customer Management
* Multi-line Invoicing
* Transactions Management
* Payment Tracking
* Monthly & Yearly Statements
* CSV Exports
* Printable Documents
* Database Backup & Restore
* Automatic Customer Creation
* Inventory Analysis & Priority Tracking

This application has evolved from a simple inventory tracker into a comprehensive inventory, invoicing, and reporting system suitable for small business inventory management and transaction tracking.
