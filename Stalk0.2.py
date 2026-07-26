import os
import re
import csv
import shutil
import sqlite3
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime
from html import escape


DB_FILE = "inventory.db"


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def safe_filename(text):
    text = text.strip() or "invoice"
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", text)
    return text.strip("_") or "invoice"


def money(value):
    return f"${float(value):,.2f}"


class InventoryDB:
    def __init__(self, path=DB_FILE):
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            sku TEXT,
            price REAL NOT NULL DEFAULT 0,
            weight TEXT,
            qty INTEGER NOT NULL DEFAULT 0,
            reorder_level INTEGER NOT NULL DEFAULT 0,
            restock_total INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        """)

        self._ensure_item_columns()

        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS txns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK(type IN ('SHIPMENT','INVOICE')),
            ref_number TEXT NOT NULL,
            item_id INTEGER NOT NULL,
            qty_change INTEGER NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(item_id) REFERENCES items(id)
        );
        """)

        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            ref_number TEXT NOT NULL,
            store_name TEXT,
            address TEXT,
            phone TEXT,
            discount_percent REAL NOT NULL DEFAULT 0,
            credit REAL NOT NULL DEFAULT 0,
            notes TEXT,
            subtotal REAL NOT NULL DEFAULT 0,
            discount_amount REAL NOT NULL DEFAULT 0,
            total REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed','void')),
            payment_method TEXT,
            printable_path TEXT,
            created_at TEXT NOT NULL,
            paid_at TEXT
        );
        """)

        self._ensure_invoice_columns()

        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS invoice_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            qty INTEGER NOT NULL,
            unit_price REAL NOT NULL DEFAULT 0,
            line_total REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(invoice_id) REFERENCES invoices(id),
            FOREIGN KEY(item_id) REFERENCES items(id)
        );
        """)

        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_name TEXT NOT NULL UNIQUE,
            address TEXT,
            phone TEXT,
            credit_balance REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        """)
        self._ensure_customer_columns()

        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS customer_credit_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            entry_type TEXT NOT NULL CHECK(entry_type IN ('REFUND','APPLIED','ADJUSTMENT')),
            reference TEXT,
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(customer_id) REFERENCES customers(id)
        );
        """)

        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS refunds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            customer_id INTEGER,
            refund_method TEXT NOT NULL CHECK(refund_method IN ('store credit','cash','original payment','other')),
            total REAL NOT NULL DEFAULT 0,
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(invoice_id) REFERENCES invoices(id),
            FOREIGN KEY(customer_id) REFERENCES customers(id)
        );
        """)

        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS refund_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            refund_id INTEGER NOT NULL,
            invoice_line_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            qty INTEGER NOT NULL,
            unit_price REAL NOT NULL,
            line_total REAL NOT NULL,
            FOREIGN KEY(refund_id) REFERENCES refunds(id),
            FOREIGN KEY(invoice_line_id) REFERENCES invoice_lines(id),
            FOREIGN KEY(item_id) REFERENCES items(id)
        );
        """)

        self._ensure_customer_links()

        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS monthly_statements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            period_label TEXT NOT NULL,
            invoice_count INTEGER NOT NULL DEFAULT 0,
            open_count INTEGER NOT NULL DEFAULT 0,
            closed_count INTEGER NOT NULL DEFAULT 0,
            void_count INTEGER NOT NULL DEFAULT 0,
            subtotal REAL NOT NULL DEFAULT 0,
            discount_amount REAL NOT NULL DEFAULT 0,
            credit REAL NOT NULL DEFAULT 0,
            total_sales REAL NOT NULL DEFAULT 0,
            paid_total REAL NOT NULL DEFAULT 0,
            open_total REAL NOT NULL DEFAULT 0,
            void_total REAL NOT NULL DEFAULT 0,
            printable_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(year, month)
        );
        """)
        self.conn.commit()

    def _ensure_item_columns(self):
        """Add new columns to existing inventory.db files without deleting data."""
        cur = self.conn.cursor()
        cur.execute("PRAGMA table_info(items);")
        columns = {row[1] for row in cur.fetchall()}

        if "price" not in columns:
            self.conn.execute("ALTER TABLE items ADD COLUMN price REAL NOT NULL DEFAULT 0;")
        if "weight" not in columns:
            self.conn.execute("ALTER TABLE items ADD COLUMN weight TEXT;")
        if "restock_total" not in columns:
            self.conn.execute("ALTER TABLE items ADD COLUMN restock_total INTEGER NOT NULL DEFAULT 0;")
            # Existing inventory gets a sensible initial baseline. The next shipment
            # will replace this with the newly replenished stock total.
            self.conn.execute("UPDATE items SET restock_total = qty WHERE restock_total = 0 AND qty > 0;")

    def _ensure_invoice_columns(self):
        """Add transaction-status columns to existing inventory.db files without deleting data."""
        cur = self.conn.cursor()
        cur.execute("PRAGMA table_info(invoices);")
        columns = {row[1] for row in cur.fetchall()}

        if "status" not in columns:
            self.conn.execute("ALTER TABLE invoices ADD COLUMN status TEXT NOT NULL DEFAULT 'open';")
        if "payment_method" not in columns:
            self.conn.execute("ALTER TABLE invoices ADD COLUMN payment_method TEXT;")
        if "printable_path" not in columns:
            self.conn.execute("ALTER TABLE invoices ADD COLUMN printable_path TEXT;")
        if "paid_at" not in columns:
            self.conn.execute("ALTER TABLE invoices ADD COLUMN paid_at TEXT;")
        if "customer_id" not in columns:
            self.conn.execute("ALTER TABLE invoices ADD COLUMN customer_id INTEGER;")

    def _ensure_customer_columns(self):
        cur = self.conn.cursor()
        cur.execute("PRAGMA table_info(customers);")
        columns = {row[1] for row in cur.fetchall()}
        if "credit_balance" not in columns:
            self.conn.execute("ALTER TABLE customers ADD COLUMN credit_balance REAL NOT NULL DEFAULT 0;")

    def _ensure_customer_links(self):
        """Create customer records for legacy invoices and link invoices by numeric ID."""
        self.conn.execute("""
            INSERT OR IGNORE INTO customers (store_name, address, phone, updated_at)
            SELECT DISTINCT TRIM(store_name), COALESCE(address,''), COALESCE(phone,''), ?
            FROM invoices
            WHERE TRIM(COALESCE(store_name,'')) <> '';
        """, (now_str(),))
        self.conn.execute("""
            UPDATE invoices
            SET customer_id = (
                SELECT c.id FROM customers c
                WHERE c.store_name = invoices.store_name COLLATE NOCASE
                LIMIT 1
            )
            WHERE customer_id IS NULL
              AND TRIM(COALESCE(store_name,'')) <> '';
        """)

    def list_items(self, search_text=""):
        cur = self.conn.cursor()
        params = []
        where = ""
        if search_text.strip():
            where = "WHERE i.name LIKE ?"
            params.append(f"%{search_text.strip()}%")

        cur.execute(f"""
            SELECT
                i.id, i.name, COALESCE(i.sku,''), COALESCE(i.price,0),
                COALESCE(i.weight,''), i.qty,
                COALESCE(SUM(CASE
                    WHEN t.type = 'INVOICE'
                     AND t.created_at >= datetime('now', '-30 days')
                    THEN -t.qty_change ELSE 0 END), 0) AS sold_days_0_30,
                COALESCE(SUM(CASE
                    WHEN t.type = 'INVOICE'
                     AND t.created_at >= datetime('now', '-60 days')
                     AND t.created_at < datetime('now', '-30 days')
                    THEN -t.qty_change ELSE 0 END), 0) AS sold_days_31_60,
                COALESCE(SUM(CASE
                    WHEN t.type = 'INVOICE'
                     AND t.created_at >= datetime('now', '-90 days')
                     AND t.created_at < datetime('now', '-60 days')
                    THEN -t.qty_change ELSE 0 END), 0) AS sold_days_61_90,
                i.updated_at
            FROM items i
            LEFT JOIN txns t ON t.item_id = i.id
            {where}
            GROUP BY i.id
            ORDER BY i.name COLLATE NOCASE;
        """, params)
        return cur.fetchall()

    def add_item(self, name, sku, price, weight):
        name = name.strip()
        if not name:
            raise ValueError("Item name is required.")
        sku = sku.strip()
        weight = weight.strip()
        price_value = float(price or 0)
        if price_value < 0:
            raise ValueError("Price must be >= 0.")
        self.conn.execute("""
            INSERT INTO items (name, sku, price, weight, qty, reorder_level, restock_total, updated_at)
            VALUES (?, ?, ?, ?, 0, 0, 0, ?);
        """, (name, sku, price_value, weight, now_str()))
        self.conn.commit()
        

    def update_item(self, item_id, name, sku, price, weight):
        name = name.strip()
        if not name:
            raise ValueError("Item name is required.")

        sku = sku.strip()
        weight = weight.strip()
        try:
            price_value = float(price or 0)
        except ValueError:
            raise ValueError("Price must be a valid number.")

        if price_value < 0:
            raise ValueError("Price must be >= 0.")

        cur = self.conn.cursor()
        cur.execute("SELECT id FROM items WHERE id = ?;", (item_id,))
        if not cur.fetchone():
            raise ValueError("Item not found.")

        cur.execute("""
            SELECT id FROM items
            WHERE name = ? COLLATE NOCASE AND id <> ?;
        """, (name, item_id))
        if cur.fetchone():
            raise ValueError("Another item already uses that name.")

        self.conn.execute("""
            UPDATE items
            SET name = ?, sku = ?, price = ?, weight = ?, updated_at = ?
            WHERE id = ?;
        """, (name, sku, price_value, weight, now_str(), item_id))
        self.conn.commit()

    def delete_item(self, item_id):
        cur = self.conn.cursor()

        # check stock
        cur.execute("SELECT qty FROM items WHERE id = ?", (item_id,))
        qty = cur.fetchone()[0]

        if qty != 0:
            raise ValueError("Cannot delete item with stock remaining.")

        # check history
        cur.execute("SELECT COUNT(*) FROM txns WHERE item_id = ?", (item_id,))
        count = cur.fetchone()[0]

        if count > 0:
            raise ValueError("Cannot delete item that has transaction history.")

        self.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        self.conn.commit()
    
    def get_item_by_name(self, name):
        cur = self.conn.cursor()
        cur.execute("SELECT id, name, qty FROM items WHERE name = ?;", (name,))
        return cur.fetchone()

    def get_item_details(self, item_id):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, name, COALESCE(price,0), COALESCE(weight,''), qty
            FROM items
            WHERE id = ?;
        """, (item_id,))
        return cur.fetchone()

    def list_customers(self, search_text=""):
        cur = self.conn.cursor()
        if search_text.strip():
            q = f"%{search_text.strip()}%"
            cur.execute("""
                SELECT store_name, COALESCE(address,''), COALESCE(phone,''), COALESCE(credit_balance,0)
                FROM customers
                WHERE store_name LIKE ?
                ORDER BY store_name COLLATE NOCASE;
            """, (q,))
        else:
            cur.execute("""
                SELECT store_name, COALESCE(address,''), COALESCE(phone,''), COALESCE(credit_balance,0)
                FROM customers
                ORDER BY store_name COLLATE NOCASE;
            """)
        return cur.fetchall()

    def get_customer_by_store_name(self, store_name):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, store_name, COALESCE(address,''), COALESCE(phone,''), COALESCE(credit_balance,0)
            FROM customers
            WHERE store_name = ? COLLATE NOCASE;
        """, (store_name.strip(),))
        return cur.fetchone()

    def save_customer(self, store_name, address, phone):
        """Create a customer and return its ID, or return the matching existing ID."""
        store_name = store_name.strip()
        if not store_name:
            raise ValueError("Store name is required.")

        existing = self.get_customer_by_store_name(store_name)
        if existing:
            return existing[0]

        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO customers (store_name, address, phone, updated_at)
            VALUES (?, ?, ?, ?);
        """, (store_name, address.strip(), phone.strip(), now_str()))
        self.conn.commit()
        return cur.lastrowid

    def list_customer_records(self, search_text=""):
        cur = self.conn.cursor()
        if search_text.strip():
            q = f"%{search_text.strip()}%"
            cur.execute("""
                SELECT id, store_name, COALESCE(address,''), COALESCE(phone,''), COALESCE(credit_balance,0), updated_at
                FROM customers
                WHERE store_name LIKE ? OR address LIKE ? OR phone LIKE ?
                ORDER BY store_name COLLATE NOCASE;
            """, (q, q, q))
        else:
            cur.execute("""
                SELECT id, store_name, COALESCE(address,''), COALESCE(phone,''), COALESCE(credit_balance,0), updated_at
                FROM customers
                ORDER BY store_name COLLATE NOCASE;
            """)
        return cur.fetchall()

    def update_customer(self, customer_id, store_name, address, phone):
        store_name = store_name.strip()
        if not store_name:
            raise ValueError("Store name is required.")
        self.conn.execute("""
            UPDATE customers
            SET store_name = ?, address = ?, phone = ?, updated_at = ?
            WHERE id = ?;
        """, (store_name, address.strip(), phone.strip(), now_str(), customer_id))
        self.conn.commit()

    def delete_customer(self, customer_id):
        cur = self.conn.cursor()
        cur.execute("SELECT store_name FROM customers WHERE id = ?;", (customer_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError("Customer not found.")
        self.conn.execute("UPDATE invoices SET customer_id = NULL WHERE customer_id = ?;", (customer_id,))
        self.conn.execute("DELETE FROM customers WHERE id = ?;", (customer_id,))
        self.conn.commit()

    def list_customer_invoices(self, customer_id):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, created_at, ref_number, total, status, COALESCE(payment_method,'')
            FROM invoices
            WHERE customer_id = ?
            ORDER BY id DESC;
        """, (customer_id,))
        return cur.fetchall()

    def get_customer_credit(self, customer_id):
        cur = self.conn.cursor()
        cur.execute("SELECT COALESCE(credit_balance,0) FROM customers WHERE id = ?;", (customer_id,))
        row = cur.fetchone()
        return float(row[0]) if row else 0.0

    def adjust_customer_credit(self, customer_id, amount, entry_type, reference="", note=""):
        amount = float(amount)
        if entry_type not in {"REFUND", "APPLIED", "ADJUSTMENT"}:
            raise ValueError("Invalid credit history type.")
        cur = self.conn.cursor()
        cur.execute("SELECT COALESCE(credit_balance,0) FROM customers WHERE id = ?;", (customer_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError("Customer not found.")
        new_balance = float(row[0]) + amount
        if new_balance < -0.005:
            raise ValueError("Customer does not have enough account credit.")
        cur.execute("UPDATE customers SET credit_balance = ?, updated_at = ? WHERE id = ?;", (max(0.0, new_balance), now_str(), customer_id))
        cur.execute("""
            INSERT INTO customer_credit_history (customer_id, amount, entry_type, reference, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?);
        """, (customer_id, amount, entry_type, reference.strip(), note.strip(), now_str()))
        self.conn.commit()

    def list_customer_credit_history(self, customer_id):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT created_at, entry_type, amount, COALESCE(reference,''), COALESCE(note,'')
            FROM customer_credit_history WHERE customer_id = ? ORDER BY id DESC;
        """, (customer_id,))
        return cur.fetchall()

    def list_refundable_invoices(self):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, ref_number, COALESCE(store_name,''), created_at, total, status
            FROM invoices WHERE status != 'void' ORDER BY id DESC;
        """)
        return cur.fetchall()

    def get_refundable_invoice_lines(self, invoice_id):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT il.id, il.item_id, il.item_name, il.qty,
                   COALESCE(SUM(rl.qty),0), il.unit_price
            FROM invoice_lines il
            LEFT JOIN refund_lines rl ON rl.invoice_line_id = il.id
            WHERE il.invoice_id = ?
            GROUP BY il.id
            HAVING il.qty - COALESCE(SUM(rl.qty),0) > 0
            ORDER BY il.id;
        """, (invoice_id,))
        return cur.fetchall()

    def save_refund(self, invoice_id, refund_method, lines, note=""):
        allowed = {"store credit", "cash", "original payment", "other"}
        if refund_method not in allowed:
            raise ValueError("Select a valid refund method.")
        cur = self.conn.cursor()
        cur.execute("SELECT customer_id, ref_number FROM invoices WHERE id = ? AND status != 'void';", (invoice_id,))
        invoice = cur.fetchone()
        if not invoice:
            raise ValueError("Invoice was not found or is void.")
        customer_id, ref_number = invoice
        if not lines:
            raise ValueError("Add at least one returned item.")
        validated = []
        total = 0.0
        for invoice_line_id, return_qty in lines:
            return_qty = int(return_qty)
            if return_qty <= 0:
                raise ValueError("Return quantities must be greater than zero.")
            cur.execute("""
                SELECT il.item_id, il.item_name, il.qty, il.unit_price,
                       COALESCE((SELECT SUM(rl.qty) FROM refund_lines rl WHERE rl.invoice_line_id = il.id),0)
                FROM invoice_lines il WHERE il.id = ? AND il.invoice_id = ?;
            """, (invoice_line_id, invoice_id))
            row = cur.fetchone()
            if not row:
                raise ValueError("A selected invoice line was not found.")
            item_id, item_name, sold_qty, unit_price, already_refunded = row
            available = sold_qty - already_refunded
            if return_qty > available:
                raise ValueError(f"Only {available} unit(s) of {item_name} remain refundable.")
            line_total = float(unit_price) * return_qty
            total += line_total
            validated.append((invoice_line_id, item_id, item_name, return_qty, float(unit_price), line_total))
        cur.execute("""
            INSERT INTO refunds (invoice_id, customer_id, refund_method, total, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?);
        """, (invoice_id, customer_id, refund_method, total, note.strip(), now_str()))
        refund_id = cur.lastrowid
        for invoice_line_id, item_id, item_name, qty, unit_price, line_total in validated:
            cur.execute("""
                INSERT INTO refund_lines (refund_id, invoice_line_id, item_id, item_name, qty, unit_price, line_total)
                VALUES (?, ?, ?, ?, ?, ?, ?);
            """, (refund_id, invoice_line_id, item_id, item_name, qty, unit_price, line_total))
            cur.execute("UPDATE items SET qty = qty + ?, updated_at = ? WHERE id = ?;", (qty, now_str(), item_id))
            cur.execute("""
                INSERT INTO txns (type, ref_number, item_id, qty_change, note, created_at)
                VALUES ('SHIPMENT', ?, ?, ?, ?, ?);
            """, (f"REFUND-{ref_number}", item_id, qty, f"Customer return: {note.strip()}", now_str()))
        if refund_method == "store credit":
            if customer_id is None:
                raise ValueError("Store credit requires an invoice linked to a saved customer.")
            cur.execute("UPDATE customers SET credit_balance = credit_balance + ?, updated_at = ? WHERE id = ?;", (total, now_str(), customer_id))
            cur.execute("""
                INSERT INTO customer_credit_history (customer_id, amount, entry_type, reference, note, created_at)
                VALUES (?, ?, 'REFUND', ?, ?, ?);
            """, (customer_id, total, f"Refund #{refund_id} / Invoice {ref_number}", note.strip(), now_str()))
        self.conn.commit()
        return refund_id, total

    def list_refunds(self):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT r.id, r.created_at, i.ref_number, COALESCE(i.store_name,''), r.refund_method, r.total, COALESCE(r.note,'')
            FROM refunds r JOIN invoices i ON i.id = r.invoice_id ORDER BY r.id DESC;
        """)
        return cur.fetchall()

    def save_invoice_header(self, customer_id, ref_number, store_name, address, phone, discount_percent, credit, notes, subtotal, discount_amount, total, printable_path=""):
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO invoices (
                customer_id, ref_number, store_name, address, phone, discount_percent, credit, notes,
                subtotal, discount_amount, total, status, payment_method, printable_path, created_at, paid_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, ?, ?, NULL);
        """, (
            customer_id, ref_number.strip(), store_name.strip(), address.strip(), phone.strip(),
            float(discount_percent), float(credit), notes.strip(),
            float(subtotal), float(discount_amount), float(total), printable_path, now_str()
        ))
        self.conn.commit()
        return cur.lastrowid

    def update_invoice_printable_path(self, invoice_id, printable_path):
        self.conn.execute("UPDATE invoices SET printable_path = ? WHERE id = ?;", (printable_path, invoice_id))
        self.conn.commit()

    def save_invoice_line(self, invoice_id, item_id, item_name, qty, unit_price):
        self.conn.execute("""
            INSERT INTO invoice_lines (invoice_id, item_id, item_name, qty, unit_price, line_total)
            VALUES (?, ?, ?, ?, ?, ?);
        """, (invoice_id, item_id, item_name, int(qty), float(unit_price), float(unit_price) * int(qty)))
        self.conn.commit()

    def list_invoices(self):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, created_at, ref_number, COALESCE(store_name,''), COALESCE(phone,''),
                   total, status, COALESCE(payment_method,''), COALESCE(paid_at,'')
            FROM invoices
            ORDER BY id DESC;
        """)
        return cur.fetchall()

    def get_invoice(self, invoice_id):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, ref_number, COALESCE(store_name,''), COALESCE(address,''), COALESCE(phone,''),
                   discount_percent, credit, COALESCE(notes,''), subtotal, discount_amount, total,
                   status, COALESCE(payment_method,''), COALESCE(printable_path,''), created_at, COALESCE(paid_at,'')
            FROM invoices
            WHERE id = ?;
        """, (invoice_id,))
        return cur.fetchone()

    def get_invoice_lines(self, invoice_id):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT item_name, qty, unit_price, line_total
            FROM invoice_lines
            WHERE invoice_id = ?
            ORDER BY id;
        """, (invoice_id,))
        return cur.fetchall()

    def close_invoice(self, invoice_id, payment_method):
        payment_method = payment_method.strip()
        allowed = {"cash", "check", "Zelle", "credit", "other"}
        if payment_method not in allowed:
            raise ValueError("Please select a valid payment method.")
        self.conn.execute("""
            UPDATE invoices
            SET status = 'closed', payment_method = ?, paid_at = ?
            WHERE id = ? AND status != 'void';
        """, (payment_method, now_str(), invoice_id))
        self.conn.commit()

    def void_invoice(self, invoice_id):
        self.conn.execute("""
            UPDATE invoices
            SET status = 'void'
            WHERE id = ? AND status != 'closed';
        """, (invoice_id,))
        self.conn.commit()

    def _month_bounds(self, year, month):
        start = f"{int(year):04d}-{int(month):02d}-01 00:00"
        if int(month) == 12:
            end = f"{int(year) + 1:04d}-01-01 00:00"
        else:
            end = f"{int(year):04d}-{int(month) + 1:02d}-01 00:00"
        return start, end

    def list_invoice_details_for_month(self, year, month):
        start, end = self._month_bounds(year, month)
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, created_at, ref_number, COALESCE(store_name,''), total,
                   status, COALESCE(payment_method,''), COALESCE(paid_at,'')
            FROM invoices
            WHERE created_at >= ? AND created_at < ?
            ORDER BY created_at, id;
        """, (start, end))
        return cur.fetchall()

    def calculate_monthly_statement(self, year, month):
        start, end = self._month_bounds(year, month)
        cur = self.conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*),
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status = 'void' THEN 1 ELSE 0 END),
                COALESCE(SUM(CASE WHEN status != 'void' THEN subtotal ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status != 'void' THEN discount_amount ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status != 'void' THEN credit ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status != 'void' THEN total ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'closed' THEN total ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'open' THEN total ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'void' THEN total ELSE 0 END), 0)
            FROM invoices
            WHERE created_at >= ? AND created_at < ?;
        """, (start, end))
        row = cur.fetchone()
        return {
            "year": int(year),
            "month": int(month),
            "period_label": datetime(int(year), int(month), 1).strftime("%B %Y"),
            "invoice_count": row[0] or 0,
            "open_count": row[1] or 0,
            "closed_count": row[2] or 0,
            "void_count": row[3] or 0,
            "subtotal": row[4] or 0,
            "discount_amount": row[5] or 0,
            "credit": row[6] or 0,
            "total_sales": row[7] or 0,
            "paid_total": row[8] or 0,
            "open_total": row[9] or 0,
            "void_total": row[10] or 0,
        }

    def save_monthly_statement(self, data, printable_path=""):
        self.conn.execute("""
            INSERT INTO monthly_statements (
                year, month, period_label, invoice_count, open_count, closed_count, void_count,
                subtotal, discount_amount, credit, total_sales, paid_total, open_total, void_total,
                printable_path, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(year, month) DO UPDATE SET
                period_label = excluded.period_label,
                invoice_count = excluded.invoice_count,
                open_count = excluded.open_count,
                closed_count = excluded.closed_count,
                void_count = excluded.void_count,
                subtotal = excluded.subtotal,
                discount_amount = excluded.discount_amount,
                credit = excluded.credit,
                total_sales = excluded.total_sales,
                paid_total = excluded.paid_total,
                open_total = excluded.open_total,
                void_total = excluded.void_total,
                printable_path = CASE WHEN excluded.printable_path != '' THEN excluded.printable_path ELSE monthly_statements.printable_path END,
                updated_at = excluded.updated_at;
        """, (
            data["year"], data["month"], data["period_label"], data["invoice_count"],
            data["open_count"], data["closed_count"], data["void_count"],
            data["subtotal"], data["discount_amount"], data["credit"], data["total_sales"],
            data["paid_total"], data["open_total"], data["void_total"],
            printable_path, now_str(), now_str()
        ))
        self.conn.commit()

    def get_monthly_statement(self, year, month):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, year, month, period_label, invoice_count, open_count, closed_count, void_count,
                   subtotal, discount_amount, credit, total_sales, paid_total, open_total, void_total,
                   COALESCE(printable_path,''), created_at, updated_at
            FROM monthly_statements
            WHERE year = ? AND month = ?;
        """, (int(year), int(month)))
        return cur.fetchone()

    def list_monthly_statements(self):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, year, month, period_label, invoice_count, open_count, closed_count, void_count,
                   subtotal, discount_amount, credit, total_sales, paid_total, open_total, void_total,
                   COALESCE(printable_path,''), updated_at
            FROM monthly_statements
            ORDER BY year DESC, month DESC;
        """)
        return cur.fetchall()

    def first_invoice_month(self):
        cur = self.conn.cursor()
        cur.execute("SELECT MIN(created_at) FROM invoices;")
        value = cur.fetchone()[0]
        if not value:
            return None
        try:
            dt = datetime.strptime(value[:7], "%Y-%m")
            return dt.year, dt.month
        except ValueError:
            return None

    def yearly_statement_from_months(self, year):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*),
                COALESCE(SUM(invoice_count), 0),
                COALESCE(SUM(open_count), 0),
                COALESCE(SUM(closed_count), 0),
                COALESCE(SUM(void_count), 0),
                COALESCE(SUM(subtotal), 0),
                COALESCE(SUM(discount_amount), 0),
                COALESCE(SUM(credit), 0),
                COALESCE(SUM(total_sales), 0),
                COALESCE(SUM(paid_total), 0),
                COALESCE(SUM(open_total), 0),
                COALESCE(SUM(void_total), 0)
            FROM monthly_statements
            WHERE year = ?;
        """, (int(year),))
        row = cur.fetchone()
        return {
            "year": int(year),
            "month_count": row[0] or 0,
            "invoice_count": row[1] or 0,
            "open_count": row[2] or 0,
            "closed_count": row[3] or 0,
            "void_count": row[4] or 0,
            "subtotal": row[5] or 0,
            "discount_amount": row[6] or 0,
            "credit": row[7] or 0,
            "total_sales": row[8] or 0,
            "paid_total": row[9] or 0,
            "open_total": row[10] or 0,
            "void_total": row[11] or 0,
        }

    def monthly_statements_for_year(self, year):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT month, period_label, invoice_count, total_sales, paid_total, open_total, void_total
            FROM monthly_statements
            WHERE year = ?
            ORDER BY month;
        """, (int(year),))
        return cur.fetchall()

    def record_shipment(self, ref_number, item_id, qty_add, note=""):
        if qty_add <= 0:
            raise ValueError("Quantity must be > 0.")
        ref_number = ref_number.strip()
        if not ref_number:
            raise ValueError("Shipment number is required.")
        note = note.strip()

        cur = self.conn.cursor()
        cur.execute("SELECT qty FROM items WHERE id = ?;", (item_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError("Item not found.")

        new_qty = row[0] + qty_add

        self.conn.execute("""
            UPDATE items
            SET qty = ?, restock_total = ?, updated_at = ?
            WHERE id = ?;
        """, (new_qty, new_qty, now_str(), item_id))

        self.conn.execute("""
            INSERT INTO txns (type, ref_number, item_id, qty_change, note, created_at)
            VALUES ('SHIPMENT', ?, ?, ?, ?, ?);
        """, (ref_number, item_id, qty_add, note, now_str()))
        self.conn.commit()

    def record_invoice(self, ref_number, item_id, qty_sub, note=""):
        if qty_sub <= 0:
            raise ValueError("Quantity must be > 0.")
        ref_number = ref_number.strip()
        if not ref_number:
            raise ValueError("Invoice number is required.")
        note = note.strip()

        cur = self.conn.cursor()
        cur.execute("SELECT qty FROM items WHERE id = ?;", (item_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError("Item not found.")

        current = row[0]
        if qty_sub > current:
            raise ValueError(f"Not enough stock. Current stock is {current}.")

        new_qty = current - qty_sub

        self.conn.execute("""
            UPDATE items SET qty = ?, updated_at = ? WHERE id = ?;
        """, (new_qty, now_str(), item_id))

        self.conn.execute("""
            INSERT INTO txns (type, ref_number, item_id, qty_change, note, created_at)
            VALUES ('INVOICE', ?, ?, ?, ?, ?);
        """, (ref_number, item_id, -qty_sub, note, now_str()))
        self.conn.commit()

    def list_history(self, limit=200):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT t.created_at, t.type, t.ref_number, i.name, t.qty_change, COALESCE(t.note,'')
            FROM txns t
            JOIN items i ON i.id = t.item_id
            ORDER BY t.id DESC
            LIMIT ?;
        """, (limit,))
        return cur.fetchall()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Simple Inventory")

        # Size the main window to the available display. This works on Windows,
        # Chromebook Linux, and smaller laptop screens without forcing a
        # desktop-sized minimum window.
        self.update_idletasks()
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        window_width = max(900, min(1500, int(screen_width * 0.96)))
        window_height = max(620, min(900, int(screen_height * 0.92)))
        x = max((screen_width - window_width) // 2, 0)
        y = max((screen_height - window_height) // 2, 0)
        self.geometry(f"{window_width}x{window_height}+{x}+{y}")
        self.minsize(min(900, screen_width), min(620, screen_height))

        # Maximize when the window manager supports it, but retain the
        # calculated geometry as a safe fallback on ChromeOS/Linux.
        try:
            if screen_width >= 1200 and screen_height >= 700:
                self.state("zoomed")
        except tk.TclError:
            try:
                if screen_width >= 1200 and screen_height >= 700:
                    self.attributes("-zoomed", True)
            except tk.TclError:
                pass

        # Scale fonts slightly for compact Chromebook displays.
        base_font_size = 10 if screen_width < 1200 or screen_height < 750 else 12
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=("Segoe UI", base_font_size))
        style.configure("Treeview", rowheight=26 if base_font_size == 10 else 30)
        style.configure("Treeview.Heading", font=("Segoe UI", base_font_size, "bold"))
        style.configure("Title.TLabel", font=("Segoe UI", base_font_size + 4, "bold"))
        style.configure("Big.TButton", font=("Segoe UI", base_font_size + 1, "bold"), padding=6)

        self.db = InventoryDB(DB_FILE)

        self.shipment_lines = []
        self.invoice_lines = []

        self._build_ui()
        self._build_menu_bar()
        self.auto_save_completed_monthly_statements()
        self.refresh_all()

    def _build_ui(self):
        header = ttk.Frame(self, padding=12)
        header.pack(fill="x")
        ttk.Label(header, text="Simple Inventory", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text=f"Database: {DB_FILE}", foreground="#666").pack(side="right")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=12)

        self.tab_inventory = ttk.Frame(self.notebook, padding=12)
        self.tab_shipment = ttk.Frame(self.notebook, padding=12)
        self.tab_invoice = ttk.Frame(self.notebook, padding=12)
        self.tab_customers = ttk.Frame(self.notebook, padding=12)
        self.tab_refunds = ttk.Frame(self.notebook, padding=12)
        self.tab_transactions = ttk.Frame(self.notebook, padding=12)
        self.tab_statements = ttk.Frame(self.notebook, padding=12)
        self.tab_history = ttk.Frame(self.notebook, padding=12)

        self.notebook.add(self.tab_inventory, text="Inventory")
        self.notebook.add(self.tab_shipment, text="Add Shipment")
        self.notebook.add(self.tab_invoice, text="Record Invoice")
        self.notebook.add(self.tab_customers, text="Customer Management")
        self.notebook.add(self.tab_refunds, text="Refunds / Accounts")
        self.notebook.add(self.tab_transactions, text="Transactions")
        self.notebook.add(self.tab_statements, text="Statements")
        self.notebook.add(self.tab_history, text="History")

        self._build_inventory_tab()
        self._build_shipment_tab()
        self._build_invoice_tab()
        self._build_customer_management_tab()
        self._build_refunds_tab()
        self._build_transactions_tab()
        self._build_statements_tab()
        self._build_history_tab()

    def _build_menu_bar(self):
        """Create the application menu bar for navigation, exports, reports, and database tools."""
        menu_bar = tk.Menu(self)
        self.config(menu=menu_bar)

        file_menu = tk.Menu(menu_bar, tearoff=False)
        file_menu.add_command(label="New Invoice", command=lambda: self._select_tab(self.tab_invoice))
        file_menu.add_command(label="New Shipment", command=lambda: self._select_tab(self.tab_shipment))
        file_menu.add_separator()
        file_menu.add_command(label="Export Inventory CSV", command=lambda: self.export_treeview_csv(self.tree, "inventory"))
        file_menu.add_command(label="Export Customers CSV", command=lambda: self.export_treeview_csv(self.customer_tree, "customers"))
        file_menu.add_command(label="Export Transactions CSV", command=lambda: self.export_treeview_csv(self.transactions_tree, "transactions"))
        file_menu.add_command(label="Export Statements CSV", command=lambda: self.export_treeview_csv(self.statements_tree, "statements"))
        file_menu.add_command(label="Export History CSV", command=lambda: self.export_treeview_csv(self.hist, "history"))
        file_menu.add_separator()
        file_menu.add_command(label="Backup Database...", command=self.backup_database)
        file_menu.add_command(label="Restore Database...", command=self.restore_database)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.destroy)
        menu_bar.add_cascade(label="File", menu=file_menu)

        reports_menu = tk.Menu(menu_bar, tearoff=False)
        reports_menu.add_command(label="Monthly Statement", command=self._go_to_monthly_statement)
        reports_menu.add_command(label="Yearly Statement", command=self._go_to_yearly_statement)
        reports_menu.add_separator()
        reports_menu.add_command(label="Transactions", command=lambda: self._select_tab(self.tab_transactions))
        reports_menu.add_command(label="Inventory", command=lambda: self._select_tab(self.tab_inventory))
        menu_bar.add_cascade(label="Reports", menu=reports_menu)

        tools_menu = tk.Menu(menu_bar, tearoff=False)
        tools_menu.add_command(label="Customer Management", command=lambda: self._select_tab(self.tab_customers))
        tools_menu.add_command(label="Refunds / Accounts", command=lambda: self._select_tab(self.tab_refunds))
        tools_menu.add_command(label="Item Management", command=lambda: self._select_tab(self.tab_inventory))
        tools_menu.add_command(label="Statements", command=lambda: self._select_tab(self.tab_statements))
        tools_menu.add_command(label="Transaction History", command=lambda: self._select_tab(self.tab_history))
        menu_bar.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(menu_bar, tearoff=False)
        help_menu.add_command(label="About", command=self.show_about)
        menu_bar.add_cascade(label="Help", menu=help_menu)

    def _select_tab(self, tab):
        self.notebook.select(tab)

    def _go_to_monthly_statement(self):
        self._select_tab(self.tab_statements)
        self.after_idle(self.on_generate_monthly_statement)

    def _go_to_yearly_statement(self):
        self._select_tab(self.tab_statements)
        self.after_idle(self.on_generate_yearly_statement)

    def backup_database(self):
        filename = f"inventory_backup_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.db"
        path = filedialog.asksaveasfilename(
            title="Backup Inventory Database",
            defaultextension=".db",
            initialfile=filename,
            filetypes=[("SQLite database", "*.db"), ("All files", "*.*")],
        )
        if not path:
            return

        try:
            backup_conn = sqlite3.connect(path)
            with backup_conn:
                self.db.conn.backup(backup_conn)
            backup_conn.close()
            messagebox.showinfo("Backup Complete", f"Database backup saved to:\n{path}")
        except (sqlite3.Error, OSError) as exc:
            messagebox.showerror("Backup Error", f"Could not back up the database.\n\n{exc}")

    def restore_database(self):
        path = filedialog.askopenfilename(
            title="Restore Inventory Database",
            filetypes=[("SQLite database", "*.db"), ("All files", "*.*")],
        )
        if not path:
            return

        if not messagebox.askyesno(
            "Restore Database",
            "Restoring will replace the current inventory database.\n\n"
            "A safety backup will be created first. Continue?",
        ):
            return

        safety_path = f"{DB_FILE}.before_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
        try:
            # Validate that the selected file is a readable SQLite database.
            test_conn = sqlite3.connect(path)
            test_conn.execute("PRAGMA schema_version;").fetchone()
            test_conn.close()

            self.db.conn.commit()
            self.db.conn.close()
            if os.path.exists(DB_FILE):
                shutil.copy2(DB_FILE, safety_path)
            shutil.copy2(path, DB_FILE)
            self.db = InventoryDB(DB_FILE)
            self.shipment_lines.clear()
            self.invoice_lines.clear()
            self.auto_save_completed_monthly_statements()
            self.refresh_all()
            messagebox.showinfo(
                "Restore Complete",
                f"Database restored successfully.\n\nSafety backup:\n{safety_path}",
            )
        except (sqlite3.Error, OSError, shutil.Error) as exc:
            try:
                if os.path.exists(safety_path):
                    shutil.copy2(safety_path, DB_FILE)
                self.db = InventoryDB(DB_FILE)
                self.refresh_all()
            except Exception:
                pass
            messagebox.showerror("Restore Error", f"Could not restore the database.\n\n{exc}")

    def show_about(self):
        messagebox.showinfo(
            "About Simple Inventory",
            "Simple Inventory\n\n"
            "Inventory, shipments, invoicing, customer management, transactions, "
            "statements, and CSV exports.",
        )

    # ---------- Inventory Tab ----------
    def _build_inventory_tab(self):
        top = ttk.Frame(self.tab_inventory)
        top.pack(fill="x", pady=(0, 10))

        ttk.Label(top, text="Search Item:").pack(side="left")
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(top, textvariable=self.search_var, width=30)
        search_entry.pack(side="left", padx=8)
        ttk.Button(top, text="Search", style="Big.TButton", command=self.refresh_inventory).pack(side="left")
        ttk.Button(top, text="Clear", style="Big.TButton", command=self._clear_search).pack(side="left", padx=(8, 0))

        add_box = ttk.LabelFrame(self.tab_inventory, text="Add New Item", padding=12)
        add_box.pack(fill="x", pady=(0, 10))

        self.new_name = tk.StringVar()
        self.new_sku = tk.StringVar()
        self.new_price = tk.StringVar(value="0.00")
        self.new_weight = tk.StringVar()

        row1 = ttk.Frame(add_box)
        row1.pack(fill="x", pady=4)
        ttk.Label(row1, text="Item Name (required):", width=22).pack(side="left")
        ttk.Entry(row1, textvariable=self.new_name, width=30).pack(side="left", padx=8)
        ttk.Label(row1, text="SKU (optional):", width=14).pack(side="left")
        ttk.Entry(row1, textvariable=self.new_sku, width=18).pack(side="left", padx=8)

        row2 = ttk.Frame(add_box)
        row2.pack(fill="x", pady=4)
        ttk.Label(row2, text="Price:", width=22).pack(side="left")
        ttk.Entry(row2, textvariable=self.new_price, width=10).pack(side="left", padx=8)
        ttk.Label(row2, text="Weight:", width=14).pack(side="left")
        ttk.Entry(row2, textvariable=self.new_weight, width=18).pack(side="left", padx=8)

        row3 = ttk.Frame(add_box)
        row3.pack(fill="x", pady=4)
        ttk.Button(row3, text="Add Item", style="Big.TButton", command=self.on_add_item).pack(side="left")
        ttk.Label(
            row3,
            text="Stock priority is calculated automatically after shipments.",
            foreground="#666"
        ).pack(side="left", padx=12)

        action_row = ttk.Frame(self.tab_inventory)
        action_row.pack(fill="x", pady=(0, 10))
        ttk.Button(action_row, text="Edit Selected Item", style="Big.TButton", command=self.on_edit_item).pack(side="left")
        ttk.Button(action_row, text="Delete Selected Item", style="Big.TButton", command=self.on_delete_item).pack(side="left", padx=(8, 0))
        ttk.Button(action_row, text="Export Inventory CSV", style="Big.TButton",
                   command=lambda: self.export_treeview_csv(self.tree, "inventory")).pack(side="left", padx=(8, 0))
        ttk.Label(action_row, text="Editing keeps the existing stock and transaction history.", foreground="#666").pack(side="left", padx=12)

        table_frame = ttk.Frame(self.tab_inventory)
        table_frame.pack(fill="both", expand=True)

        cols = ("name", "sku", "weight", "price", "qty", "value", "daily_usage", "doh", "priority", "updated")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=12)
        self.tree.heading("name", text="Item Name")
        self.tree.heading("sku", text="SKU")
        self.tree.heading("weight", text="Weight")
        self.tree.heading("price", text="Price")
        self.tree.heading("qty", text="In Stock")
        self.tree.heading("value", text="Inventory Value")
        self.tree.heading("daily_usage", text="Avg Daily Out")
        self.tree.heading("doh", text="Days On Hand")
        self.tree.heading("priority", text="Stock Priority")
        self.tree.heading("updated", text="Last Updated")

        self.tree.column("name", width=240, anchor="w")
        self.tree.column("sku", width=110, anchor="center")
        self.tree.column("weight", width=110, anchor="center")
        self.tree.column("price", width=90, anchor="e")
        self.tree.column("qty", width=90, anchor="center")
        self.tree.column("value", width=130, anchor="e")
        self.tree.column("daily_usage", width=115, anchor="center")
        self.tree.column("doh", width=115, anchor="center")
        self.tree.column("priority", width=150, anchor="center")
        self.tree.column("updated", width=160, anchor="center")

        self.tree.tag_configure("watch", background="#fff2a8")
        self.tree.tag_configure("low", background="#ffd59a")
        self.tree.tag_configure("critical", background="#ffc7ce")

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda event: self.on_edit_item())

        hint = ttk.Label(
            self.tab_inventory,
            text=("Days On Hand = current stock divided by weighted average daily usage. "
                  "Yellow: 30-60 days, orange: 14-29 days, red: under 14 days."),
            foreground="#666"
        )
        hint.pack(anchor="w", pady=(8, 0))

    def _clear_search(self):
        self.search_var.set("")
        self.refresh_inventory()

    def on_add_item(self):
        try:
            self.db.add_item(
                self.new_name.get(),
                self.new_sku.get(),
                self.new_price.get(),
                self.new_weight.get()
            )
            self.new_name.set("")
            self.new_sku.set("")
            self.new_price.set("0.00")
            self.new_weight.set("")
            messagebox.showinfo("Added", "Item added.")
            self.refresh_all()
        except sqlite3.IntegrityError:
            messagebox.showerror("Error", "That item name already exists.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def on_edit_item(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showerror("Error", "Select an item to edit.")
            return

        item_id = int(selected[0])
        details = self.db.get_item_details(item_id)
        if not details:
            messagebox.showerror("Error", "Item not found.")
            return

        _item_id, current_name, current_price, current_weight, current_qty = details
        cur = self.db.conn.cursor()
        cur.execute("SELECT COALESCE(sku,'') FROM items WHERE id = ?;", (item_id,))
        row = cur.fetchone()
        current_sku = row[0] if row else ""

        dialog = tk.Toplevel(self)
        dialog.title("Edit Item")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)

        name_var = tk.StringVar(value=current_name)
        sku_var = tk.StringVar(value=current_sku)
        price_var = tk.StringVar(value=f"{float(current_price):.2f}")
        weight_var = tk.StringVar(value=current_weight)

        ttk.Label(frame, text="Item Name:", width=16).grid(row=0, column=0, sticky="w", pady=6)
        name_entry = ttk.Entry(frame, textvariable=name_var, width=36)
        name_entry.grid(row=0, column=1, sticky="ew", pady=6)

        ttk.Label(frame, text="SKU:", width=16).grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=sku_var, width=36).grid(row=1, column=1, sticky="ew", pady=6)

        ttk.Label(frame, text="Price:", width=16).grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=price_var, width=18).grid(row=2, column=1, sticky="w", pady=6)

        ttk.Label(frame, text="Weight:", width=16).grid(row=3, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=weight_var, width=36).grid(row=3, column=1, sticky="ew", pady=6)

        ttk.Label(frame, text=f"Current stock: {current_qty}", foreground="#666").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(4, 10)
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e")

        def save_changes():
            try:
                self.db.update_item(
                    item_id,
                    name_var.get(),
                    sku_var.get(),
                    price_var.get(),
                    weight_var.get()
                )
                dialog.destroy()
                self.refresh_all()
                messagebox.showinfo("Updated", "Item details updated.")
            except Exception as e:
                messagebox.showerror("Error", str(e), parent=dialog)

        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="Save Changes", style="Big.TButton", command=save_changes).pack(side="right", padx=(0, 8))

        dialog.bind("<Return>", lambda event: save_changes())
        dialog.bind("<Escape>", lambda event: dialog.destroy())
        name_entry.focus_set()

        dialog.update_idletasks()
        x = self.winfo_rootx() + max((self.winfo_width() - dialog.winfo_width()) // 2, 0)
        y = self.winfo_rooty() + max((self.winfo_height() - dialog.winfo_height()) // 2, 0)
        dialog.geometry(f"+{x}+{y}")

    def on_delete_item(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showerror("Error", "Select an item to delete.")
            return

        item_id = int(selected[0])
        values = self.tree.item(selected[0], "values")
        item_name = values[0].replace("⚠ ", "", 1) if values else "this item"

        confirm = messagebox.askyesno(
            "Delete Item",
            f"Delete '{item_name}'?\n\nThis is only allowed if stock is 0 and there is no transaction history."
        )
        if not confirm:
            return

        try:
            self.db.delete_item(item_id)
            messagebox.showinfo("Deleted", "Item deleted.")
            self.refresh_all()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def refresh_inventory(self):
        for row in self.tree.get_children():
            self.tree.delete(row)

        rows = self.db.list_items(self.search_var.get())
        for (
            item_id, name, sku, price, weight, qty,
            sold_0_30, sold_31_60, sold_61_90, updated_at
        ) in rows:
            price_display = money(price)
            value_display = money(price * qty)

            # Weighted 90-day demand rate. Recent sales influence the forecast most.
            recent_daily = sold_0_30 / 30.0
            middle_daily = sold_31_60 / 30.0
            older_daily = sold_61_90 / 30.0
            average_daily_usage = (
                recent_daily * 0.60
                + middle_daily * 0.30
                + older_daily * 0.10
            )

            if qty <= 0:
                days_display = "0.0"
                priority = "Out of Stock"
                tag = "critical"
            elif average_daily_usage <= 0:
                days_display = "—"
                priority = "No recent sales"
                tag = ""
            else:
                days_on_hand = qty / average_daily_usage
                days_display = f"{days_on_hand:.1f}"

                if days_on_hand < 14:
                    priority = "Critical"
                    tag = "critical"
                elif days_on_hand < 30:
                    priority = "Low"
                    tag = "low"
                elif days_on_hand <= 60:
                    priority = "Watch"
                    tag = "watch"
                else:
                    priority = "Healthy"
                    tag = ""

            daily_usage_display = (
                f"{average_daily_usage:.2f}"
                if average_daily_usage > 0
                else "0.00"
            )

            self.tree.insert(
                "", "end", iid=str(item_id),
                values=(
                    name, sku, weight, price_display, qty, value_display,
                    daily_usage_display, days_display, priority, updated_at
                ),
                tags=(tag,) if tag else ()
            )

        self._refresh_item_dropdowns()

    # ---------- Shipment Tab ----------
    def _build_shipment_tab(self):
        box = ttk.LabelFrame(self.tab_shipment, text="Add Shipment (Increase Stock)", padding=12)
        box.pack(fill="x", pady=(0, 10))

        self.ship_ref = tk.StringVar()
        self.ship_qty = tk.StringVar()
        self.ship_note = tk.StringVar()
        self.ship_item = tk.StringVar()

        r1 = ttk.Frame(box); r1.pack(fill="x", pady=6)
        ttk.Label(r1, text="Shipment #:", width=18).pack(side="left")
        ttk.Entry(r1, textvariable=self.ship_ref, width=30).pack(side="left", padx=8)

        r2 = ttk.Frame(box); r2.pack(fill="x", pady=6)
        ttk.Label(r2, text="Item:", width=18).pack(side="left")
        self.ship_item_cb = ttk.Combobox(r2, textvariable=self.ship_item, state="readonly", width=40)
        self.ship_item_cb.pack(side="left", padx=8)
        ttk.Label(r2, text="Qty:").pack(side="left", padx=(12, 0))
        ttk.Entry(r2, textvariable=self.ship_qty, width=10).pack(side="left", padx=8)
        ttk.Button(r2, text="Add Line", style="Big.TButton", command=self.on_add_shipment_line).pack(side="left", padx=(8, 0))

        r3 = ttk.Frame(box); r3.pack(fill="x", pady=6)
        ttk.Label(r3, text="Note (optional):", width=18).pack(side="left")
        ttk.Entry(r3, textvariable=self.ship_note, width=60).pack(side="left", padx=8)

        lines_box = ttk.LabelFrame(self.tab_shipment, text="Shipment Lines To Save", padding=12)
        lines_box.pack(fill="both", expand=True, pady=(0, 10))

        cols = ("item", "qty")
        self.ship_lines_tree = ttk.Treeview(lines_box, columns=cols, show="headings", height=8)
        self.ship_lines_tree.heading("item", text="Item")
        self.ship_lines_tree.heading("qty", text="Quantity Received")
        self.ship_lines_tree.column("item", width=450)
        self.ship_lines_tree.column("qty", width=150, anchor="center")

        vsb = ttk.Scrollbar(lines_box, orient="vertical", command=self.ship_lines_tree.yview)
        self.ship_lines_tree.configure(yscrollcommand=vsb.set)
        self.ship_lines_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        buttons = ttk.Frame(self.tab_shipment)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Remove Selected Line", style="Big.TButton", command=self.on_remove_shipment_line).pack(side="left")
        ttk.Button(buttons, text="Clear Lines", style="Big.TButton", command=self.on_clear_shipment_lines).pack(side="left", padx=8)
        ttk.Button(buttons, text="Save Full Shipment", style="Big.TButton", command=self.on_save_shipment).pack(side="right")

        self.ship_hint = ttk.Label(self.tab_shipment, text="", foreground="#666")
        self.ship_hint.pack(anchor="w", pady=(8, 0))

    def on_add_shipment_line(self):
        try:
            item_name = self.ship_item.get()
            item_id = self._selected_item_id(item_name)
            qty = int(self.ship_qty.get())

            if qty <= 0:
                raise ValueError("Quantity must be > 0.")

            self.shipment_lines.append((item_id, item_name, qty))

            self.ship_qty.set("")
            self.refresh_shipment_lines()

        except Exception as e:
            messagebox.showerror("Error", str(e))

    def on_save_shipment(self):
        try:
            ref = self.ship_ref.get().strip()

            if not ref:
                raise ValueError("Shipment number is required.")

            if not self.shipment_lines:
                raise ValueError("Add at least one item to the shipment list.")

            for item_id, item_name, qty in self.shipment_lines:
                self.db.record_shipment(ref, item_id, qty, self.ship_note.get())

            self.shipment_lines.clear()
            self.ship_ref.set("")
            self.ship_qty.set("")
            self.ship_note.set("")
            self.refresh_shipment_lines()

            messagebox.showinfo("Saved", "Shipment list recorded and stock increased.")
            self.refresh_all()

        except Exception as e:
            messagebox.showerror("Error", str(e))

    def on_remove_shipment_line(self):
        selected = self.ship_lines_tree.selection()
        if not selected:
            messagebox.showerror("Error", "Select a shipment line to remove.")
            return

        index = self.ship_lines_tree.index(selected[0])
        del self.shipment_lines[index]
        self.refresh_shipment_lines()

    def on_clear_shipment_lines(self):
        self.shipment_lines.clear()
        self.refresh_shipment_lines()

    def refresh_shipment_lines(self):
        for row in self.ship_lines_tree.get_children():
            self.ship_lines_tree.delete(row)

        for item_id, item_name, qty in self.shipment_lines:
            self.ship_lines_tree.insert("", "end", values=(item_name, qty))

    # ---------- Invoice Tab ----------
    def _build_invoice_tab(self):
        self.inv_ref = tk.StringVar()
        self.inv_store_name = tk.StringVar()
        self.inv_address = tk.StringVar()
        self.inv_phone = tk.StringVar()
        self.inv_discount = tk.StringVar(value="0")
        self.inv_credit = tk.StringVar(value="0.00")
        self.inv_order_notes = tk.StringVar()
        self.inv_qty = tk.StringVar()
        self.inv_item = tk.StringVar()

        # Two-column workspace. Only the item list expands; the footer remains visible.
        workspace = ttk.Frame(self.tab_invoice)
        workspace.pack(fill="both", expand=True)
        workspace.columnconfigure(0, weight=1, minsize=340)
        workspace.columnconfigure(1, weight=3, minsize=620)
        workspace.rowconfigure(0, weight=1)

        left = ttk.Frame(workspace, padding=(0, 0, 10, 0))
        right = ttk.Frame(workspace, padding=(10, 0, 0, 0))
        left.grid(row=0, column=0, sticky="nsew")
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        # ----- Left: invoice and customer information -----
        details = ttk.LabelFrame(left, text="Invoice Details", padding=12)
        details.pack(fill="x", pady=(0, 10))
        details.columnconfigure(1, weight=1)

        ttk.Label(details, text="Invoice #:").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(details, textvariable=self.inv_ref).grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=5)

        customer = ttk.LabelFrame(left, text="Customer Information", padding=12)
        customer.pack(fill="x", pady=(0, 10))
        customer.columnconfigure(1, weight=1)

        ttk.Label(customer, text="Store Name:").grid(row=0, column=0, sticky="w", pady=5)
        self.inv_store_cb = ttk.Combobox(customer, textvariable=self.inv_store_name)
        self.inv_store_cb.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=5)
        self.inv_store_cb.bind("<<ComboboxSelected>>", self.on_customer_selected)
        self.inv_store_cb.bind("<KeyRelease>", self.on_customer_name_typed)
        self.inv_store_cb.bind("<FocusOut>", self.on_customer_focus_out)

        ttk.Label(customer, text="Phone:").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(customer, textvariable=self.inv_phone).grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=5)

        ttk.Label(customer, text="Address:").grid(row=2, column=0, sticky="nw", pady=5)
        ttk.Entry(customer, textvariable=self.inv_address).grid(row=2, column=1, sticky="ew", padx=(10, 0), pady=5)

        adjustments = ttk.LabelFrame(left, text="Order Adjustments", padding=12)
        adjustments.pack(fill="x", pady=(0, 10))
        adjustments.columnconfigure(1, weight=1)

        ttk.Label(adjustments, text="Discount %:").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(adjustments, textvariable=self.inv_discount, width=12).grid(row=0, column=1, sticky="w", padx=(10, 0), pady=5)

        ttk.Label(adjustments, text="Apply Account Credit $:").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(adjustments, textvariable=self.inv_credit, width=12).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=5)
        self.inv_available_credit_label = ttk.Label(adjustments, text="Available account credit: $0.00", foreground="#666")
        self.inv_available_credit_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 5))

        ttk.Label(adjustments, text="Notes:").grid(row=3, column=0, sticky="nw", pady=5)
        notes_entry = ttk.Entry(adjustments, textvariable=self.inv_order_notes)
        notes_entry.grid(row=3, column=1, sticky="ew", padx=(10, 0), pady=5)

        # ----- Right: add lines and view invoice contents -----
        add_line = ttk.LabelFrame(right, text="Add Invoice Line", padding=12)
        add_line.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        add_line.columnconfigure(1, weight=1)

        ttk.Label(add_line, text="Item:").grid(row=0, column=0, sticky="w")
        self.inv_item_cb = ttk.Combobox(add_line, textvariable=self.inv_item, state="readonly")
        self.inv_item_cb.grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Label(add_line, text="Qty:").grid(row=0, column=2, sticky="w", padx=(8, 0))
        qty_entry = ttk.Entry(add_line, textvariable=self.inv_qty, width=9)
        qty_entry.grid(row=0, column=3, padx=8)
        ttk.Button(add_line, text="Add Line", style="Big.TButton", command=self.on_add_invoice_line).grid(row=0, column=4)
        qty_entry.bind("<Return>", lambda event: self.on_add_invoice_line())

        lines_box = ttk.LabelFrame(right, text="Invoice Lines", padding=10)
        lines_box.grid(row=1, column=0, sticky="nsew")
        lines_box.rowconfigure(0, weight=1)
        lines_box.columnconfigure(0, weight=1)

        cols = ("item", "qty", "price", "line_total")
        self.inv_lines_tree = ttk.Treeview(lines_box, columns=cols, show="headings", height=12)
        self.inv_lines_tree.heading("item", text="Item")
        self.inv_lines_tree.heading("qty", text="Quantity")
        self.inv_lines_tree.heading("price", text="Unit Price")
        self.inv_lines_tree.heading("line_total", text="Line Total")
        self.inv_lines_tree.column("item", width=360, anchor="w")
        self.inv_lines_tree.column("qty", width=90, anchor="center", stretch=False)
        self.inv_lines_tree.column("price", width=110, anchor="e", stretch=False)
        self.inv_lines_tree.column("line_total", width=120, anchor="e", stretch=False)

        vsb = ttk.Scrollbar(lines_box, orient="vertical", command=self.inv_lines_tree.yview)
        self.inv_lines_tree.configure(yscrollcommand=vsb.set)
        self.inv_lines_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        line_buttons = ttk.Frame(right)
        line_buttons.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(line_buttons, text="Remove Selected Line", command=self.on_remove_invoice_line).pack(side="left")
        ttk.Button(line_buttons, text="Clear Lines", command=self.on_clear_invoice_lines).pack(side="left", padx=8)

        # ----- Fixed footer: totals and save action -----
        footer = ttk.Frame(self.tab_invoice, padding=(0, 10, 0, 0))
        footer.pack(fill="x", side="bottom")

        self.inv_subtotal_label = ttk.Label(footer, text="Subtotal: $0.00")
        self.inv_discount_label = ttk.Label(footer, text="Discount: $0.00")
        self.inv_credit_label = ttk.Label(footer, text="Credit: $0.00")
        self.inv_total_label = ttk.Label(footer, text="Total: $0.00", style="Title.TLabel")

        self.inv_subtotal_label.pack(side="left", padx=(0, 18))
        self.inv_discount_label.pack(side="left", padx=(0, 18))
        self.inv_credit_label.pack(side="left", padx=(0, 18))
        self.inv_total_label.pack(side="left")
        ttk.Button(footer, text="Save Full Invoice", style="Big.TButton", command=self.on_save_invoice).pack(side="right")

        self.inv_hint = ttk.Label(
            self.tab_invoice,
            text="Add one or more item lines, then save the complete invoice.",
            foreground="#666"
        )
        self.inv_hint.pack(fill="x", side="bottom", pady=(5, 0))

        self.inv_discount.trace_add("write", lambda *_: self._refresh_invoice_total_labels())
        self.inv_credit.trace_add("write", lambda *_: self._refresh_invoice_total_labels())
    def refresh_customer_dropdowns(self, search_text=""):
        customers = self.db.list_customers(search_text)
        self.customer_lookup = {store: (address, phone, credit) for store, address, phone, credit in customers}
        self.inv_store_cb["values"] = [store for store, _address, _phone, _credit in customers]

    def on_customer_selected(self, event=None):
        customer = self.db.get_customer_by_store_name(self.inv_store_name.get())
        if customer:
            _customer_id, _store_name, address, phone, credit_balance = customer
            self.inv_address.set(address)
            self.inv_phone.set(phone)
            if hasattr(self, "inv_available_credit_label"):
                self.inv_available_credit_label.config(text=f"Available account credit: {money(credit_balance)}")

    def on_customer_name_typed(self, event=None):
        # Refresh the suggestion list as the user types, but leave address/phone alone
        # until an exact saved customer is selected or matched.
        typed = self.inv_store_name.get()
        self.refresh_customer_dropdowns(typed)

        customer = self.db.get_customer_by_store_name(typed)
        if customer:
            _customer_id, _store_name, address, phone, credit_balance = customer
            self.inv_address.set(address)
            self.inv_phone.set(phone)
            if hasattr(self, "inv_available_credit_label"):
                self.inv_available_credit_label.config(text=f"Available account credit: {money(credit_balance)}")

    def on_customer_focus_out(self, event=None):
        # If the typed store name exactly matches a saved customer, fill in details.
        self.on_customer_selected()

    def on_add_invoice_line(self):
        try:
            item_name = self.inv_item.get()
            item_id = self._selected_item_id(item_name)
            qty = int(self.inv_qty.get())

            if qty <= 0:
                raise ValueError("Quantity must be > 0.")

            self.invoice_lines.append((item_id, item_name, qty))
            self.inv_qty.set("")
            self.refresh_invoice_lines()

        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _invoice_totals(self):
        subtotal = 0.0
        for item_id, _item_name, qty in self.invoice_lines:
            item = self.db.get_item_details(item_id)
            price = item[2] if item else 0
            subtotal += price * qty

        discount_percent = float(self.inv_discount.get() or 0)
        credit = float(self.inv_credit.get() or 0)

        if discount_percent < 0:
            raise ValueError("Discount percent must be >= 0.")
        if credit < 0:
            raise ValueError("Credit must be >= 0.")

        discount_amount = subtotal * (discount_percent / 100.0)
        total = subtotal - discount_amount - credit
        if total < 0:
            total = 0.0

        return subtotal, discount_percent, discount_amount, credit, total

    def on_save_invoice(self):
        try:
            ref = self.inv_ref.get().strip()

            if not ref:
                raise ValueError("Invoice number is required.")

            if not self.invoice_lines:
                raise ValueError("Add at least one item to the invoice list.")

            # Pre-check the full invoice so duplicate item lines cannot accidentally
            # subtract more stock than is available.
            totals = {}
            names = {}
            for item_id, item_name, qty in self.invoice_lines:
                totals[item_id] = totals.get(item_id, 0) + qty
                names[item_id] = item_name

            for item_id, total_qty in totals.items():
                item = self.db.get_item_details(item_id)
                if not item:
                    raise ValueError(f"Item not found: {names[item_id]}")
                current_stock = item[4]
                if total_qty > current_stock:
                    raise ValueError(
                        f"Not enough stock for {names[item_id]}. "
                        f"Current stock is {current_stock}, invoice total is {total_qty}."
                    )

            subtotal, discount_percent, discount_amount, credit, total = self._invoice_totals()
            printable_lines = list(self.invoice_lines)
            notes = self.inv_order_notes.get()

            store_name = self.inv_store_name.get().strip()
            address = self.inv_address.get().strip()
            phone = self.inv_phone.get().strip()
            if not store_name:
                raise ValueError("Customer store name is required.")

            existing_customer = self.db.get_customer_by_store_name(store_name)
            if existing_customer:
                customer_id, saved_name, saved_address, saved_phone, saved_credit_balance = existing_customer
                details_changed = (address != saved_address or phone != saved_phone)
                if details_changed:
                    update_saved = messagebox.askyesno(
                        "Customer Information Changed",
                        f"Saved information for {saved_name} is different.\n\n"
                        "Update the saved customer record with the address and phone from this invoice?"
                    )
                    if update_saved:
                        self.db.update_customer(customer_id, store_name, address, phone)
            else:
                customer_id = self.db.save_customer(store_name, address, phone)

            available_credit = self.db.get_customer_credit(customer_id)
            if credit > available_credit + 0.005:
                raise ValueError(
                    f"Account credit entered is {money(credit)}, but {store_name} only has {money(available_credit)} available."
                )

            for item_id, item_name, qty in self.invoice_lines:
                self.db.record_invoice(ref, item_id, qty, notes)

            printable_path = self.create_printable_invoice(
                ref, printable_lines, self.inv_store_name.get(), self.inv_address.get(),
                self.inv_phone.get(), discount_percent, discount_amount, credit, subtotal, total, notes
            )

            invoice_id = self.db.save_invoice_header(
                customer_id,
                ref,
                self.inv_store_name.get(),
                self.inv_address.get(),
                self.inv_phone.get(),
                discount_percent,
                credit,
                notes,
                subtotal,
                discount_amount,
                total,
                printable_path
            )

            for item_id, item_name, qty in printable_lines:
                item = self.db.get_item_details(item_id)
                unit_price = item[2] if item else 0
                self.db.save_invoice_line(invoice_id, item_id, item_name, qty, unit_price)

            if credit > 0:
                self.db.adjust_customer_credit(customer_id, -credit, "APPLIED", f"Invoice {ref}", "Account credit applied to invoice")

            self.invoice_lines.clear()
            self.inv_ref.set("")
            self.inv_store_name.set("")
            self.inv_address.set("")
            self.inv_phone.set("")
            self.inv_discount.set("0")
            self.inv_credit.set("0.00")
            self.inv_order_notes.set("")
            self.inv_qty.set("")
            self.refresh_invoice_lines()
            self.refresh_customer_dropdowns()

            messagebox.showinfo(
                "Saved",
                "Invoice list recorded and stock decreased.\n\n"
                f"Printable invoice created:\n{printable_path}"
            )
            try:
                webbrowser.open(f"file://{os.path.abspath(printable_path)}")
            except Exception:
                pass

            self.refresh_all()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def create_printable_invoice(self, ref, lines, store_name, address, phone, discount_percent, discount_amount, credit, subtotal, total, notes=""):
        invoice_dir = "printable_invoices"
        os.makedirs(invoice_dir, exist_ok=True)

        created_at = now_str()
        file_name = f"invoice_{safe_filename(ref)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        path = os.path.join(invoice_dir, file_name)

        rows_html = []
        for index, (item_id, item_name, qty) in enumerate(lines, start=1):
            item = self.db.get_item_details(item_id)
            price = item[2] if item else 0
            line_total = price * qty
            rows_html.append(
                "<tr>"
                f"<td>{index}</td>"
                f"<td>{escape(item_name)}</td>"
                f"<td class='qty'>{qty}</td>"
                f"<td class='money'>{money(price)}</td>"
                f"<td class='money'>{money(line_total)}</td>"
                "</tr>"
            )

        customer_html = f"""
        <div class="customer">
            <h2>Customer</h2>
            <strong>Store:</strong> {escape(store_name) if store_name.strip() else '<span class="muted">Not provided</span>'}<br>
            <strong>Address:</strong> {escape(address) if address.strip() else '<span class="muted">Not provided</span>'}<br>
            <strong>Phone:</strong> {escape(phone) if phone.strip() else '<span class="muted">Not provided</span>'}
        </div>
        """

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Invoice {escape(ref)}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; color: #222; }}
        .header {{ display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 2px solid #222; padding-bottom: 16px; margin-bottom: 24px; }}
        h1 {{ margin: 0; font-size: 30px; }}
        h2 {{ margin: 0 0 8px 0; font-size: 18px; }}
        .muted {{ color: #666; }}
        .details, .customer {{ margin-bottom: 24px; line-height: 1.6; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
        th, td {{ border: 1px solid #ccc; padding: 10px; text-align: left; }}
        th {{ background: #f0f0f0; }}
        .qty {{ text-align: center; }}
        .money {{ text-align: right; }}
        .totals {{ margin-left: auto; margin-top: 18px; width: 360px; }}
        .totals td {{ border: none; padding: 6px 0; }}
        .totals .label {{ text-align: right; padding-right: 18px; }}
        .totals .amount {{ text-align: right; }}
        .grand-total td {{ font-size: 20px; font-weight: bold; border-top: 2px solid #222; padding-top: 10px; }}
        .note {{ margin-top: 24px; padding: 12px; border: 1px solid #ccc; min-height: 50px; }}
        .print-button {{ margin-bottom: 20px; padding: 10px 16px; font-size: 16px; }}
        @media print {{ .print-button {{ display: none; }} body {{ margin: 24px; }} }}
    </style>
</head>
<body>
    <button class="print-button" onclick="window.print()">Print Invoice</button>

    <div class="header">
        <div>
            <h1>Invoice</h1>
            <div class="muted">Inventory items removed from stock</div>
        </div>
        <div>
            <strong>Invoice #:</strong> {escape(ref)}<br>
            <strong>Date:</strong> {escape(created_at)}
        </div>
    </div>

    {customer_html}

    <table>
        <thead>
            <tr>
                <th style="width: 70px;">#</th>
                <th>Item</th>
                <th style="width: 130px; text-align: center;">Qty</th>
                <th style="width: 130px; text-align: right;">Unit Price</th>
                <th style="width: 150px; text-align: right;">Line Total</th>
            </tr>
        </thead>
        <tbody>
            {''.join(rows_html)}
        </tbody>
    </table>

    <table class="totals">
        <tr><td class="label">Subtotal</td><td class="amount">{money(subtotal)}</td></tr>
        <tr><td class="label">Discount ({discount_percent:g}%)</td><td class="amount">- {money(discount_amount)}</td></tr>
        <tr><td class="label">Credit</td><td class="amount">- {money(credit)}</td></tr>
        <tr class="grand-total"><td class="label">Total</td><td class="amount">{money(total)}</td></tr>
    </table>

    <div class="note">
        <strong>Notes:</strong><br>
        {escape(notes) if notes.strip() else '<span class="muted">No notes entered.</span>'}
    </div>
</body>
</html>
"""

        with open(path, "w", encoding="utf-8") as f:
            f.write(html)

        return os.path.abspath(path)

    def on_remove_invoice_line(self):
        selected = self.inv_lines_tree.selection()
        if not selected:
            messagebox.showerror("Error", "Select an invoice line to remove.")
            return

        index = self.inv_lines_tree.index(selected[0])
        del self.invoice_lines[index]
        self.refresh_invoice_lines()

    def on_clear_invoice_lines(self):
        self.invoice_lines.clear()
        self.refresh_invoice_lines()

    def _refresh_invoice_total_labels(self):
        if not hasattr(self, "inv_subtotal_label"):
            return
        try:
            subtotal, _discount_percent, discount_amount, credit, total = self._invoice_totals()
        except (ValueError, tk.TclError):
            subtotal = 0.0
            for item_id, _item_name, qty in self.invoice_lines:
                item = self.db.get_item_details(item_id)
                subtotal += (item[2] if item else 0) * qty
            discount_amount = 0.0
            credit = 0.0
            total = subtotal

        self.inv_subtotal_label.config(text=f"Subtotal: {money(subtotal)}")
        self.inv_discount_label.config(text=f"Discount: {money(discount_amount)}")
        self.inv_credit_label.config(text=f"Credit: {money(credit)}")
        self.inv_total_label.config(text=f"Total: {money(total)}")

    def refresh_invoice_lines(self):
        for row in self.inv_lines_tree.get_children():
            self.inv_lines_tree.delete(row)

        for item_id, item_name, qty in self.invoice_lines:
            item = self.db.get_item_details(item_id)
            price = item[2] if item else 0
            self.inv_lines_tree.insert("", "end", values=(item_name, qty, money(price), money(price * qty)))

        self._refresh_invoice_total_labels()

    # ---------- Customer Management Tab ----------
    def _build_customer_management_tab(self):
        self.customer_selected_id = None

        search_row = ttk.Frame(self.tab_customers)
        search_row.pack(fill="x", pady=(0, 10))
        ttk.Label(search_row, text="Search Customers:").pack(side="left")
        self.customer_search_var = tk.StringVar()
        customer_search = ttk.Entry(search_row, textvariable=self.customer_search_var, width=35)
        customer_search.pack(side="left", padx=8)
        customer_search.bind("<Return>", lambda event: self.refresh_customer_management())
        ttk.Button(search_row, text="Search", style="Big.TButton", command=self.refresh_customer_management).pack(side="left")
        ttk.Button(search_row, text="Clear", style="Big.TButton", command=self.clear_customer_search).pack(side="left", padx=8)
        ttk.Button(search_row, text="Export Customers CSV", style="Big.TButton",
                   command=lambda: self.export_treeview_csv(self.customer_tree, "customers")).pack(side="left", padx=8)

        content = ttk.Panedwindow(self.tab_customers, orient="horizontal")
        content.pack(fill="both", expand=True)

        left = ttk.Frame(content, padding=(0, 0, 8, 0))
        right = ttk.Frame(content, padding=(8, 0, 0, 0))
        content.add(left, weight=3)
        content.add(right, weight=2)

        table_box = ttk.LabelFrame(left, text="Saved Customers", padding=8)
        table_box.pack(fill="both", expand=True)
        cols = ("store", "address", "phone", "credit", "updated")
        self.customer_tree = ttk.Treeview(table_box, columns=cols, show="headings", height=16)
        for c, title, width, anchor in [
            ("store", "Store Name", 210, "w"),
            ("address", "Address", 310, "w"),
            ("phone", "Phone", 150, "center"),
            ("credit", "Account Credit", 130, "e"),
            ("updated", "Last Updated", 155, "center"),
        ]:
            self.customer_tree.heading(c, text=title)
            self.customer_tree.column(c, width=width, anchor=anchor)
        customer_scroll = ttk.Scrollbar(table_box, orient="vertical", command=self.customer_tree.yview)
        self.customer_tree.configure(yscrollcommand=customer_scroll.set)
        self.customer_tree.pack(side="left", fill="both", expand=True)
        customer_scroll.pack(side="right", fill="y")
        self.customer_tree.bind("<<TreeviewSelect>>", self.on_customer_management_selected)

        form = ttk.LabelFrame(right, text="Customer Details", padding=12)
        form.pack(fill="x")
        self.manage_store_var = tk.StringVar()
        self.manage_phone_var = tk.StringVar()

        ttk.Label(form, text="Store Name:").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.manage_store_var, width=38).grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=5)
        ttk.Label(form, text="Phone:").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.manage_phone_var, width=38).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=5)
        ttk.Label(form, text="Address:").grid(row=2, column=0, sticky="nw", pady=5)
        self.manage_address_text = tk.Text(form, width=38, height=4, font=("Segoe UI", 12), wrap="word")
        self.manage_address_text.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=5)
        form.columnconfigure(1, weight=1)

        buttons = ttk.Frame(form)
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(buttons, text="Add New", style="Big.TButton", command=self.on_add_managed_customer).pack(side="left")
        ttk.Button(buttons, text="Save Changes", style="Big.TButton", command=self.on_update_managed_customer).pack(side="left", padx=8)
        ttk.Button(buttons, text="Delete", style="Big.TButton", command=self.on_delete_managed_customer).pack(side="left")
        ttk.Button(buttons, text="Clear Form", style="Big.TButton", command=self.clear_customer_form).pack(side="left", padx=8)

        credit_box = ttk.LabelFrame(right, text="Account Credit History", padding=8)
        credit_box.pack(fill="both", expand=True, pady=(10, 0))
        credit_cols = ("date", "type", "amount", "reference")
        self.customer_credit_tree = ttk.Treeview(credit_box, columns=credit_cols, show="headings", height=6)
        for c, title, width, anchor in [
            ("date", "Date", 145, "center"), ("type", "Type", 90, "center"),
            ("amount", "Amount", 100, "e"), ("reference", "Reference", 220, "w")
        ]:
            self.customer_credit_tree.heading(c, text=title)
            self.customer_credit_tree.column(c, width=width, anchor=anchor)
        self.customer_credit_tree.pack(fill="both", expand=True)

        history_box = ttk.LabelFrame(right, text="Customer Invoice History", padding=8)
        history_box.pack(fill="both", expand=True, pady=(10, 0))
        hist_cols = ("date", "ref", "total", "status", "payment")
        self.customer_history_tree = ttk.Treeview(history_box, columns=hist_cols, show="headings", height=9)
        for c, title, width, anchor in [
            ("date", "Date", 145, "center"),
            ("ref", "Invoice #", 100, "center"),
            ("total", "Total", 95, "e"),
            ("status", "Status", 90, "center"),
            ("payment", "Payment", 100, "center"),
        ]:
            self.customer_history_tree.heading(c, text=title)
            self.customer_history_tree.column(c, width=width, anchor=anchor)
        hist_scroll = ttk.Scrollbar(history_box, orient="vertical", command=self.customer_history_tree.yview)
        self.customer_history_tree.configure(yscrollcommand=hist_scroll.set)
        self.customer_history_tree.pack(side="left", fill="both", expand=True)
        hist_scroll.pack(side="right", fill="y")
        self.customer_history_tree.bind("<Double-1>", lambda event: self.open_selected_customer_invoice())

    def refresh_customer_management(self):
        if not hasattr(self, "customer_tree"):
            return
        for row in self.customer_tree.get_children():
            self.customer_tree.delete(row)
        for customer_id, store, address, phone, credit_balance, updated in self.db.list_customer_records(self.customer_search_var.get()):
            self.customer_tree.insert("", "end", iid=str(customer_id), values=(store, address, phone, money(credit_balance), updated))

    def clear_customer_search(self):
        self.customer_search_var.set("")
        self.refresh_customer_management()

    def clear_customer_form(self):
        self.customer_selected_id = None
        self.manage_store_var.set("")
        self.manage_phone_var.set("")
        self.manage_address_text.delete("1.0", "end")
        for row in self.customer_history_tree.get_children():
            self.customer_history_tree.delete(row)
        for selected in self.customer_tree.selection():
            self.customer_tree.selection_remove(selected)

    def on_customer_management_selected(self, event=None):
        selected = self.customer_tree.selection()
        if not selected:
            return
        self.customer_selected_id = int(selected[0])
        values = self.customer_tree.item(selected[0], "values")
        self.manage_store_var.set(values[0])
        self.manage_address_text.delete("1.0", "end")
        self.manage_address_text.insert("1.0", values[1])
        self.manage_phone_var.set(values[2])
        self.refresh_customer_invoice_history(self.customer_selected_id)
        self.refresh_customer_credit_history(self.customer_selected_id)

    def refresh_customer_invoice_history(self, customer_id):
        for row in self.customer_history_tree.get_children():
            self.customer_history_tree.delete(row)
        for invoice_id, created_at, ref, total, status, payment in self.db.list_customer_invoices(customer_id):
            self.customer_history_tree.insert(
                "", "end", iid=str(invoice_id),
                values=(created_at, ref, money(total), self._status_display(status), payment)
            )

    def refresh_customer_credit_history(self, customer_id):
        if not hasattr(self, "customer_credit_tree"):
            return
        for row in self.customer_credit_tree.get_children():
            self.customer_credit_tree.delete(row)
        if customer_id is None:
            return
        for created_at, entry_type, amount, reference, note in self.db.list_customer_credit_history(customer_id):
            display_amount = money(abs(amount))
            display_amount = ("-" if amount < 0 else "+") + display_amount
            self.customer_credit_tree.insert("", "end", values=(created_at, entry_type.title(), display_amount, reference))

    def on_add_managed_customer(self):
        try:
            store = self.manage_store_var.get().strip()
            if not store:
                raise ValueError("Store name is required.")
            if self.db.get_customer_by_store_name(store):
                raise ValueError("That store name already exists. Select it and use Save Changes.")
            self.db.save_customer(store, self.manage_address_text.get("1.0", "end").strip(), self.manage_phone_var.get())
            messagebox.showinfo("Saved", "Customer added.")
            self.clear_customer_form()
            self.refresh_customer_management()
            self.refresh_customer_dropdowns()
        except sqlite3.IntegrityError:
            messagebox.showerror("Error", "That store name already exists.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def on_update_managed_customer(self):
        try:
            if self.customer_selected_id is None:
                raise ValueError("Select a customer first.")
            self.db.update_customer(
                self.customer_selected_id,
                self.manage_store_var.get(),
                self.manage_address_text.get("1.0", "end").strip(),
                self.manage_phone_var.get()
            )
            messagebox.showinfo("Updated", "Customer information updated.")
            self.refresh_customer_management()
            self.refresh_customer_dropdowns()
        except sqlite3.IntegrityError:
            messagebox.showerror("Error", "That store name already exists.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def on_delete_managed_customer(self):
        try:
            if self.customer_selected_id is None:
                raise ValueError("Select a customer first.")
            store = self.manage_store_var.get().strip()
            if not messagebox.askyesno(
                "Delete Customer",
                f"Delete {store} from saved customers?\n\nExisting invoices will remain unchanged."
            ):
                return
            self.db.delete_customer(self.customer_selected_id)
            self.clear_customer_form()
            self.refresh_customer_management()
            self.refresh_customer_dropdowns()
            messagebox.showinfo("Deleted", "Customer removed from saved customers.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def open_selected_customer_invoice(self):
        selected = self.customer_history_tree.selection()
        if selected:
            self.open_transaction_detail(int(selected[0]))

    # ---------- Refunds / Customer Accounts Tab ----------
    def _build_refunds_tab(self):
        self.refund_lines = []
        self.refund_invoice_var = tk.StringVar()
        self.refund_method_var = tk.StringVar(value="store credit")
        self.refund_note_var = tk.StringVar()
        self.refund_qty_var = tk.StringVar()
        self.refund_invoice_lookup = {}

        top = ttk.LabelFrame(self.tab_refunds, text="Create Customer Refund", padding=12)
        top.pack(fill="x", pady=(0, 10))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Invoice:").grid(row=0, column=0, sticky="w")
        self.refund_invoice_cb = ttk.Combobox(top, textvariable=self.refund_invoice_var, state="readonly")
        self.refund_invoice_cb.grid(row=0, column=1, sticky="ew", padx=8)
        self.refund_invoice_cb.bind("<<ComboboxSelected>>", self.on_refund_invoice_selected)
        ttk.Button(top, text="Refresh Invoices", command=self.refresh_refund_invoice_choices).grid(row=0, column=2)
        ttk.Label(top, text="Refund Method:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(top, textvariable=self.refund_method_var, state="readonly", width=22,
                     values=("store credit", "cash", "original payment", "other")).grid(row=1, column=1, sticky="w", padx=8, pady=(8, 0))
        ttk.Label(top, text="Note:").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(top, textvariable=self.refund_note_var).grid(row=2, column=1, columnspan=2, sticky="ew", padx=8, pady=(8, 0))

        middle = ttk.Panedwindow(self.tab_refunds, orient="horizontal")
        middle.pack(fill="both", expand=True)
        available_box = ttk.LabelFrame(middle, text="Refundable Invoice Items", padding=10)
        selected_box = ttk.LabelFrame(middle, text="Items Being Returned", padding=10)
        middle.add(available_box, weight=1); middle.add(selected_box, weight=1)
        cols = ("item", "sold", "refunded", "available", "price")
        self.refund_available_tree = ttk.Treeview(available_box, columns=cols, show="headings", height=14)
        for c, title, width, anchor in [("item","Item",230,"w"),("sold","Sold",70,"center"),("refunded","Already Returned",110,"center"),("available","Available",80,"center"),("price","Unit Price",90,"e")]:
            self.refund_available_tree.heading(c, text=title); self.refund_available_tree.column(c, width=width, anchor=anchor)
        self.refund_available_tree.pack(fill="both", expand=True)
        add_row = ttk.Frame(available_box); add_row.pack(fill="x", pady=(8,0))
        ttk.Label(add_row, text="Return Qty:").pack(side="left")
        ttk.Entry(add_row, textvariable=self.refund_qty_var, width=9).pack(side="left", padx=8)
        ttk.Button(add_row, text="Add Return Line", command=self.on_add_refund_line).pack(side="left")

        rcols = ("item", "qty", "price", "total")
        self.refund_selected_tree = ttk.Treeview(selected_box, columns=rcols, show="headings", height=14)
        for c, title, width, anchor in [("item","Item",240,"w"),("qty","Qty",70,"center"),("price","Unit Price",90,"e"),("total","Refund",100,"e")]:
            self.refund_selected_tree.heading(c, text=title); self.refund_selected_tree.column(c, width=width, anchor=anchor)
        self.refund_selected_tree.pack(fill="both", expand=True)
        selected_buttons = ttk.Frame(selected_box); selected_buttons.pack(fill="x", pady=(8,0))
        ttk.Button(selected_buttons, text="Remove Selected", command=self.on_remove_refund_line).pack(side="left")
        ttk.Button(selected_buttons, text="Clear", command=self.on_clear_refund_lines).pack(side="left", padx=8)
        self.refund_total_label = ttk.Label(selected_buttons, text="Refund Total: $0.00", style="Title.TLabel")
        self.refund_total_label.pack(side="right")

        bottom = ttk.Frame(self.tab_refunds); bottom.pack(fill="x", pady=(10,0))
        ttk.Button(bottom, text="Save Refund", style="Big.TButton", command=self.on_save_refund).pack(side="right")
        history = ttk.LabelFrame(self.tab_refunds, text="Refund History", padding=8)
        history.pack(fill="both", expand=False, pady=(10,0))
        hcols = ("date", "invoice", "customer", "method", "total", "note")
        self.refund_history_tree = ttk.Treeview(history, columns=hcols, show="headings", height=6)
        for c, title, width, anchor in [("date","Date",145,"center"),("invoice","Invoice",100,"center"),("customer","Customer",220,"w"),("method","Method",120,"center"),("total","Total",100,"e"),("note","Note",260,"w")]:
            self.refund_history_tree.heading(c, text=title); self.refund_history_tree.column(c, width=width, anchor=anchor)
        self.refund_history_tree.pack(fill="both", expand=True)
        self.refresh_refund_invoice_choices(); self.refresh_refund_history()

    def refresh_refund_invoice_choices(self):
        rows = self.db.list_refundable_invoices(); self.refund_invoice_lookup = {}; labels = []
        for invoice_id, ref, store, created_at, total, status in rows:
            label = f"{ref} | {store or 'No customer'} | {created_at} | {money(total)}"
            labels.append(label); self.refund_invoice_lookup[label] = invoice_id
        self.refund_invoice_cb["values"] = labels
        if self.refund_invoice_var.get() not in labels: self.refund_invoice_var.set("")
        self.on_refund_invoice_selected()

    def on_refund_invoice_selected(self, event=None):
        self.refund_lines.clear(); self.refresh_refund_selected_lines()
        for row in self.refund_available_tree.get_children(): self.refund_available_tree.delete(row)
        invoice_id = self.refund_invoice_lookup.get(self.refund_invoice_var.get())
        if not invoice_id: return
        for line_id, item_id, item_name, sold_qty, refunded_qty, unit_price in self.db.get_refundable_invoice_lines(invoice_id):
            available = sold_qty - refunded_qty
            self.refund_available_tree.insert("", "end", iid=str(line_id), values=(item_name, sold_qty, refunded_qty, available, money(unit_price)))

    def on_add_refund_line(self):
        try:
            selected = self.refund_available_tree.selection()
            if not selected: raise ValueError("Select an invoice item first.")
            line_id = int(selected[0]); qty = int(self.refund_qty_var.get())
            values = self.refund_available_tree.item(selected[0], "values"); available = int(values[3])
            if qty <= 0 or qty > available: raise ValueError(f"Return quantity must be between 1 and {available}.")
            unit_price = float(str(values[4]).replace("$", "").replace(",", ""))
            if any(line[0] == line_id for line in self.refund_lines): raise ValueError("That line is already in the return list.")
            self.refund_lines.append((line_id, values[0], qty, unit_price)); self.refund_qty_var.set(""); self.refresh_refund_selected_lines()
        except Exception as e: messagebox.showerror("Error", str(e))

    def refresh_refund_selected_lines(self):
        for row in self.refund_selected_tree.get_children(): self.refund_selected_tree.delete(row)
        total = 0.0
        for line_id, item_name, qty, unit_price in self.refund_lines:
            line_total = qty * unit_price; total += line_total
            self.refund_selected_tree.insert("", "end", iid=str(line_id), values=(item_name, qty, money(unit_price), money(line_total)))
        self.refund_total_label.config(text=f"Refund Total: {money(total)}")

    def on_remove_refund_line(self):
        selected = self.refund_selected_tree.selection()
        if selected:
            line_id = int(selected[0]); self.refund_lines = [line for line in self.refund_lines if line[0] != line_id]; self.refresh_refund_selected_lines()

    def on_clear_refund_lines(self):
        self.refund_lines.clear(); self.refresh_refund_selected_lines()

    def on_save_refund(self):
        try:
            invoice_id = self.refund_invoice_lookup.get(self.refund_invoice_var.get())
            if not invoice_id: raise ValueError("Select an invoice first.")
            refund_id, total = self.db.save_refund(invoice_id, self.refund_method_var.get(), [(line_id, qty) for line_id, _name, qty, _price in self.refund_lines], self.refund_note_var.get())
            messagebox.showinfo(
                "Refund Saved",
                f"Refund #{refund_id} saved for {money(total)} using {self.refund_method_var.get()}.\n\n"
                "Returned inventory has been restored."
            )
            self.refund_note_var.set(""); self.refund_lines.clear(); self.refresh_refund_invoice_choices(); self.refresh_refund_history(); self.refresh_all()
        except Exception as e: messagebox.showerror("Error", str(e))

    def refresh_refund_history(self):
        for row in self.refund_history_tree.get_children(): self.refund_history_tree.delete(row)
        for refund_id, created_at, ref, store, method, total, note in self.db.list_refunds():
            self.refund_history_tree.insert("", "end", iid=str(refund_id), values=(created_at, ref, store, method.title(), money(total), note))

    # ---------- Transactions Tab ----------
    def _status_display(self, status):
        status = (status or "").lower()
        if status == "open":
            return "🟡 Open"
        if status == "closed":
            return "🟢 Closed"
        if status == "void":
            return "🔴 Void"
        return status.title()

    def _build_transactions_tab(self):
        top = ttk.Frame(self.tab_transactions)
        top.pack(fill="x", pady=(0, 10))
        ttk.Button(top, text="Refresh", style="Big.TButton", command=self.refresh_transactions).pack(side="left")
        ttk.Button(top, text="View Selected Invoice", style="Big.TButton", command=self.on_view_transaction).pack(side="left", padx=8)
        ttk.Button(top, text="Export Transactions CSV", style="Big.TButton",
                   command=lambda: self.export_treeview_csv(self.transactions_tree, "transactions")).pack(side="left", padx=8)
        ttk.Label(
            top,
            text="Invoices start as open. Selecting a payment method in the detail window closes the invoice.",
            foreground="#666"
        ).pack(side="left", padx=12)

        table_frame = ttk.Frame(self.tab_transactions)
        table_frame.pack(fill="both", expand=True)

        cols = ("time", "ref", "store", "phone", "total", "status", "payment", "paid")
        self.transactions_tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=16)
        for c, title, w, anchor in [
            ("time", "Date", 160, "center"),
            ("ref", "Invoice #", 140, "center"),
            ("store", "Store", 240, "w"),
            ("phone", "Phone", 140, "center"),
            ("total", "Total", 120, "e"),
            ("status", "Status", 100, "center"),
            ("payment", "Payment", 120, "center"),
            ("paid", "Paid At", 160, "center"),
        ]:
            self.transactions_tree.heading(c, text=title)
            self.transactions_tree.column(c, width=w, anchor=anchor)

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.transactions_tree.yview)
        self.transactions_tree.configure(yscrollcommand=vsb.set)
        self.transactions_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.transactions_tree.tag_configure("open", background="#fff8d6")
        self.transactions_tree.tag_configure("closed", background="#ddf5dd")
        self.transactions_tree.tag_configure("void", background="#f8dddd")
        self.transactions_tree.bind("<Double-1>", lambda event: self.on_view_transaction())

    def refresh_transactions(self):
        for row in self.transactions_tree.get_children():
            self.transactions_tree.delete(row)

        for invoice_id, created_at, ref, store, phone, total, status, payment, paid_at in self.db.list_invoices():
            status_key = (status or "").lower()
            self.transactions_tree.insert(
                "", "end", iid=str(invoice_id), tags=(status_key,),
                values=(created_at, ref, store, phone, money(total), self._status_display(status), payment, paid_at)
            )

    def on_view_transaction(self):
        selected = self.transactions_tree.selection()
        if not selected:
            messagebox.showerror("Error", "Select an invoice transaction first.")
            return
        self.open_transaction_detail(int(selected[0]))

    def open_transaction_detail(self, invoice_id):
        invoice = self.db.get_invoice(invoice_id)
        if not invoice:
            messagebox.showerror("Error", "Invoice not found.")
            return

        (
            _id, ref, store, address, phone, discount_percent, credit, notes,
            subtotal, discount_amount, total, status, payment_method, printable_path,
            created_at, paid_at
        ) = invoice

        win = tk.Toplevel(self)
        win.title(f"Invoice {ref}")
        win.transient(self)

        # Keep invoice details inside the current screen on smaller laptops
        # and Chromebook displays.
        screen_width = win.winfo_screenwidth()
        screen_height = win.winfo_screenheight()
        detail_width = max(760, min(1200, int(screen_width * 0.90)))
        detail_height = max(560, min(760, int(screen_height * 0.86)))
        detail_x = max((screen_width - detail_width) // 2, 0)
        detail_y = max((screen_height - detail_height) // 2, 0)
        win.geometry(f"{detail_width}x{detail_height}+{detail_x}+{detail_y}")
        win.minsize(min(760, screen_width), min(560, screen_height))

        summary = ttk.LabelFrame(win, text="Invoice Details", padding=12)
        summary.pack(fill="x", padx=12, pady=12)

        row1 = ttk.Frame(summary); row1.pack(fill="x", pady=3)
        ttk.Label(row1, text=f"Invoice #: {ref}", width=28).pack(side="left")
        ttk.Label(row1, text=f"Date: {created_at}", width=28).pack(side="left")
        ttk.Label(row1, text=f"Status: {self._status_display(status)}", width=18).pack(side="left")

        row2 = ttk.Frame(summary); row2.pack(fill="x", pady=3)
        ttk.Label(row2, text=f"Store: {store or 'Not provided'}", width=42).pack(side="left")
        ttk.Label(row2, text=f"Phone: {phone or 'Not provided'}").pack(side="left")

        row3 = ttk.Frame(summary); row3.pack(fill="x", pady=3)
        ttk.Label(row3, text=f"Address: {address or 'Not provided'}").pack(side="left")

        lines_box = ttk.LabelFrame(win, text="Invoice Lines", padding=12)
        lines_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        cols = ("item", "qty", "price", "line_total")
        lines_tree = ttk.Treeview(lines_box, columns=cols, show="headings", height=10)
        lines_tree.heading("item", text="Item")
        lines_tree.heading("qty", text="Qty")
        lines_tree.heading("price", text="Unit Price")
        lines_tree.heading("line_total", text="Line Total")
        lines_tree.column("item", width=430, anchor="w")
        lines_tree.column("qty", width=90, anchor="center")
        lines_tree.column("price", width=120, anchor="e")
        lines_tree.column("line_total", width=130, anchor="e")
        lines_tree.pack(fill="both", expand=True)

        for item_name, qty, unit_price, line_total in self.db.get_invoice_lines(invoice_id):
            lines_tree.insert("", "end", values=(item_name, qty, money(unit_price), money(line_total)))

        totals_box = ttk.LabelFrame(win, text="Totals / Payment", padding=12)
        totals_box.pack(fill="x", padx=12, pady=(0, 12))

        totals_row = ttk.Frame(totals_box); totals_row.pack(fill="x", pady=4)
        ttk.Label(totals_row, text=f"Subtotal: {money(subtotal)}", width=22).pack(side="left")
        ttk.Label(totals_row, text=f"Discount ({discount_percent:g}%): -{money(discount_amount)}", width=28).pack(side="left")
        ttk.Label(totals_row, text=f"Credit: -{money(credit)}", width=20).pack(side="left")
        ttk.Label(totals_row, text=f"Total: {money(total)}", font=("Segoe UI", 12, "bold")).pack(side="left")

        payment_row = ttk.Frame(totals_box); payment_row.pack(fill="x", pady=8)
        ttk.Label(payment_row, text="Payment Method:", width=18).pack(side="left")
        payment_var = tk.StringVar(value=payment_method if payment_method else "")
        payment_cb = ttk.Combobox(
            payment_row,
            textvariable=payment_var,
            state="readonly",
            values=("cash", "check", "Zelle", "credit", "other"),
            width=18
        )
        payment_cb.pack(side="left", padx=8)
        if status == "closed":
            ttk.Label(payment_row, text=f"Closed/Paid: {paid_at}", foreground="#666").pack(side="left", padx=12)

        note_box = ttk.LabelFrame(win, text="Notes", padding=12)
        note_box.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Label(note_box, text=notes if notes else "No notes entered.", wraplength=760).pack(anchor="w")

        # Dock detail-window buttons to the bottom so payment/void actions stay visible.
        buttons = ttk.Frame(win, padding=12)
        buttons.pack(side="bottom", fill="x")

        def save_payment():
            try:
                if status == "void":
                    raise ValueError("Void invoices cannot be closed.")
                if not payment_var.get():
                    raise ValueError("Select a payment method first.")
                self.db.close_invoice(invoice_id, payment_var.get())
                messagebox.showinfo("Saved", "Payment saved. Invoice status changed to closed.")
                win.destroy()
                self.refresh_transactions()
            except Exception as e:
                messagebox.showerror("Error", str(e))

        def void_invoice():
            if status == "closed":
                messagebox.showerror("Error", "Closed invoices cannot be voided.")
                return
            if not messagebox.askyesno("Void Invoice", "Mark this invoice as void?\n\nThis does not restore inventory; it only marks the invoice record as void."):
                return
            self.db.void_invoice(invoice_id)
            messagebox.showinfo("Voided", "Invoice marked as void.")
            win.destroy()
            self.refresh_transactions()

        def open_printable():
            if printable_path and os.path.exists(printable_path):
                webbrowser.open(f"file://{os.path.abspath(printable_path)}")
            else:
                messagebox.showerror("Error", "Printable invoice file was not found.")

        ttk.Button(buttons, text="Save Payment / Close Invoice", style="Big.TButton", command=save_payment).pack(side="left")
        ttk.Button(buttons, text="Open Printable Invoice", style="Big.TButton", command=open_printable).pack(side="left", padx=8)
        ttk.Button(buttons, text="Void Invoice", style="Big.TButton", command=void_invoice).pack(side="left", padx=8)
        ttk.Button(buttons, text="Close Window", style="Big.TButton", command=win.destroy).pack(side="right")

    # ---------- Statements Tab ----------
    def _build_statements_tab(self):
        top = ttk.Frame(self.tab_statements)
        top.pack(fill="x", pady=(0, 10))

        self.statement_month = tk.StringVar(value=str(datetime.now().month))
        self.statement_year = tk.StringVar(value=str(datetime.now().year))
        self.year_statement_year = tk.StringVar(value=str(datetime.now().year))

        ttk.Label(top, text="Month:").pack(side="left")
        ttk.Combobox(top, textvariable=self.statement_month, state="readonly", width=10,
                     values=[str(i) for i in range(1, 13)]).pack(side="left", padx=6)
        ttk.Label(top, text="Year:").pack(side="left", padx=(8, 0))
        ttk.Entry(top, textvariable=self.statement_year, width=8).pack(side="left", padx=6)
        ttk.Button(top, text="Generate Monthly Statement", style="Big.TButton", command=self.on_generate_monthly_statement).pack(side="left", padx=8)

        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=12)
        ttk.Label(top, text="Year:").pack(side="left")
        ttk.Entry(top, textvariable=self.year_statement_year, width=8).pack(side="left", padx=6)
        ttk.Button(top, text="Generate Yearly Statement", style="Big.TButton", command=self.on_generate_yearly_statement).pack(side="left", padx=8)

        ttk.Button(top, text="Open Selected Monthly", style="Big.TButton", command=self.on_open_selected_monthly_statement).pack(side="right")
        ttk.Button(top, text="Export Statements CSV", style="Big.TButton",
                   command=lambda: self.export_treeview_csv(self.statements_tree, "statements")).pack(side="right", padx=8)

        table_frame = ttk.Frame(self.tab_statements)
        table_frame.pack(fill="both", expand=True)

        cols = ("period", "invoices", "open", "closed", "void", "sales", "paid", "balance", "updated")
        self.statements_tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=16)
        for c, title, w, anchor in [
            ("period", "Period", 150, "center"),
            ("invoices", "Invoices", 90, "center"),
            ("open", "Open", 80, "center"),
            ("closed", "Closed", 80, "center"),
            ("void", "Void", 80, "center"),
            ("sales", "Total Sales", 130, "e"),
            ("paid", "Paid", 130, "e"),
            ("balance", "Open Balance", 140, "e"),
            ("updated", "Last Updated", 170, "center"),
        ]:
            self.statements_tree.heading(c, text=title)
            self.statements_tree.column(c, width=w, anchor=anchor)

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.statements_tree.yview)
        self.statements_tree.configure(yscrollcommand=vsb.set)
        self.statements_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.statements_tree.bind("<Double-1>", lambda event: self.on_open_selected_monthly_statement())

        hint = ttk.Label(
            self.tab_statements,
            text="Monthly statements are auto-created for completed months when the app opens. Yearly statements are calculated from the saved monthly statements.",
            foreground="#666"
        )
        hint.pack(anchor="w", pady=(8, 0))

    def auto_save_completed_monthly_statements(self):
        first = self.db.first_invoice_month()
        if not first:
            return

        today = datetime.now()
        # Save completed months only. The current month is still changing.
        end_year = today.year
        end_month = today.month - 1
        if end_month == 0:
            end_month = 12
            end_year -= 1

        year, month = first
        while (year, month) <= (end_year, end_month):
            data = self.db.calculate_monthly_statement(year, month)
            self.db.save_monthly_statement(data)
            if month == 12:
                year += 1
                month = 1
            else:
                month += 1

    def refresh_statements(self):
        for row in self.statements_tree.get_children():
            self.statements_tree.delete(row)

        for row in self.db.list_monthly_statements():
            (
                _id, year, month, period_label, invoice_count, open_count, closed_count, void_count,
                _subtotal, _discount, _credit, total_sales, paid_total, open_total, _void_total,
                _printable_path, updated_at
            ) = row
            self.statements_tree.insert(
                "", "end", iid=f"{year}-{month:02d}",
                values=(period_label, invoice_count, open_count, closed_count, void_count,
                        money(total_sales), money(paid_total), money(open_total), updated_at)
            )

    def on_generate_monthly_statement(self):
        try:
            year = int(self.statement_year.get())
            month = int(self.statement_month.get())
            if month < 1 or month > 12:
                raise ValueError("Month must be 1 through 12.")
            data = self.db.calculate_monthly_statement(year, month)
            path = self.create_printable_monthly_statement(data)
            self.db.save_monthly_statement(data, path)
            self.refresh_statements()
            messagebox.showinfo("Statement Created", f"Monthly statement created:\n{path}")
            webbrowser.open(f"file://{os.path.abspath(path)}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def on_generate_yearly_statement(self):
        try:
            year = int(self.year_statement_year.get())
            # Re-save all monthly statements for this year up to completed/current existing invoice months
            # before building the yearly statement from monthly records.
            for month in range(1, 13):
                data = self.db.calculate_monthly_statement(year, month)
                self.db.save_monthly_statement(data)
            path = self.create_printable_yearly_statement(year)
            self.refresh_statements()
            messagebox.showinfo("Statement Created", f"Yearly statement created:\n{path}")
            webbrowser.open(f"file://{os.path.abspath(path)}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def on_open_selected_monthly_statement(self):
        selected = self.statements_tree.selection()
        if not selected:
            messagebox.showerror("Error", "Select a monthly statement first.")
            return
        year_text, month_text = selected[0].split("-")
        statement = self.db.get_monthly_statement(int(year_text), int(month_text))
        if not statement:
            messagebox.showerror("Error", "Statement was not found.")
            return
        printable_path = statement[15]
        if printable_path and os.path.exists(printable_path):
            webbrowser.open(f"file://{os.path.abspath(printable_path)}")
        else:
            data = self.db.calculate_monthly_statement(int(year_text), int(month_text))
            path = self.create_printable_monthly_statement(data)
            self.db.save_monthly_statement(data, path)
            self.refresh_statements()
            webbrowser.open(f"file://{os.path.abspath(path)}")

    def create_printable_monthly_statement(self, data):
        statement_dir = "printable_statements"
        os.makedirs(statement_dir, exist_ok=True)
        year = data["year"]
        month = data["month"]
        file_name = f"monthly_statement_{year}_{month:02d}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        path = os.path.join(statement_dir, file_name)

        invoice_rows = []
        for _id, created_at, ref, store, total, status, payment, paid_at in self.db.list_invoice_details_for_month(year, month):
            invoice_rows.append(
                "<tr>"
                f"<td>{escape(created_at)}</td>"
                f"<td>{escape(ref)}</td>"
                f"<td>{escape(store)}</td>"
                f"<td>{escape(status.title())}</td>"
                f"<td>{escape(payment)}</td>"
                f"<td>{escape(paid_at)}</td>"
                f"<td class='money'>{money(total)}</td>"
                "</tr>"
            )

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Monthly Statement - {escape(data['period_label'])}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; color: #222; }}
        h1 {{ margin-bottom: 4px; }}
        .muted {{ color: #666; }}
        .summary {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 24px 0; }}
        .card {{ border: 1px solid #ccc; padding: 12px; }}
        .label {{ color: #666; font-size: 13px; }}
        .value {{ font-size: 22px; font-weight: bold; margin-top: 4px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 18px; }}
        th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; }}
        th {{ background: #f0f0f0; }}
        .money {{ text-align: right; }}
        .print-button {{ margin-bottom: 20px; padding: 10px 16px; font-size: 16px; }}
        @media print {{ .print-button {{ display: none; }} body {{ margin: 24px; }} }}
    </style>
</head>
<body>
    <button class="print-button" onclick="window.print()">Print Statement</button>
    <h1>Monthly Statement</h1>
    <div class="muted">{escape(data['period_label'])} &bull; Created {escape(now_str())}</div>

    <div class="summary">
        <div class="card"><div class="label">Invoices</div><div class="value">{data['invoice_count']}</div></div>
        <div class="card"><div class="label">Total sales</div><div class="value">{money(data['total_sales'])}</div></div>
        <div class="card"><div class="label">Paid</div><div class="value">{money(data['paid_total'])}</div></div>
        <div class="card"><div class="label">Open balance</div><div class="value">{money(data['open_total'])}</div></div>
    </div>

    <table>
        <tr><th>Subtotal</th><td class="money">{money(data['subtotal'])}</td></tr>
        <tr><th>Discounts</th><td class="money">- {money(data['discount_amount'])}</td></tr>
        <tr><th>Credits</th><td class="money">- {money(data['credit'])}</td></tr>
        <tr><th>Total sales</th><td class="money"><strong>{money(data['total_sales'])}</strong></td></tr>
        <tr><th>Open / Closed / Void</th><td>{data['open_count']} open, {data['closed_count']} closed, {data['void_count']} void</td></tr>
    </table>

    <h2>Invoices</h2>
    <table>
        <thead>
            <tr><th>Date</th><th>Invoice #</th><th>Store</th><th>Status</th><th>Payment</th><th>Paid At</th><th class="money">Total</th></tr>
        </thead>
        <tbody>{''.join(invoice_rows) if invoice_rows else '<tr><td colspan="7">No invoices for this month.</td></tr>'}</tbody>
    </table>
</body>
</html>
"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        return os.path.abspath(path)

    def create_printable_yearly_statement(self, year):
        statement_dir = "printable_statements"
        os.makedirs(statement_dir, exist_ok=True)
        file_name = f"yearly_statement_{year}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        path = os.path.join(statement_dir, file_name)

        data = self.db.yearly_statement_from_months(year)
        month_rows = []
        for month, period_label, invoice_count, total_sales, paid_total, open_total, void_total in self.db.monthly_statements_for_year(year):
            month_rows.append(
                "<tr>"
                f"<td>{escape(period_label)}</td>"
                f"<td class='center'>{invoice_count}</td>"
                f"<td class='money'>{money(total_sales)}</td>"
                f"<td class='money'>{money(paid_total)}</td>"
                f"<td class='money'>{money(open_total)}</td>"
                f"<td class='money'>{money(void_total)}</td>"
                "</tr>"
            )

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Yearly Statement - {year}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; color: #222; }}
        h1 {{ margin-bottom: 4px; }}
        .muted {{ color: #666; }}
        .summary {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 24px 0; }}
        .card {{ border: 1px solid #ccc; padding: 12px; }}
        .label {{ color: #666; font-size: 13px; }}
        .value {{ font-size: 22px; font-weight: bold; margin-top: 4px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 18px; }}
        th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; }}
        th {{ background: #f0f0f0; }}
        .money {{ text-align: right; }}
        .center {{ text-align: center; }}
        .print-button {{ margin-bottom: 20px; padding: 10px 16px; font-size: 16px; }}
        @media print {{ .print-button {{ display: none; }} body {{ margin: 24px; }} }}
    </style>
</head>
<body>
    <button class="print-button" onclick="window.print()">Print Statement</button>
    <h1>Yearly Statement</h1>
    <div class="muted">{year} &bull; Built from saved monthly statements &bull; Created {escape(now_str())}</div>

    <div class="summary">
        <div class="card"><div class="label">Months saved</div><div class="value">{data['month_count']}</div></div>
        <div class="card"><div class="label">Invoices</div><div class="value">{data['invoice_count']}</div></div>
        <div class="card"><div class="label">Total sales</div><div class="value">{money(data['total_sales'])}</div></div>
        <div class="card"><div class="label">Open balance</div><div class="value">{money(data['open_total'])}</div></div>
    </div>

    <table>
        <tr><th>Subtotal</th><td class="money">{money(data['subtotal'])}</td></tr>
        <tr><th>Discounts</th><td class="money">- {money(data['discount_amount'])}</td></tr>
        <tr><th>Credits</th><td class="money">- {money(data['credit'])}</td></tr>
        <tr><th>Total sales</th><td class="money"><strong>{money(data['total_sales'])}</strong></td></tr>
        <tr><th>Paid</th><td class="money">{money(data['paid_total'])}</td></tr>
        <tr><th>Open balance</th><td class="money">{money(data['open_total'])}</td></tr>
        <tr><th>Open / Closed / Void invoices</th><td>{data['open_count']} open, {data['closed_count']} closed, {data['void_count']} void</td></tr>
    </table>

    <h2>Monthly Breakdown</h2>
    <table>
        <thead>
            <tr><th>Month</th><th class="center">Invoices</th><th class="money">Sales</th><th class="money">Paid</th><th class="money">Open</th><th class="money">Void</th></tr>
        </thead>
        <tbody>{''.join(month_rows) if month_rows else '<tr><td colspan="6">No monthly statements saved for this year.</td></tr>'}</tbody>
    </table>
</body>
</html>
"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        return os.path.abspath(path)

    # ---------- History Tab ----------
    def _build_history_tab(self):
        top = ttk.Frame(self.tab_history)
        top.pack(fill="x", pady=(0, 10))
        ttk.Button(top, text="Refresh", style="Big.TButton", command=self.refresh_history).pack(side="left")
        ttk.Button(top, text="Export History CSV", style="Big.TButton",
                   command=lambda: self.export_treeview_csv(self.hist, "history")).pack(side="left", padx=8)
        ttk.Label(top, text="Shows last 200 transactions (shipments/invoices).", foreground="#666").pack(side="left", padx=12)

        table_frame = ttk.Frame(self.tab_history)
        table_frame.pack(fill="both", expand=True)

        cols = ("time", "type", "ref", "item", "change", "note")
        self.hist = ttk.Treeview(table_frame, columns=cols, show="headings", height=14)
        for c, title, w in [
            ("time", "Time", 160),
            ("type", "Type", 100),
            ("ref", "Ref #", 140),
            ("item", "Item", 250),
            ("change", "Qty Change", 110),
            ("note", "Note", 260),
        ]:
            self.hist.heading(c, text=title)
            self.hist.column(c, width=w)

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.hist.yview)
        self.hist.configure(yscrollcommand=vsb.set)
        self.hist.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

    def refresh_history(self):
        for row in self.hist.get_children():
            self.hist.delete(row)

        rows = self.db.list_history(limit=200)
        for created_at, typ, ref, item_name, qty_change, note in rows:
            self.hist.insert("", "end", values=(created_at, typ, ref, item_name, qty_change, note))

    def export_treeview_csv(self, tree, default_name):
        """Export the currently displayed rows of a Treeview to a CSV file."""
        rows = tree.get_children()
        if not rows:
            messagebox.showinfo("Nothing to Export", "There are no displayed rows to export.")
            return

        filename = f"{default_name}_{datetime.now().strftime('%Y-%m-%d')}.csv"
        path = filedialog.asksaveasfilename(
            title=f"Export {default_name.title()}",
            defaultextension=".csv",
            initialfile=filename,
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return

        try:
            columns = list(tree["columns"])
            headings = [tree.heading(column, "text") or column for column in columns]

            # utf-8-sig adds a BOM so Excel opens Unicode text and status icons correctly.
            with open(path, "w", newline="", encoding="utf-8-sig") as csv_file:
                writer = csv.writer(csv_file)
                writer.writerow(headings)
                for item_id in rows:
                    writer.writerow(tree.item(item_id, "values"))

            messagebox.showinfo("Export Complete", f"The table was exported to:\n{path}")
        except (OSError, csv.Error) as exc:
            messagebox.showerror("Export Error", f"Could not export the table.\n\n{exc}")

    # ---------- Shared ----------
    def refresh_all(self):
        self.refresh_inventory()
        self.refresh_history()
        self.refresh_transactions()
        self.refresh_statements()
        self.refresh_customer_management()
        self.refresh_customer_dropdowns()
        self._update_hints()

    def _refresh_item_dropdowns(self):
        items = self.db.list_items("")
        names = [row[1] for row in items]
        self.ship_item_cb["values"] = names
        self.inv_item_cb["values"] = names

        # Keep selection if still valid
        if self.ship_item.get() not in names:
            self.ship_item.set(names[0] if names else "")
        if self.inv_item.get() not in names:
            self.inv_item.set(names[0] if names else "")

    def _selected_item_id(self, name):
        row = self.db.get_item_by_name(name)
        if not row:
            raise ValueError("Please select an item first (add it in Inventory tab).")
        return row[0]

    def _update_hints(self):
        items = self.db.list_items("")
        if not items:
            self.ship_hint.config(text="Add at least one item in the Inventory tab first.")
            self.inv_hint.config(text="Add at least one item in the Inventory tab first.")
        else:
            self.ship_hint.config(text="Enter a shipment number, add one or more item lines, then click Save Full Shipment.")
            self.inv_hint.config(text="Start typing a saved store name to autofill customer info, add item lines, then click Save Full Invoice.")


if __name__ == "__main__":
    app = App()
    app.mainloop()
