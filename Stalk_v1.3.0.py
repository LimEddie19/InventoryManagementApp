import os
import re
import csv
import shutil
import sqlite3
import webbrowser
import json
import urllib.request
import urllib.error
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime
from html import escape


APP_NAME = "Stalk"
APP_VERSION = "1.1.0"

# GitHub-hosted release metadata used by Help > Check for Updates.
UPDATE_INFO_URL = (
    "https://raw.githubusercontent.com/LimEddie19/InventoryManagementApp/main/version.json"
)

DB_FILE = "inventory.db"
SETTINGS_FILE = "stalk_settings.json"

DEFAULT_SETTINGS = {
    "check_updates_on_startup": True,
    "skipped_version": "",
}


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
            unit_price REAL NOT NULL DEFAULT 0,
            units_per_box INTEGER NOT NULL DEFAULT 1,
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
        CREATE TABLE IF NOT EXISTS shipments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ref_number TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS shipment_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shipment_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            entered_qty INTEGER NOT NULL,
            received_as TEXT NOT NULL CHECK(received_as IN ('box','unit')),
            unit_qty INTEGER NOT NULL,
            units_per_box INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(shipment_id) REFERENCES shipments(id),
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
            sale_type TEXT NOT NULL DEFAULT 'unit',
            unit_qty INTEGER NOT NULL DEFAULT 0,
            units_per_box INTEGER NOT NULL DEFAULT 1,
            base_price REAL NOT NULL DEFAULT 0,
            surcharge_amount REAL NOT NULL DEFAULT 0,
            surcharge_reason TEXT,
            unit_price REAL NOT NULL DEFAULT 0,
            line_total REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(invoice_id) REFERENCES invoices(id),
            FOREIGN KEY(item_id) REFERENCES items(id)
        );
        """)

        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS invoice_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            source_invoice_id INTEGER,
            source_invoice_line_id INTEGER,
            source_ref_number TEXT,
            item_name TEXT,
            amount REAL NOT NULL CHECK(amount > 0),
            reason TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(invoice_id) REFERENCES invoices(id),
            FOREIGN KEY(source_invoice_id) REFERENCES invoices(id),
            FOREIGN KEY(source_invoice_line_id) REFERENCES invoice_lines(id)
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
        self.ensure_internal_use_customer()
        self._ensure_invoice_line_columns()

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
        CREATE TABLE IF NOT EXISTS customer_item_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            special_price REAL NOT NULL CHECK(special_price >= 0),
            special_unit_price REAL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(customer_id) REFERENCES customers(id),
            FOREIGN KEY(item_id) REFERENCES items(id),
            UNIQUE(customer_id, item_id)
        );
        """)

        self._ensure_special_price_columns()

        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS item_surcharges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            box_surcharge REAL NOT NULL DEFAULT 0 CHECK(box_surcharge >= 0),
            unit_surcharge REAL NOT NULL DEFAULT 0 CHECK(unit_surcharge >= 0),
            start_date TEXT NOT NULL,
            end_date TEXT,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(item_id) REFERENCES items(id)
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
            surcharge_amount REAL NOT NULL DEFAULT 0,
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
        self._ensure_monthly_statement_columns()
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
        if "unit_price" not in columns:
            self.conn.execute("ALTER TABLE items ADD COLUMN unit_price REAL NOT NULL DEFAULT 0;")
        if "units_per_box" not in columns:
            self.conn.execute("ALTER TABLE items ADD COLUMN units_per_box INTEGER NOT NULL DEFAULT 1;")

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

    def _ensure_invoice_line_columns(self):
        cur = self.conn.cursor()
        cur.execute("PRAGMA table_info(invoice_lines);")
        columns = {row[1] for row in cur.fetchall()}
        if "sale_type" not in columns:
            self.conn.execute("ALTER TABLE invoice_lines ADD COLUMN sale_type TEXT NOT NULL DEFAULT 'unit';")
        if "unit_qty" not in columns:
            self.conn.execute("ALTER TABLE invoice_lines ADD COLUMN unit_qty INTEGER NOT NULL DEFAULT 0;")
            self.conn.execute("UPDATE invoice_lines SET unit_qty = qty WHERE unit_qty = 0;")
        if "units_per_box" not in columns:
            self.conn.execute("ALTER TABLE invoice_lines ADD COLUMN units_per_box INTEGER NOT NULL DEFAULT 1;")
        if "base_price" not in columns:
            self.conn.execute("ALTER TABLE invoice_lines ADD COLUMN base_price REAL NOT NULL DEFAULT 0;")
            self.conn.execute("UPDATE invoice_lines SET base_price = unit_price WHERE base_price = 0;")
        if "surcharge_amount" not in columns:
            self.conn.execute("ALTER TABLE invoice_lines ADD COLUMN surcharge_amount REAL NOT NULL DEFAULT 0;")
        if "surcharge_reason" not in columns:
            self.conn.execute("ALTER TABLE invoice_lines ADD COLUMN surcharge_reason TEXT;")

    def _ensure_special_price_columns(self):
        cur = self.conn.cursor()
        cur.execute("PRAGMA table_info(customer_item_prices);")
        columns = {row[1] for row in cur.fetchall()}
        if "special_unit_price" not in columns:
            self.conn.execute("ALTER TABLE customer_item_prices ADD COLUMN special_unit_price REAL;")

    def _ensure_monthly_statement_columns(self):
        cur = self.conn.cursor()
        cur.execute("PRAGMA table_info(monthly_statements);")
        columns = {row[1] for row in cur.fetchall()}
        if "surcharge_amount" not in columns:
            self.conn.execute("ALTER TABLE monthly_statements ADD COLUMN surcharge_amount REAL NOT NULL DEFAULT 0;")

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
            where = "WHERE (i.name LIKE ? OR COALESCE(i.sku,'') LIKE ?)"
            like = f"%{search_text.strip()}%"
            params.extend((like, like))
        cur.execute(f"""
            SELECT i.id, i.name, COALESCE(i.sku,''), COALESCE(i.price,0),
                   COALESCE(i.unit_price,0), COALESCE(i.units_per_box,1),
                   COALESCE(i.weight,''), i.qty,
                   COALESCE(SUM(CASE WHEN t.type='INVOICE' AND t.created_at >= datetime('now','-30 days') THEN -t.qty_change ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN t.type='INVOICE' AND t.created_at >= datetime('now','-60 days') AND t.created_at < datetime('now','-30 days') THEN -t.qty_change ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN t.type='INVOICE' AND t.created_at >= datetime('now','-90 days') AND t.created_at < datetime('now','-60 days') THEN -t.qty_change ELSE 0 END),0),
                   i.updated_at
            FROM items i LEFT JOIN txns t ON t.item_id=i.id
            {where}
            GROUP BY i.id ORDER BY i.name COLLATE NOCASE;
        """, params)
        return cur.fetchall()

    def add_item(self, name, sku, box_price, unit_price, units_per_box, weight):
        name = name.strip()
        if not name: raise ValueError("Item name is required.")
        try:
            box_price_value = float(box_price or 0)
            unit_price_value = float(unit_price or 0)
            units_value = int(units_per_box or 1)
        except ValueError:
            raise ValueError("Prices and units per box must be valid numbers.")
        if box_price_value < 0 or unit_price_value < 0: raise ValueError("Prices must be >= 0.")
        if units_value < 1: raise ValueError("Units per box must be at least 1.")
        self.conn.execute("""
            INSERT INTO items (name,sku,price,unit_price,units_per_box,weight,qty,reorder_level,restock_total,updated_at)
            VALUES (?,?,?,?,?,?,0,0,0,?);
        """, (name,sku.strip(),box_price_value,unit_price_value,units_value,weight.strip(),now_str()))
        self.conn.commit()

    def update_item(self, item_id, name, sku, box_price, unit_price, units_per_box, weight, convert_existing=False):
        name=name.strip()
        if not name: raise ValueError("Item name is required.")
        try:
            box_value=float(box_price or 0); unit_value=float(unit_price or 0); upb=int(units_per_box or 1)
        except ValueError: raise ValueError("Prices and units per box must be valid numbers.")
        if box_value < 0 or unit_value < 0: raise ValueError("Prices must be >= 0.")
        if upb < 1: raise ValueError("Units per box must be at least 1.")
        cur=self.conn.cursor(); cur.execute("SELECT qty, units_per_box FROM items WHERE id=?",(item_id,)); row=cur.fetchone()
        if not row: raise ValueError("Item not found.")
        cur.execute("SELECT id FROM items WHERE name=? COLLATE NOCASE AND id<>?",(name,item_id))
        if cur.fetchone(): raise ValueError("Another item already uses that name.")
        qty, old_upb=row
        if convert_existing and old_upb == 1 and upb > 1 and qty > 0:
            qty *= upb
        self.conn.execute("""UPDATE items SET name=?,sku=?,price=?,unit_price=?,units_per_box=?,weight=?,qty=?,updated_at=? WHERE id=?""",
                          (name,sku.strip(),box_value,unit_value,upb,weight.strip(),qty,now_str(),item_id))
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
        cur=self.conn.cursor(); cur.execute("""
            SELECT id,name,COALESCE(price,0),COALESCE(unit_price,0),COALESCE(units_per_box,1),COALESCE(weight,''),qty
            FROM items WHERE id=?;
        """,(item_id,)); return cur.fetchone()

    def ensure_internal_use_customer(self):
        name = "INTERNAL USE"
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id FROM customers WHERE store_name = ? COLLATE NOCASE;",
            (name,)
        )
        row = cur.fetchone()
        if row:
            return row[0]

        cur.execute("""
            INSERT INTO customers (store_name, address, phone, credit_balance, updated_at)
            VALUES (?, ?, ?, 0, ?);
        """, (name, "Warehouse / Company Internal Use", "", now_str()))
        self.conn.commit()
        return cur.lastrowid

    def is_internal_use_customer(self, customer_id):
        if customer_id is None:
            return False
        cur = self.conn.cursor()
        cur.execute("SELECT store_name FROM customers WHERE id = ?;", (customer_id,))
        row = cur.fetchone()
        return bool(row and str(row[0]).strip().upper() == "INTERNAL USE")

    def mark_invoice_internal_use(self, invoice_id):
        cur = self.conn.cursor()
        cur.execute("""
            UPDATE invoices
            SET status = 'closed',
                payment_method = 'internal use',
                paid_at = ?
            WHERE id = ? AND status = 'open';
        """, (now_str(), invoice_id))
        if cur.rowcount != 1:
            raise ValueError("Internal-use invoice could not be completed.")
        self.conn.commit()

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
        if str(row[0]).strip().upper() == "INTERNAL USE":
            raise ValueError("INTERNAL USE is a built-in warehouse account and cannot be deleted.")
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

    def get_customer_special_price(self, customer_id, item_id, sale_type="box"):
        cur=self.conn.cursor(); cur.execute("SELECT special_price,special_unit_price FROM customer_item_prices WHERE customer_id=? AND item_id=?",(customer_id,item_id)); row=cur.fetchone()
        if not row: return None
        value = row[0] if sale_type == "box" else row[1]
        return float(value) if value is not None else None

    def get_effective_item_price(self, customer_id, item_id, sale_type="box"):
        cur=self.conn.cursor()
        if customer_id is not None:
            cur.execute("SELECT special_price,special_unit_price FROM customer_item_prices WHERE customer_id=? AND item_id=?",(customer_id,item_id)); row=cur.fetchone()
            if row:
                value = row[0] if sale_type == "box" else row[1]
                if value is not None: return float(value), "Special"
        cur.execute("SELECT price,unit_price FROM items WHERE id=?",(item_id,)); row=cur.fetchone()
        if not row: raise ValueError("Item not found.")
        return float(row[0] if sale_type == "box" else row[1]), "Standard"

    def set_customer_special_price(self, customer_id, item_id, sale_type, special_price):
        if sale_type not in {"box","unit"}: raise ValueError("Price type must be box or unit.")
        try: value=float(special_price)
        except (TypeError,ValueError): raise ValueError("Special price must be a valid number.")
        if value < 0: raise ValueError("Special price must be >= 0.")
        cur=self.conn.cursor(); cur.execute("SELECT id FROM customers WHERE id=?",(customer_id,))
        if not cur.fetchone(): raise ValueError("Customer not found.")
        cur.execute("SELECT id FROM items WHERE id=?",(item_id,))
        if not cur.fetchone(): raise ValueError("Item not found.")
        existing=self.get_customer_special_price(customer_id,item_id,"box")
        self.conn.execute("""INSERT INTO customer_item_prices(customer_id,item_id,special_price,special_unit_price,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(customer_id,item_id) DO UPDATE SET
            special_price=CASE WHEN ?='box' THEN excluded.special_price ELSE customer_item_prices.special_price END,
            special_unit_price=CASE WHEN ?='unit' THEN excluded.special_unit_price ELSE customer_item_prices.special_unit_price END,
            updated_at=excluded.updated_at""",
            (customer_id,item_id,value if sale_type=='box' else (existing if existing is not None else 0),value if sale_type=='unit' else None,now_str(),sale_type,sale_type))
        self.conn.commit()

    def remove_customer_special_price(self, customer_id, item_id, sale_type):
        column = "special_price" if sale_type == "box" else "special_unit_price"
        if sale_type == "box":
            self.conn.execute("UPDATE customer_item_prices SET special_price=0, updated_at=? WHERE customer_id=? AND item_id=?",(now_str(),customer_id,item_id))
        else:
            self.conn.execute("UPDATE customer_item_prices SET special_unit_price=NULL, updated_at=? WHERE customer_id=? AND item_id=?",(now_str(),customer_id,item_id))
        self.conn.execute("DELETE FROM customer_item_prices WHERE customer_id=? AND item_id=? AND special_price=0 AND special_unit_price IS NULL",(customer_id,item_id))
        self.conn.commit()

    def list_customer_special_prices(self, customer_id):
        cur=self.conn.cursor(); cur.execute("""
            SELECT i.id,i.name,COALESCE(i.price,0),COALESCE(i.unit_price,0),cip.special_price,cip.special_unit_price,cip.updated_at
            FROM customer_item_prices cip JOIN items i ON i.id=cip.item_id WHERE cip.customer_id=? ORDER BY i.name COLLATE NOCASE
        """,(customer_id,)); return cur.fetchall()

    def save_item_surcharge(self, item_id, reason, box_surcharge, unit_surcharge, start_date, end_date="", note=""):
        reason = reason.strip()
        if not reason:
            raise ValueError("Surcharge reason is required.")
        try:
            box_value = float(box_surcharge or 0)
            unit_value = float(unit_surcharge or 0)
        except ValueError:
            raise ValueError("Surcharge amounts must be valid numbers.")
        if box_value < 0 or unit_value < 0:
            raise ValueError("Surcharges cannot be negative.")
        if box_value == 0 and unit_value == 0:
            raise ValueError("Enter a box or unit surcharge greater than zero.")
        try:
            start = datetime.strptime(start_date.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            raise ValueError("Start date must use YYYY-MM-DD.")
        end = end_date.strip()
        if end:
            try:
                end = datetime.strptime(end, "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                raise ValueError("End date must use YYYY-MM-DD.")
            if end < start:
                raise ValueError("End date cannot be earlier than the start date.")
        self.conn.execute("""
            INSERT INTO item_surcharges
                (item_id, reason, box_surcharge, unit_surcharge, start_date, end_date, active, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?);
        """, (item_id, reason, box_value, unit_value, start, end or None, note.strip(), now_str(), now_str()))
        self.conn.commit()

    def list_item_surcharges(self, item_id):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, reason, box_surcharge, unit_surcharge, start_date, COALESCE(end_date,''),
                   active, COALESCE(note,''), updated_at
            FROM item_surcharges WHERE item_id = ? ORDER BY start_date DESC, id DESC;
        """, (item_id,))
        return cur.fetchall()

    def set_item_surcharge_active(self, surcharge_id, active):
        self.conn.execute(
            "UPDATE item_surcharges SET active = ?, updated_at = ? WHERE id = ?;",
            (1 if active else 0, now_str(), surcharge_id),
        )
        self.conn.commit()

    def get_active_item_surcharge(self, item_id, sale_type, on_date=None):
        sale_type = sale_type.lower().strip()
        if sale_type not in {"box", "unit"}:
            raise ValueError("Sale type must be box or unit.")
        date_text = on_date or datetime.now().strftime("%Y-%m-%d")
        column = "box_surcharge" if sale_type == "box" else "unit_surcharge"
        cur = self.conn.cursor()
        cur.execute(f"""
            SELECT {column}, reason
            FROM item_surcharges
            WHERE item_id = ? AND active = 1
              AND start_date <= ?
              AND (end_date IS NULL OR end_date = '' OR end_date >= ?)
              AND {column} > 0
            ORDER BY start_date DESC, id DESC
            LIMIT 1;
        """, (item_id, date_text, date_text))
        row = cur.fetchone()
        return (float(row[0]), row[1]) if row else (0.0, "")

    def get_invoice_price_details(self, customer_id, item_id, sale_type="box"):
        if self.is_internal_use_customer(customer_id):
            return 0.0, 0.0, 0.0, "Internal Use", ""

        base_price, pricing_type = self.get_effective_item_price(
            customer_id, item_id, sale_type
        )
        surcharge, reason = self.get_active_item_surcharge(item_id, sale_type)
        return base_price, surcharge, base_price + surcharge, pricing_type, reason


    def list_refundable_invoices(self):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, ref_number, COALESCE(store_name,''), created_at, total, status
            FROM invoices WHERE status != 'void' ORDER BY id DESC;
        """)
        return cur.fetchall()

    def get_refundable_invoice_lines(self, invoice_id):
        cur=self.conn.cursor(); cur.execute("""
            SELECT il.id,il.item_id,il.item_name,il.qty,COALESCE(SUM(rl.qty),0),il.unit_price,il.sale_type,il.units_per_box
            FROM invoice_lines il LEFT JOIN refund_lines rl ON rl.invoice_line_id=il.id
            WHERE il.invoice_id=? GROUP BY il.id HAVING il.qty-COALESCE(SUM(rl.qty),0)>0 ORDER BY il.id
        """,(invoice_id,)); return cur.fetchall()

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
                SELECT il.item_id, il.item_name, il.qty, il.unit_price, il.sale_type, il.units_per_box,
                       COALESCE((SELECT SUM(rl.qty) FROM refund_lines rl WHERE rl.invoice_line_id = il.id),0)
                FROM invoice_lines il WHERE il.id = ? AND il.invoice_id = ?;
            """, (invoice_line_id, invoice_id))
            row = cur.fetchone()
            if not row:
                raise ValueError("A selected invoice line was not found.")
            item_id, item_name, sold_qty, unit_price, sale_type, units_per_box, already_refunded = row
            available = sold_qty - already_refunded
            if return_qty > available:
                raise ValueError(f"Only {available} unit(s) of {item_name} remain refundable.")
            line_total = float(unit_price) * return_qty
            total += line_total
            validated.append((invoice_line_id, item_id, item_name, return_qty, float(unit_price), line_total, sale_type, units_per_box))
        cur.execute("""
            INSERT INTO refunds (invoice_id, customer_id, refund_method, total, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?);
        """, (invoice_id, customer_id, refund_method, total, note.strip(), now_str()))
        refund_id = cur.lastrowid
        for invoice_line_id, item_id, item_name, qty, unit_price, line_total, sale_type, units_per_box in validated:
            cur.execute("""
                INSERT INTO refund_lines (refund_id, invoice_line_id, item_id, item_name, qty, unit_price, line_total)
                VALUES (?, ?, ?, ?, ?, ?, ?);
            """, (refund_id, invoice_line_id, item_id, item_name, qty, unit_price, line_total))
            cur.execute("UPDATE items SET qty = qty + ?, updated_at = ? WHERE id = ?;", (qty * (units_per_box if sale_type == "box" else 1), now_str(), item_id))
            cur.execute("""
                INSERT INTO txns (type, ref_number, item_id, qty_change, note, created_at)
                VALUES ('SHIPMENT', ?, ?, ?, ?, ?);
            """, (f"REFUND-{ref_number}", item_id, qty * (units_per_box if sale_type == "box" else 1), f"Customer return: {note.strip()}", now_str()))
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

    def save_invoice_line(self, invoice_id, item_id, item_name, qty, sale_type, unit_qty, units_per_box, base_price, surcharge_amount, charged_price, surcharge_reason=""):
        self.conn.execute("""INSERT INTO invoice_lines(
                invoice_id,item_id,item_name,qty,sale_type,unit_qty,units_per_box,
                base_price,surcharge_amount,surcharge_reason,unit_price,line_total
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (invoice_id,item_id,item_name,int(qty),sale_type,int(unit_qty),int(units_per_box),
             float(base_price),float(surcharge_amount),surcharge_reason.strip(),float(charged_price),float(charged_price)*int(qty)))
        self.conn.commit()

    def list_closed_invoice_lines_for_customer(self, customer_id):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT inv.id, inv.ref_number, inv.created_at,
                   il.id, il.item_name, il.qty, il.sale_type,
                   il.unit_price, il.line_total
            FROM invoices inv
            JOIN invoice_lines il ON il.invoice_id = inv.id
            WHERE inv.customer_id = ?
              AND inv.status = 'closed'
              AND LOWER(COALESCE(inv.payment_method,'')) <> 'internal use'
            ORDER BY inv.id DESC, il.id;
        """, (customer_id,))
        return cur.fetchall()

    def save_invoice_adjustment(
        self, invoice_id, source_invoice_id, source_invoice_line_id,
        source_ref_number, item_name, amount, reason=""
    ):
        amount = float(amount)
        if amount <= 0:
            raise ValueError("Additional charge must be greater than zero.")
        self.conn.execute("""
            INSERT INTO invoice_adjustments(
                invoice_id, source_invoice_id, source_invoice_line_id,
                source_ref_number, item_name, amount, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            invoice_id, source_invoice_id, source_invoice_line_id,
            str(source_ref_number or "").strip(),
            str(item_name or "").strip(),
            amount, str(reason or "").strip(), now_str()
        ))
        self.conn.commit()

    def get_invoice_adjustments(self, invoice_id):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, source_invoice_id, source_invoice_line_id,
                   COALESCE(source_ref_number,''), COALESCE(item_name,''),
                   amount, COALESCE(reason,''), created_at
            FROM invoice_adjustments
            WHERE invoice_id = ?
            ORDER BY id;
        """, (invoice_id,))
        return cur.fetchall()

    def get_invoice_adjustment_total(self, invoice_id):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT COALESCE(SUM(amount),0)
            FROM invoice_adjustments
            WHERE invoice_id = ?;
        """, (invoice_id,))
        return float(cur.fetchone()[0] or 0)

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
        cur=self.conn.cursor(); cur.execute("""SELECT item_name,qty,sale_type,base_price,surcharge_amount,COALESCE(surcharge_reason,''),unit_price,line_total FROM invoice_lines WHERE invoice_id=? ORDER BY id""",(invoice_id,)); return cur.fetchall()

    def get_invoice_lines_for_edit(self, invoice_id):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, item_id, item_name, qty, sale_type, unit_qty, units_per_box,
                   base_price, surcharge_amount, COALESCE(surcharge_reason,''),
                   unit_price, line_total
            FROM invoice_lines
            WHERE invoice_id = ?
            ORDER BY id;
        """, (invoice_id,))
        return cur.fetchall()

    def get_invoice_customer_id(self, invoice_id):
        cur = self.conn.cursor()
        cur.execute("SELECT customer_id FROM invoices WHERE id = ?;", (invoice_id,))
        row = cur.fetchone()
        return row[0] if row else None

    def update_open_invoice_lines(self, invoice_id, lines):
        if not lines:
            raise ValueError("An invoice must contain at least one item.")

        cur = self.conn.cursor()
        cur.execute("""
            SELECT ref_number, status, discount_percent, credit, customer_id
            FROM invoices WHERE id = ?;
        """, (invoice_id,))
        invoice = cur.fetchone()
        if not invoice:
            raise ValueError("Invoice not found.")

        ref_number, status, discount_percent, old_credit, customer_id = invoice
        if status != "open":
            raise ValueError("Only open invoices can be edited.")

        cur.execute("SELECT item_id, unit_qty FROM invoice_lines WHERE invoice_id = ?;", (invoice_id,))
        old_units = {}
        for item_id, unit_qty in cur.fetchall():
            old_units[item_id] = old_units.get(item_id, 0) + int(unit_qty)

        new_units = {}
        subtotal = 0.0
        validated = []

        for line in lines:
            item_id, item_name, qty, sale_type, unit_qty, units_per_box, base_price, surcharge_amount, charged_price, pricing_type, surcharge_reason = line
            qty = int(qty)
            unit_qty = int(unit_qty)
            units_per_box = int(units_per_box)
            if qty <= 0 or unit_qty <= 0:
                raise ValueError(f"Quantity for {item_name} must be greater than zero.")
            if sale_type not in {"box", "unit"}:
                raise ValueError("Sale type must be box or unit.")

            cur.execute("SELECT name, qty FROM items WHERE id = ?;", (item_id,))
            item = cur.fetchone()
            if not item:
                raise ValueError(f"Item not found: {item_name}")

            new_units[item_id] = new_units.get(item_id, 0) + unit_qty
            line_total = float(charged_price) * qty
            subtotal += line_total
            validated.append((
                int(item_id), str(item_name), qty, sale_type, unit_qty, units_per_box,
                float(base_price), float(surcharge_amount), str(surcharge_reason or ""),
                float(charged_price), line_total
            ))

        all_item_ids = set(old_units) | set(new_units)
        for item_id in all_item_ids:
            cur.execute("SELECT name, qty FROM items WHERE id = ?;", (item_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError("An item on this invoice no longer exists.")
            item_name, current_qty = row
            available = int(current_qty) + old_units.get(item_id, 0)
            requested = new_units.get(item_id, 0)
            if requested > available:
                raise ValueError(
                    f"Not enough stock for {item_name}. Available for this edited invoice: "
                    f"{available}; requested: {requested}."
                )

        subtotal += self.get_invoice_adjustment_total(invoice_id)

        discount_percent = float(discount_percent or 0)
        discount_amount = subtotal * (discount_percent / 100.0)
        before_credit = max(0.0, subtotal - discount_amount)
        old_credit = float(old_credit or 0)
        new_credit = min(old_credit, before_credit)
        returned_credit = max(0.0, old_credit - new_credit)
        total = max(0.0, before_credit - new_credit)

        try:
            self.conn.execute("BEGIN")

            for item_id in all_item_ids:
                stock_delta = old_units.get(item_id, 0) - new_units.get(item_id, 0)
                if stock_delta:
                    cur.execute(
                        "UPDATE items SET qty = qty + ?, updated_at = ? WHERE id = ?;",
                        (stock_delta, now_str(), item_id)
                    )
                    cur.execute("""
                        INSERT INTO txns(type, ref_number, item_id, qty_change, note, created_at)
                        VALUES ('INVOICE', ?, ?, ?, ?, ?);
                    """, (
                        ref_number, item_id, stock_delta,
                        "Open invoice edit adjustment", now_str()
                    ))

            cur.execute("DELETE FROM invoice_lines WHERE invoice_id = ?;", (invoice_id,))
            for item_id, item_name, qty, sale_type, unit_qty, units_per_box, base_price, surcharge_amount, surcharge_reason, charged_price, line_total in validated:
                cur.execute("""
                    INSERT INTO invoice_lines(
                        invoice_id, item_id, item_name, qty, sale_type, unit_qty,
                        units_per_box, base_price, surcharge_amount, surcharge_reason,
                        unit_price, line_total
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    invoice_id, item_id, item_name, qty, sale_type, unit_qty,
                    units_per_box, base_price, surcharge_amount, surcharge_reason,
                    charged_price, line_total
                ))

            cur.execute("""
                UPDATE invoices
                SET subtotal = ?, discount_amount = ?, credit = ?, total = ?
                WHERE id = ? AND status = 'open';
            """, (subtotal, discount_amount, new_credit, total, invoice_id))

            if returned_credit > 0 and customer_id is not None:
                cur.execute(
                    "UPDATE customers SET credit_balance = credit_balance + ?, updated_at = ? WHERE id = ?;",
                    (returned_credit, now_str(), customer_id)
                )
                cur.execute("""
                    INSERT INTO customer_credit_history
                        (customer_id, amount, entry_type, reference, note, created_at)
                    VALUES (?, ?, 'ADJUSTMENT', ?, ?, ?);
                """, (
                    customer_id, returned_credit, f"Invoice {ref_number}",
                    "Credit returned after open invoice edit", now_str()
                ))

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return subtotal, discount_amount, new_credit, total

    def list_open_invoices_for_customer(self, customer_id):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, ref_number, created_at, COALESCE(total,0),
                   COALESCE(store_name,'')
            FROM invoices
            WHERE customer_id = ?
              AND status = 'open'
            ORDER BY created_at, id;
        """, (customer_id,))
        return cur.fetchall()

    def close_multiple_invoices(self, invoice_ids, payment_method):
        invoice_ids = [int(x) for x in invoice_ids]
        if not invoice_ids:
            raise ValueError("Select at least one open invoice.")

        placeholders = ",".join("?" for _ in invoice_ids)
        cur = self.conn.cursor()
        cur.execute(f"""
            SELECT id, ref_number, status, COALESCE(total,0), customer_id
            FROM invoices
            WHERE id IN ({placeholders});
        """, invoice_ids)
        rows = cur.fetchall()

        if len(rows) != len(invoice_ids):
            raise ValueError("One or more selected invoices could not be found.")

        customer_ids = {row[4] for row in rows}
        if len(customer_ids) != 1:
            raise ValueError("Combined payment can only include one customer.")

        for invoice_id, ref, status, total, customer_id in rows:
            if status != "open":
                raise ValueError(f"Invoice {ref} is no longer open.")

        paid_at = now_str()
        try:
            self.conn.execute("BEGIN")
            for invoice_id in invoice_ids:
                cur.execute("""
                    UPDATE invoices
                    SET status='closed', payment_method=?, paid_at=?
                    WHERE id=? AND status='open';
                """, (payment_method, paid_at, invoice_id))
                if cur.rowcount != 1:
                    raise ValueError("An invoice changed status before payment completed.")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return {
            "invoice_count": len(rows),
            "total": sum(float(row[3] or 0) for row in rows),
            "refs": [str(row[1]) for row in rows],
        }

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
        cur = self.conn.cursor()

        cur.execute("""
            SELECT ref_number, status, customer_id, COALESCE(credit, 0)
            FROM invoices
            WHERE id = ?;
        """, (invoice_id,))
        invoice = cur.fetchone()

        if not invoice:
            raise ValueError("Invoice not found.")

        ref_number, status, customer_id, applied_credit = invoice

        if status == "closed":
            raise ValueError("Closed invoices cannot be voided.")
        if status == "void":
            raise ValueError("This invoice has already been voided.")
        if status != "open":
            raise ValueError("Only open invoices can be voided.")

        cur.execute("""
            SELECT item_id, COALESCE(unit_qty, 0)
            FROM invoice_lines
            WHERE invoice_id = ?;
        """, (invoice_id,))
        lines = cur.fetchall()

        try:
            self.conn.execute("BEGIN")

            for item_id, unit_qty in lines:
                unit_qty = int(unit_qty or 0)
                if unit_qty <= 0:
                    continue

                cur.execute("""
                    UPDATE items
                    SET qty = qty + ?, updated_at = ?
                    WHERE id = ?;
                """, (unit_qty, now_str(), item_id))

                if cur.rowcount != 1:
                    raise ValueError(
                        "An item on this invoice no longer exists. "
                        "The invoice was not voided."
                    )

                cur.execute("""
                    INSERT INTO txns
                        (type, ref_number, item_id, qty_change, note, created_at)
                    VALUES ('INVOICE', ?, ?, ?, ?, ?);
                """, (
                    ref_number,
                    item_id,
                    unit_qty,
                    "Stock restored from voided open invoice",
                    now_str(),
                ))

            applied_credit = float(applied_credit or 0)
            if applied_credit > 0:
                if customer_id is None:
                    raise ValueError(
                        "This invoice has applied customer credit but is not linked "
                        "to a customer. The invoice was not voided."
                    )

                cur.execute("""
                    UPDATE customers
                    SET credit_balance = credit_balance + ?, updated_at = ?
                    WHERE id = ?;
                """, (applied_credit, now_str(), customer_id))

                if cur.rowcount != 1:
                    raise ValueError(
                        "The customer linked to this invoice no longer exists. "
                        "The invoice was not voided."
                    )

                cur.execute("""
                    INSERT INTO customer_credit_history
                        (customer_id, amount, entry_type, reference, note, created_at)
                    VALUES (?, ?, 'ADJUSTMENT', ?, ?, ?);
                """, (
                    customer_id,
                    applied_credit,
                    f"Invoice {ref_number}",
                    "Account credit restored because open invoice was voided",
                    now_str(),
                ))

            cur.execute("""
                UPDATE invoices
                SET status = 'void',
                    payment_method = NULL,
                    paid_at = NULL
                WHERE id = ? AND status = 'open';
            """, (invoice_id,))

            if cur.rowcount != 1:
                raise ValueError(
                    "Invoice status changed before the void could be completed."
                )

            self.conn.commit()

        except Exception:
            self.conn.rollback()
            raise

        return {
            "ref_number": ref_number,
            "restored_units": sum(int(row[1] or 0) for row in lines),
            "restored_credit": applied_credit,
        }


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
                COALESCE(SUM(CASE WHEN status = 'void' THEN total ELSE 0 END), 0),
                COALESCE((SELECT SUM(il.surcharge_amount * il.qty)
                          FROM invoice_lines il JOIN invoices inv2 ON inv2.id = il.invoice_id
                          WHERE inv2.created_at >= ? AND inv2.created_at < ? AND inv2.status != 'void'), 0)
            FROM invoices
            WHERE created_at >= ? AND created_at < ?;
        """, (start, end, start, end))
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
            "surcharge_amount": row[11] or 0,
        }

    def save_monthly_statement(self, data, printable_path=""):
        self.conn.execute("""
            INSERT INTO monthly_statements (
                year, month, period_label, invoice_count, open_count, closed_count, void_count,
                subtotal, discount_amount, credit, surcharge_amount, total_sales, paid_total, open_total, void_total,
                printable_path, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(year, month) DO UPDATE SET
                period_label = excluded.period_label,
                invoice_count = excluded.invoice_count,
                open_count = excluded.open_count,
                closed_count = excluded.closed_count,
                void_count = excluded.void_count,
                subtotal = excluded.subtotal,
                discount_amount = excluded.discount_amount,
                credit = excluded.credit,
                surcharge_amount = excluded.surcharge_amount,
                total_sales = excluded.total_sales,
                paid_total = excluded.paid_total,
                open_total = excluded.open_total,
                void_total = excluded.void_total,
                printable_path = CASE WHEN excluded.printable_path != '' THEN excluded.printable_path ELSE monthly_statements.printable_path END,
                updated_at = excluded.updated_at;
        """, (
            data["year"], data["month"], data["period_label"], data["invoice_count"],
            data["open_count"], data["closed_count"], data["void_count"],
            data["subtotal"], data["discount_amount"], data["credit"], data["surcharge_amount"], data["total_sales"],
            data["paid_total"], data["open_total"], data["void_total"],
            printable_path, now_str(), now_str()
        ))
        self.conn.commit()

    def get_monthly_statement(self, year, month):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, year, month, period_label, invoice_count, open_count, closed_count, void_count,
                   subtotal, discount_amount, credit, surcharge_amount, total_sales, paid_total, open_total, void_total,
                   COALESCE(printable_path,''), created_at, updated_at
            FROM monthly_statements
            WHERE year = ? AND month = ?;
        """, (int(year), int(month)))
        return cur.fetchone()

    def list_monthly_statements(self):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, year, month, period_label, invoice_count, open_count, closed_count, void_count,
                   subtotal, discount_amount, credit, surcharge_amount, total_sales, paid_total, open_total, void_total,
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
                COALESCE(SUM(surcharge_amount), 0),
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
            "surcharge_amount": row[8] or 0,
            "total_sales": row[9] or 0,
            "paid_total": row[10] or 0,
            "open_total": row[11] or 0,
            "void_total": row[12] or 0,
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

    def record_full_shipment(self, ref_number, lines, note=""):
        ref_number = ref_number.strip()
        if not ref_number:
            raise ValueError("Shipment number is required.")
        if not lines:
            raise ValueError("Add at least one item to the shipment list.")

        cur = self.conn.cursor()
        created = now_str()

        try:
            self.conn.execute("BEGIN")
            cur.execute("""
                INSERT INTO shipments(ref_number, note, created_at, updated_at)
                VALUES (?, ?, ?, ?);
            """, (ref_number, note.strip(), created, created))
            shipment_id = cur.lastrowid

            for item_id, item_name, qty, received_as, unit_qty in lines:
                qty = int(qty)
                unit_qty = int(unit_qty)
                if qty <= 0 or unit_qty <= 0:
                    raise ValueError(f"Quantity for {item_name} must be greater than zero.")
                if received_as not in {"box", "unit"}:
                    raise ValueError("Received as must be box or unit.")

                cur.execute(
                    "SELECT qty, units_per_box FROM items WHERE id = ?;",
                    (item_id,)
                )
                item = cur.fetchone()
                if not item:
                    raise ValueError(f"Item not found: {item_name}")

                current_qty, units_per_box = item
                expected_units = qty * (
                    int(units_per_box) if received_as == "box" else 1
                )
                if expected_units != unit_qty:
                    unit_qty = expected_units

                new_qty = int(current_qty) + unit_qty
                cur.execute("""
                    UPDATE items
                    SET qty = ?, restock_total = ?, updated_at = ?
                    WHERE id = ?;
                """, (new_qty, new_qty, created, item_id))

                cur.execute("""
                    INSERT INTO shipment_lines(
                        shipment_id, item_id, item_name, entered_qty,
                        received_as, unit_qty, units_per_box
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """, (
                    shipment_id, item_id, item_name, qty,
                    received_as, unit_qty, int(units_per_box)
                ))

                cur.execute("""
                    INSERT INTO txns(
                        type, ref_number, item_id, qty_change, note, created_at
                    ) VALUES ('SHIPMENT', ?, ?, ?, ?, ?);
                """, (
                    ref_number,
                    item_id,
                    unit_qty,
                    f"{qty} {received_as}(s). {note.strip()}",
                    created,
                ))

            self.conn.commit()
            return shipment_id

        except Exception:
            self.conn.rollback()
            raise

    def get_shipment_by_ref(self, ref_number):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, ref_number, COALESCE(note,''), created_at, updated_at
            FROM shipments
            WHERE ref_number = ?
            ORDER BY id DESC
            LIMIT 1;
        """, (ref_number,))
        return cur.fetchone()

    def get_shipment_lines(self, shipment_id):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, item_id, item_name, entered_qty, received_as,
                   unit_qty, units_per_box
            FROM shipment_lines
            WHERE shipment_id = ?
            ORDER BY id;
        """, (shipment_id,))
        return cur.fetchall()

    def get_legacy_shipment_lines(self, ref_number):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT t.item_id, i.name, SUM(t.qty_change) AS units,
                   GROUP_CONCAT(COALESCE(t.note,''), ' | ')
            FROM txns t
            JOIN items i ON i.id = t.item_id
            WHERE t.type = 'SHIPMENT' AND t.ref_number = ?
            GROUP BY t.item_id, i.name
            HAVING SUM(t.qty_change) <> 0
            ORDER BY i.name COLLATE NOCASE;
        """, (ref_number,))
        return cur.fetchall()

    def update_shipment(self, shipment_id, lines, note=""):
        if not lines:
            raise ValueError("A shipment must contain at least one item.")

        cur = self.conn.cursor()
        cur.execute("""
            SELECT ref_number
            FROM shipments
            WHERE id = ?;
        """, (shipment_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError("Shipment not found.")
        ref_number = row[0]

        cur.execute("""
            SELECT item_id, unit_qty
            FROM shipment_lines
            WHERE shipment_id = ?;
        """, (shipment_id,))
        old_units = {}
        for item_id, unit_qty in cur.fetchall():
            old_units[item_id] = old_units.get(item_id, 0) + int(unit_qty)

        validated = []
        new_units = {}

        for item_id, item_name, qty, received_as, unit_qty in lines:
            qty = int(qty)
            if qty <= 0:
                raise ValueError(f"Quantity for {item_name} must be greater than zero.")
            if received_as not in {"box", "unit"}:
                raise ValueError("Received as must be box or unit.")

            cur.execute(
                "SELECT qty, units_per_box, restock_total FROM items WHERE id = ?;",
                (item_id,)
            )
            item = cur.fetchone()
            if not item:
                raise ValueError(f"Item not found: {item_name}")

            current_qty, units_per_box, restock_total = item
            calculated_units = qty * (
                int(units_per_box) if received_as == "box" else 1
            )
            new_units[item_id] = new_units.get(item_id, 0) + calculated_units
            validated.append((
                item_id, item_name, qty, received_as,
                calculated_units, int(units_per_box)
            ))

        # Reducing a historical shipment removes stock that had previously
        # been added. Do not allow the correction to make current stock negative.
        all_ids = set(old_units) | set(new_units)
        for item_id in all_ids:
            cur.execute("SELECT name, qty FROM items WHERE id = ?;", (item_id,))
            item_name, current_qty = cur.fetchone()
            delta = new_units.get(item_id, 0) - old_units.get(item_id, 0)
            if int(current_qty) + delta < 0:
                raise ValueError(
                    f"Cannot reduce shipment enough for {item_name}. "
                    f"Current stock is {current_qty}; correction would remove {-delta} unit(s)."
                )

        updated = now_str()
        try:
            self.conn.execute("BEGIN")

            for item_id in all_ids:
                delta = new_units.get(item_id, 0) - old_units.get(item_id, 0)
                if not delta:
                    continue

                cur.execute("""
                    UPDATE items
                    SET qty = qty + ?,
                        restock_total = MAX(0, restock_total + ?),
                        updated_at = ?
                    WHERE id = ?;
                """, (delta, delta, updated, item_id))

                cur.execute("""
                    INSERT INTO txns(
                        type, ref_number, item_id, qty_change, note, created_at
                    ) VALUES ('SHIPMENT', ?, ?, ?, ?, ?);
                """, (
                    ref_number,
                    item_id,
                    delta,
                    "Shipment correction adjustment",
                    updated,
                ))

            cur.execute(
                "DELETE FROM shipment_lines WHERE shipment_id = ?;",
                (shipment_id,)
            )
            for item_id, item_name, qty, received_as, unit_qty, units_per_box in validated:
                cur.execute("""
                    INSERT INTO shipment_lines(
                        shipment_id, item_id, item_name, entered_qty,
                        received_as, unit_qty, units_per_box
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """, (
                    shipment_id, item_id, item_name, qty,
                    received_as, unit_qty, units_per_box
                ))

            cur.execute("""
                UPDATE shipments
                SET note = ?, updated_at = ?
                WHERE id = ?;
            """, (note.strip(), updated, shipment_id))

            self.conn.commit()

        except Exception:
            self.conn.rollback()
            raise

    def correct_legacy_shipment_units(self, ref_number, lines):
        cur = self.conn.cursor()

        old_rows = self.get_legacy_shipment_lines(ref_number)
        old_units = {int(row[0]): int(row[2]) for row in old_rows}
        new_units = {}
        for item_id, item_name, units in lines:
            units = int(units)
            if units < 0:
                raise ValueError("Shipment units cannot be negative.")
            if units:
                new_units[int(item_id)] = new_units.get(int(item_id), 0) + units

        all_ids = set(old_units) | set(new_units)
        for item_id in all_ids:
            cur.execute("SELECT name, qty FROM items WHERE id = ?;", (item_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError("An item from this legacy shipment no longer exists.")
            item_name, current_qty = row
            delta = new_units.get(item_id, 0) - old_units.get(item_id, 0)
            if int(current_qty) + delta < 0:
                raise ValueError(
                    f"Cannot apply this correction for {item_name}; "
                    "current inventory would become negative."
                )

        corrected = now_str()
        try:
            self.conn.execute("BEGIN")
            for item_id in all_ids:
                delta = new_units.get(item_id, 0) - old_units.get(item_id, 0)
                if not delta:
                    continue
                cur.execute("""
                    UPDATE items
                    SET qty = qty + ?,
                        restock_total = MAX(0, restock_total + ?),
                        updated_at = ?
                    WHERE id = ?;
                """, (delta, delta, corrected, item_id))
                cur.execute("""
                    INSERT INTO txns(
                        type, ref_number, item_id, qty_change, note, created_at
                    ) VALUES ('SHIPMENT', ?, ?, ?, ?, ?);
                """, (
                    ref_number, item_id, delta,
                    "Legacy shipment correction adjustment",
                    corrected
                ))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def record_shipment(self, ref_number, item_id, quantity, received_as="box", note=""):
        if quantity <= 0: raise ValueError("Quantity must be > 0.")
        if received_as not in {"box","unit"}: raise ValueError("Received as must be box or unit.")
        ref_number=ref_number.strip()
        if not ref_number: raise ValueError("Shipment number is required.")
        cur=self.conn.cursor(); cur.execute("SELECT qty,units_per_box FROM items WHERE id=?",(item_id,)); row=cur.fetchone()
        if not row: raise ValueError("Item not found.")
        units_added=int(quantity)*(int(row[1]) if received_as=='box' else 1); new_qty=row[0]+units_added
        self.conn.execute("UPDATE items SET qty=?,restock_total=?,updated_at=? WHERE id=?",(new_qty,new_qty,now_str(),item_id))
        self.conn.execute("INSERT INTO txns(type,ref_number,item_id,qty_change,note,created_at) VALUES('SHIPMENT',?,?,?,?,?)",
                          (ref_number,item_id,units_added,f"{quantity} {received_as}(s). {note.strip()}",now_str()))
        self.conn.commit()

    def record_invoice(self, ref_number, item_id, unit_qty, note=""):
        if unit_qty <= 0: raise ValueError("Quantity must be > 0.")
        ref_number=ref_number.strip()
        if not ref_number: raise ValueError("Invoice number is required.")
        cur=self.conn.cursor(); cur.execute("SELECT qty FROM items WHERE id=?",(item_id,)); row=cur.fetchone()
        if not row: raise ValueError("Item not found.")
        if unit_qty > row[0]: raise ValueError(f"Not enough stock. Current stock is {row[0]} unit(s).")
        self.conn.execute("UPDATE items SET qty=?,updated_at=? WHERE id=?",(row[0]-unit_qty,now_str(),item_id))
        self.conn.execute("INSERT INTO txns(type,ref_number,item_id,qty_change,note,created_at) VALUES('INVOICE',?,?,?,?,?)",(ref_number,item_id,-unit_qty,note.strip(),now_str()))
        self.conn.commit()

    def list_document_history(self, limit=500):
        cur = self.conn.cursor()
        rows = []

        # One row per invoice/order.
        cur.execute("""
            SELECT inv.id, inv.created_at, inv.ref_number,
                   COALESCE(inv.store_name,''),
                   COALESCE(inv.total,0),
                   COALESCE(inv.status,'open'),
                   COALESCE(inv.payment_method,''),
                   COUNT(il.id),
                   COALESCE(SUM(il.unit_qty),0)
            FROM invoices inv
            LEFT JOIN invoice_lines il ON il.invoice_id = inv.id
            GROUP BY inv.id
            ORDER BY inv.id DESC;
        """)
        for invoice_id, created_at, ref, store, total, status, payment, line_count, units in cur.fetchall():
            is_internal = str(payment or "").strip().lower() == "internal use"
            typ = "Internal Use" if is_internal else "Invoice"
            display_status = "Completed" if is_internal else str(status or "open").title()
            summary = (
                f"{int(line_count or 0)} item(s) / {int(units or 0)} unit(s)"
                + ("" if is_internal else f" / {money(total)}")
            )
            rows.append((
                f"invoice:{invoice_id}",
                created_at,
                typ,
                ref,
                store or "—",
                summary,
                display_status,
            ))

        # New-format shipments created after detailed shipment storage was added.
        cur.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='shipments';
        """)
        has_shipments = cur.fetchone() is not None

        if has_shipments:
            cur.execute("""
                SELECT s.id, s.created_at, s.ref_number, COALESCE(s.note,''),
                       COUNT(sl.id), COALESCE(SUM(sl.unit_qty),0),
                       CASE WHEN EXISTS(
                           SELECT 1 FROM txns t
                           WHERE t.type='SHIPMENT'
                             AND t.ref_number=s.ref_number
                             AND LOWER(COALESCE(t.note,'')) LIKE '%correction%'
                       ) THEN 1 ELSE 0 END
                FROM shipments s
                LEFT JOIN shipment_lines sl ON sl.shipment_id=s.id
                GROUP BY s.id
                ORDER BY s.id DESC;
            """)
            for shipment_id, created_at, ref, note, line_count, units, corrected in cur.fetchall():
                rows.append((
                    f"shipment:{ref}",
                    created_at,
                    "Shipment",
                    ref,
                    note.strip() or "Warehouse Receipt",
                    f"{int(line_count or 0)} item(s) / +{int(units or 0)} unit(s)",
                    "Corrected" if corrected else "Completed",
                ))

        # Legacy shipments were recorded only in txns. Group the entire shipment
        # reference into one logical row instead of exposing every item movement.
        if has_shipments:
            legacy_sql = """
                SELECT MIN(t.created_at), t.ref_number,
                       COUNT(DISTINCT t.item_id),
                       COALESCE(SUM(t.qty_change),0),
                       MAX(CASE WHEN LOWER(COALESCE(t.note,'')) LIKE '%correction%'
                                THEN 1 ELSE 0 END)
                FROM txns t
                WHERE t.type='SHIPMENT'
                  AND NOT EXISTS(
                      SELECT 1 FROM shipments s WHERE s.ref_number=t.ref_number
                  )
                GROUP BY t.ref_number;
            """
        else:
            legacy_sql = """
                SELECT MIN(t.created_at), t.ref_number,
                       COUNT(DISTINCT t.item_id),
                       COALESCE(SUM(t.qty_change),0),
                       MAX(CASE WHEN LOWER(COALESCE(t.note,'')) LIKE '%correction%'
                                THEN 1 ELSE 0 END)
                FROM txns t
                WHERE t.type='SHIPMENT'
                GROUP BY t.ref_number;
            """

        cur.execute(legacy_sql)
        for created_at, ref, item_count, units, corrected in cur.fetchall():
            rows.append((
                f"shipment:{ref}",
                created_at,
                "Shipment",
                ref,
                "Legacy Shipment",
                f"{int(item_count or 0)} item(s) / +{int(units or 0)} unit(s)",
                "Corrected" if corrected else "Completed",
            ))

        # Refunds get one row per refund rather than one row per returned item.
        cur.execute("""
            SELECT r.id, r.created_at, COALESCE(inv.ref_number,''),
                   COALESCE(inv.store_name,''), COALESCE(r.total,0),
                   COUNT(rl.id), COALESCE(SUM(rl.qty),0),
                   COALESCE(r.refund_method,'')
            FROM refunds r
            LEFT JOIN invoices inv ON inv.id=r.invoice_id
            LEFT JOIN refund_lines rl ON rl.refund_id=r.id
            GROUP BY r.id
            ORDER BY r.id DESC;
        """)
        for refund_id, created_at, invoice_ref, store, total, line_count, qty, method in cur.fetchall():
            rows.append((
                f"refund:{refund_id}",
                created_at,
                "Refund",
                f"Refund #{refund_id}",
                store or (f"Invoice {invoice_ref}" if invoice_ref else "—"),
                f"{int(line_count or 0)} item(s) / {money(total)} / {method}",
                "Completed",
            ))

        rows.sort(key=lambda row: str(row[1] or ""), reverse=True)
        return rows[:int(limit)]

    def get_refund_history_detail(self, refund_id):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT r.id, r.created_at, COALESCE(r.refund_method,''),
                   COALESCE(r.total,0), COALESCE(r.note,''),
                   r.invoice_id, COALESCE(inv.ref_number,''),
                   COALESCE(inv.store_name,'')
            FROM refunds r
            LEFT JOIN invoices inv ON inv.id=r.invoice_id
            WHERE r.id=?;
        """, (refund_id,))
        header = cur.fetchone()
        if not header:
            return None, []
        cur.execute("""
            SELECT item_name, qty, unit_price, line_total
            FROM refund_lines
            WHERE refund_id=?
            ORDER BY id;
        """, (refund_id,))
        return header, cur.fetchall()

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
        self.title("Tea Gather Inventory")

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
        self.invoice_adjustments = []
        self.update_settings = self._load_update_settings()
        self._update_check_running = False

        self._build_ui()
        self._build_menu_bar()
        self.auto_save_completed_monthly_statements()
        self.refresh_all()

        # Perform a quiet update check after the interface has finished loading.
        if self.update_settings.get("check_updates_on_startup", True):
            self.after(1500, self.check_for_updates_in_background)

    def _create_scrollable_area(self, parent, padding=12):
        outer = ttk.Frame(parent)
        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        content = ttk.Frame(canvas, padding=padding)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")

        def update_region(event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def fit_width(event):
            canvas.itemconfigure(window_id, width=event.width)

        content.bind("<Configure>", update_region)
        canvas.bind("<Configure>", fit_width)

        outer._scroll_canvas = canvas
        outer._scroll_content = content
        return outer, content

    def _make_scrollable_popup_content(self, window, padding=12):
        outer, content = self._create_scrollable_area(window, padding=padding)
        outer.pack(fill="both", expand=True)
        return content

    def _build_ui(self):
        header = ttk.Frame(self, padding=12)
        header.pack(fill="x")
        ttk.Label(header, text="Tea Gather Inventory", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text=f"Database: {DB_FILE}", foreground="#666").pack(side="right")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=12)

        self.tab_inventory_page, self.tab_inventory = self._create_scrollable_area(self.notebook)
        self.tab_shipment_page, self.tab_shipment = self._create_scrollable_area(self.notebook)
        self.tab_invoice_page, self.tab_invoice = self._create_scrollable_area(self.notebook)
        self.tab_customers_page, self.tab_customers = self._create_scrollable_area(self.notebook)
        self.tab_refunds_page, self.tab_refunds = self._create_scrollable_area(self.notebook)
        self.tab_transactions_page, self.tab_transactions = self._create_scrollable_area(self.notebook)
        self.tab_statements_page, self.tab_statements = self._create_scrollable_area(self.notebook)
        self.tab_history_page, self.tab_history = self._create_scrollable_area(self.notebook)

        self.notebook.add(self.tab_inventory_page, text="Inventory")
        self.notebook.add(self.tab_shipment_page, text="Add Shipment")
        self.notebook.add(self.tab_invoice_page, text="Record Invoice")
        self.notebook.add(self.tab_customers_page, text="Customer Management")
        self.notebook.add(self.tab_refunds_page, text="Refunds / Accounts")
        self.notebook.add(self.tab_transactions_page, text="Transactions")
        self.notebook.add(self.tab_statements_page, text="Statements")
        self.notebook.add(self.tab_history_page, text="History")
        self.tab_inventory._notebook_page = self.tab_inventory_page
        self.tab_shipment._notebook_page = self.tab_shipment_page
        self.tab_invoice._notebook_page = self.tab_invoice_page
        self.tab_customers._notebook_page = self.tab_customers_page
        self.tab_refunds._notebook_page = self.tab_refunds_page
        self.tab_transactions._notebook_page = self.tab_transactions_page
        self.tab_statements._notebook_page = self.tab_statements_page
        self.tab_history._notebook_page = self.tab_history_page

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
        help_menu.add_command(label="Check for Updates...", command=self.check_for_updates)
        help_menu.add_command(label="Update Settings...", command=self.show_update_settings)
        help_menu.add_separator()
        help_menu.add_command(label="About", command=self.show_about)
        menu_bar.add_cascade(label="Help", menu=help_menu)

    def _select_tab(self, tab):
        page = getattr(tab, "_notebook_page", None)
        self.notebook.select(page if page is not None else tab)

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

    def _load_update_settings(self):
        settings = dict(DEFAULT_SETTINGS)
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as settings_file:
                saved = json.load(settings_file)
            if isinstance(saved, dict):
                settings.update(saved)
        except (OSError, json.JSONDecodeError):
            pass
        return settings

    def _save_update_settings(self):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as settings_file:
                json.dump(self.update_settings, settings_file, indent=2)
        except OSError as exc:
            messagebox.showerror(
                "Settings Error",
                f"Stalk could not save the update settings.\n\n{exc}",
            )

    def show_update_settings(self):
        dialog = tk.Toplevel(self)
        dialog.title("Update Settings")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)

        startup_var = tk.BooleanVar(
            value=bool(self.update_settings.get("check_updates_on_startup", True))
        )
        ttk.Checkbutton(
            frame,
            text="Automatically check for updates when Stalk starts",
            variable=startup_var,
        ).pack(anchor="w")

        skipped = str(self.update_settings.get("skipped_version", "")).strip()
        skipped_label = ttk.Label(
            frame,
            text=(
                f"Skipped version: {skipped}"
                if skipped
                else "No update version is currently skipped."
            ),
            foreground="#666",
        )
        skipped_label.pack(anchor="w", pady=(10, 0))

        def clear_skipped():
            self.update_settings["skipped_version"] = ""
            skipped_label.config(text="No update version is currently skipped.")

        if skipped:
            ttk.Button(frame, text="Clear Skipped Version", command=clear_skipped).pack(
                anchor="w", pady=(8, 0)
            )

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(18, 0))

        def save_settings():
            self.update_settings["check_updates_on_startup"] = startup_var.get()
            self._save_update_settings()
            dialog.destroy()

        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(
            buttons, text="Save", style="Big.TButton", command=save_settings
        ).pack(side="right", padx=(0, 8))

        dialog.update_idletasks()
        x = self.winfo_rootx() + max((self.winfo_width() - dialog.winfo_width()) // 2, 0)
        y = self.winfo_rooty() + max((self.winfo_height() - dialog.winfo_height()) // 2, 0)
        dialog.geometry(f"+{x}+{y}")

    @staticmethod
    def _version_tuple(version):
        """Convert a version such as 1.2.3 into a comparable integer tuple."""
        cleaned = str(version).strip().lstrip("vV")
        parts = cleaned.split(".")
        values = []
        for part in parts:
            match = re.match(r"(\d+)", part)
            values.append(int(match.group(1)) if match else 0)
        while len(values) < 3:
            values.append(0)
        return tuple(values)

    def _fetch_update_info(self):
        if "USERNAME/REPOSITORY" in UPDATE_INFO_URL:
            raise ValueError("The GitHub update URL has not been configured.")

        request = urllib.request.Request(
            UPDATE_INFO_URL,
            headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            update_info = json.loads(response.read().decode("utf-8"))

        latest_version = str(update_info.get("version", "")).strip()
        if not latest_version:
            raise ValueError("The update file does not contain a version.")
        return update_info

    def check_for_updates_in_background(self):
        """Quietly check for a newer release without delaying application startup."""
        if self._update_check_running:
            return
        self._update_check_running = True

        def worker():
            try:
                update_info = self._fetch_update_info()
            except Exception:
                # Startup checks remain silent when offline or GitHub is unavailable.
                self.after(0, lambda: setattr(self, "_update_check_running", False))
                return

            def finish():
                self._update_check_running = False
                latest = str(update_info.get("version", "")).strip()
                skipped = str(self.update_settings.get("skipped_version", "")).strip()
                if (
                    self._version_tuple(latest) > self._version_tuple(APP_VERSION)
                    and self._version_tuple(latest) != self._version_tuple(skipped)
                ):
                    self._show_update_available_dialog(update_info, allow_skip=True)

            self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def check_for_updates(self):
        """Manually check GitHub for a newer release."""
        if self._update_check_running:
            messagebox.showinfo("Update Check", "An update check is already in progress.")
            return
        self._update_check_running = True

        def worker():
            try:
                update_info = self._fetch_update_info()
            except (urllib.error.URLError, TimeoutError):
                self.after(0, lambda: self._finish_manual_update_error(
                    "Unable to Check for Updates",
                    "Stalk could not connect to GitHub.\n\nCheck the Internet connection and try again.",
                ))
                return
            except (json.JSONDecodeError, ValueError, KeyError) as exc:
                self.after(0, lambda exc=exc: self._finish_manual_update_error(
                    "Invalid Update Information",
                    f"The GitHub update file could not be read.\n\n{exc}",
                ))
                return
            except Exception as exc:
                self.after(0, lambda exc=exc: self._finish_manual_update_error(
                    "Update Check Error",
                    f"An unexpected error occurred while checking for updates.\n\n{exc}",
                ))
                return

            def finish():
                self._update_check_running = False
                latest = str(update_info.get("version", "")).strip()
                if self._version_tuple(latest) > self._version_tuple(APP_VERSION):
                    self._show_update_available_dialog(update_info, allow_skip=True)
                else:
                    messagebox.showinfo(
                        "Stalk Is Up to Date",
                        f"You are running the latest version.\n\nVersion {APP_VERSION}",
                    )

            self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_manual_update_error(self, title, message):
        self._update_check_running = False
        messagebox.showerror(title, message)

    def _show_update_available_dialog(self, update_info, allow_skip=True):
        latest_version = str(update_info.get("version", "")).strip()
        release_url = str(update_info.get("release_url", "")).strip()
        notes = update_info.get("notes", [])
        if isinstance(notes, list):
            notes_text = "\n".join(f"• {note}" for note in notes)
        else:
            notes_text = str(notes).strip()

        dialog = tk.Toplevel(self)
        dialog.title("Stalk Update Available")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Stalk Update Available", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text=f"Installed version: {APP_VERSION}\nLatest version: {latest_version}",
        ).pack(anchor="w", pady=(10, 0))

        if notes_text:
            notes_box = ttk.LabelFrame(frame, text="What's New", padding=10)
            notes_box.pack(fill="both", expand=True, pady=(12, 0))
            ttk.Label(notes_box, text=notes_text, justify="left", wraplength=520).pack(anchor="w")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(18, 0))

        def open_release():
            if release_url:
                webbrowser.open(release_url)
                dialog.destroy()
            else:
                messagebox.showinfo(
                    "Release Link Missing",
                    "The update was found, but version.json does not contain a release_url.",
                    parent=dialog,
                )

        def skip_version():
            self.update_settings["skipped_version"] = latest_version
            self._save_update_settings()
            dialog.destroy()

        ttk.Button(buttons, text="Later", command=dialog.destroy).pack(side="right")
        if allow_skip:
            ttk.Button(buttons, text="Skip This Version", command=skip_version).pack(
                side="right", padx=(0, 8)
            )
        ttk.Button(
            buttons, text="View Update", style="Big.TButton", command=open_release
        ).pack(side="right", padx=(0, 8))

        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.update_idletasks()
        x = self.winfo_rootx() + max((self.winfo_width() - dialog.winfo_width()) // 2, 0)
        y = self.winfo_rooty() + max((self.winfo_height() - dialog.winfo_height()) // 2, 0)
        dialog.geometry(f"+{x}+{y}")

    def show_about(self):
        messagebox.showinfo(
            f"About {APP_NAME}",
            f"{APP_NAME}\nVersion {APP_VERSION}\n\n"
            "Inventory, shipments, invoicing, customer management, transactions, "
            "refunds, statements, and CSV exports.",
        )

    # ---------- Inventory Tab ----------
    def _build_inventory_tab(self):
        top = ttk.Frame(self.tab_inventory)
        top.pack(fill="x", pady=(0, 10))

        ttk.Label(top, text="Search Item:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_combo = ttk.Combobox(
            top,
            textvariable=self.search_var,
            values=[row[1] for row in self.db.list_items("")],
            width=36,
        )
        self.search_combo.pack(side="left", padx=8)
        self._configure_item_autocomplete(
            self.search_combo, self.search_var, filter_inventory=True
        )
        self.search_combo.bind(
            "<<ComboboxSelected>>", self.on_inventory_search_selected, add="+"
        )
        ttk.Button(top, text="Search", style="Big.TButton", command=self.refresh_inventory).pack(side="left")
        ttk.Button(top, text="Clear", style="Big.TButton", command=self._clear_search).pack(side="left", padx=(8, 0))
        add_box = ttk.LabelFrame(self.tab_inventory, text="Add New Item", padding=12)
        add_box.pack(fill="x", pady=(0, 10))

        self.new_name = tk.StringVar()
        self.new_sku = tk.StringVar()
        self.new_price = tk.StringVar(value="0.00")
        self.new_unit_price = tk.StringVar(value="0.00")
        self.new_units_per_box = tk.StringVar(value="1")
        self.new_weight = tk.StringVar()

        row1 = ttk.Frame(add_box)
        row1.pack(fill="x", pady=4)
        ttk.Label(row1, text="Item Name (required):", width=22).pack(side="left")
        ttk.Entry(row1, textvariable=self.new_name, width=30).pack(side="left", padx=8)
        ttk.Label(row1, text="SKU (optional):", width=14).pack(side="left")
        ttk.Entry(row1, textvariable=self.new_sku, width=18).pack(side="left", padx=8)

        row2 = ttk.Frame(add_box)
        row2.pack(fill="x", pady=4)
        ttk.Label(row2, text="Box Price:", width=22).pack(side="left")
        ttk.Entry(row2, textvariable=self.new_price, width=10).pack(side="left", padx=8)
        ttk.Label(row2, text="Unit Price:", width=14).pack(side="left")
        ttk.Entry(row2, textvariable=self.new_unit_price, width=10).pack(side="left", padx=8)
        ttk.Label(row2, text="Units/Box:").pack(side="left", padx=(8,0))
        ttk.Entry(row2, textvariable=self.new_units_per_box, width=7).pack(side="left", padx=8)
        ttk.Label(row2, text="Weight:").pack(side="left")
        ttk.Entry(row2, textvariable=self.new_weight, width=14).pack(side="left", padx=8)

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
        ttk.Button(action_row, text="Manage Temporary Surcharges", style="Big.TButton", command=self.on_manage_surcharges).pack(side="left", padx=(8, 0))
        ttk.Button(action_row, text="Delete Selected Item", style="Big.TButton", command=self.on_delete_item).pack(side="left", padx=(8, 0))
        ttk.Button(action_row, text="Export Inventory CSV", style="Big.TButton",
                   command=lambda: self.export_treeview_csv(self.tree, "inventory")).pack(side="left", padx=(8, 0))
        ttk.Label(action_row, text="Editing keeps the existing stock and transaction history.", foreground="#666").pack(side="left", padx=12)

        table_frame = ttk.Frame(self.tab_inventory)
        table_frame.pack(fill="both", expand=True)

        cols = ("name", "sku", "weight", "upb", "box_price", "unit_price", "units", "boxes", "loose", "value", "daily_usage", "doh", "priority", "updated")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=12)
        self.tree.heading("name", text="Item Name")
        self.tree.heading("sku", text="SKU")
        self.tree.heading("weight", text="Weight")
        self.tree.heading("upb", text="Units/Box")
        self.tree.heading("box_price", text="Box Price")
        self.tree.heading("unit_price", text="Unit Price")
        self.tree.heading("units", text="Total Units")
        self.tree.heading("boxes", text="Full Boxes")
        self.tree.heading("loose", text="Loose Units")
        self.tree.heading("value", text="Inventory Value")
        self.tree.heading("daily_usage", text="Avg Daily Out")
        self.tree.heading("doh", text="Days On Hand")
        self.tree.heading("priority", text="Stock Priority")
        self.tree.heading("updated", text="Last Updated")

        self.tree.column("name", width=240, anchor="w")
        self.tree.column("sku", width=110, anchor="center")
        self.tree.column("weight", width=110, anchor="center")
        self.tree.column("upb", width=85, anchor="center")
        self.tree.column("box_price", width=90, anchor="e")
        self.tree.column("unit_price", width=90, anchor="e")
        self.tree.column("units", width=90, anchor="center")
        self.tree.column("boxes", width=85, anchor="center")
        self.tree.column("loose", width=85, anchor="center")
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

    def on_inventory_search_typed(self, event=None):
        return self._autocomplete_keyrelease(
            self.search_combo, self.search_var, event
        )

    def on_inventory_search_selected(self, event=None):
        # Highlight the exact row chosen from the autocomplete suggestions.
        selected_name = self.search_var.get().strip()
        self.refresh_inventory()

        for item_id in self.tree.get_children():
            values = self.tree.item(item_id, "values")
            if values and str(values[0]).casefold() == selected_name.casefold():
                self.tree.selection_set(item_id)
                self.tree.focus(item_id)
                self.tree.see(item_id)
                break

    def on_add_item(self):
        try:
            self.db.add_item(
                self.new_name.get(),
                self.new_sku.get(),
                self.new_price.get(),
                self.new_unit_price.get(),
                self.new_units_per_box.get(),
                self.new_weight.get()
            )
            self.new_name.set("")
            self.new_sku.set("")
            self.new_price.set("0.00")
            self.new_unit_price.set("0.00")
            self.new_units_per_box.set("1")
            self.new_weight.set("")
            messagebox.showinfo("Added", "Item added.")
            self.refresh_all()
        except sqlite3.IntegrityError:
            messagebox.showerror("Error", "That item name already exists.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def on_edit_item(self):
        selected=self.tree.selection()
        if not selected: messagebox.showerror("Error","Select an item to edit."); return
        item_id=int(selected[0]); d=self.db.get_item_details(item_id)
        if not d: messagebox.showerror("Error","Item not found."); return
        _id,name,box_price,unit_price,upb,weight,qty=d
        sku=self.db.conn.execute("SELECT COALESCE(sku,'') FROM items WHERE id=?",(item_id,)).fetchone()[0]
        dialog=tk.Toplevel(self); dialog.title("Edit Item"); dialog.transient(self); dialog.grab_set()
        frame=self._make_scrollable_popup_content(dialog,padding=16)
        vars_=[tk.StringVar(value=v) for v in (name,sku,f"{box_price:.2f}",f"{unit_price:.2f}",str(upb),weight)]
        labels=("Item Name:","SKU:","Box Price:","Unit Price:","Units Per Box:","Weight:")
        for r,(label,var) in enumerate(zip(labels,vars_)):
            ttk.Label(frame,text=label,width=17).grid(row=r,column=0,sticky="w",pady=5); ttk.Entry(frame,textvariable=var,width=34).grid(row=r,column=1,sticky="ew",pady=5)
        ttk.Label(frame,text=f"Current inventory: {qty} unit(s) = {qty//upb} box(es) + {qty%upb} loose",foreground="#666").grid(row=6,column=0,columnspan=2,sticky="w",pady=6)
        def save():
            try:
                new_upb=int(vars_[4].get() or 1); convert=False
                if upb==1 and new_upb>1 and qty>0:
                    convert=messagebox.askyesno("Convert Existing Stock", f"Current stock is {qty}. Does that represent {qty} BOXES?\n\nYes: convert to {qty*new_upb} units.\nNo: keep {qty} units.", parent=dialog)
                self.db.update_item(item_id,vars_[0].get(),vars_[1].get(),vars_[2].get(),vars_[3].get(),vars_[4].get(),vars_[5].get(),convert)
                dialog.destroy(); self.refresh_all(); messagebox.showinfo("Updated","Item details updated.")
            except Exception as e: messagebox.showerror("Error",str(e),parent=dialog)
        buttons=ttk.Frame(frame); buttons.grid(row=7,column=0,columnspan=2,sticky="e",pady=(8,0)); ttk.Button(buttons,text="Cancel",command=dialog.destroy).pack(side="right"); ttk.Button(buttons,text="Save Changes",style="Big.TButton",command=save).pack(side="right",padx=8)
        dialog.bind("<Return>",lambda e:save()); dialog.bind("<Escape>",lambda e:dialog.destroy())

    def on_manage_surcharges(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showerror("Error", "Select an item first.")
            return
        item_id = int(selected[0])
        item_name = self.tree.item(selected[0], "values")[0]

        win = tk.Toplevel(self)
        win.title(f"Temporary Surcharges - {item_name}")
        win.transient(self)
        win.geometry("900x620")
        win.minsize(780, 520)
        surcharge_content = self._make_scrollable_popup_content(win, padding=0)

        form = ttk.LabelFrame(surcharge_content, text="Add Temporary Product Surcharge", padding=12)
        form.pack(fill="x", padx=12, pady=12)
        reason_var = tk.StringVar(value="Tariff")
        box_var = tk.StringVar(value="0.00")
        unit_var = tk.StringVar(value="0.00")
        start_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        end_var = tk.StringVar()
        note_var = tk.StringVar()
        ttk.Label(form, text="Reason:").grid(row=0,column=0,sticky="w",pady=5)
        ttk.Combobox(form,textvariable=reason_var,values=("Tariff","Demurrage","Freight","Customs","Other"),width=20).grid(row=0,column=1,sticky="w",padx=8,pady=5)
        ttk.Label(form, text="Box surcharge:").grid(row=0,column=2,sticky="w",pady=5)
        ttk.Entry(form,textvariable=box_var,width=12).grid(row=0,column=3,sticky="w",padx=8,pady=5)
        ttk.Label(form, text="Unit surcharge:").grid(row=0,column=4,sticky="w",pady=5)
        ttk.Entry(form,textvariable=unit_var,width=12).grid(row=0,column=5,sticky="w",padx=8,pady=5)
        ttk.Label(form, text="Start date:").grid(row=1,column=0,sticky="w",pady=5)
        ttk.Entry(form,textvariable=start_var,width=14).grid(row=1,column=1,sticky="w",padx=8,pady=5)
        ttk.Label(form, text="End date (optional):").grid(row=1,column=2,sticky="w",pady=5)
        ttk.Entry(form,textvariable=end_var,width=14).grid(row=1,column=3,sticky="w",padx=8,pady=5)
        ttk.Label(form, text="Note:").grid(row=2,column=0,sticky="w",pady=5)
        ttk.Entry(form,textvariable=note_var).grid(row=2,column=1,columnspan=5,sticky="ew",padx=8,pady=5)
        form.columnconfigure(5,weight=1)

        table_box = ttk.LabelFrame(surcharge_content, text="Surcharge History", padding=10)
        table_box.pack(fill="both", expand=True, padx=12, pady=(0,12))
        cols=("reason","box","unit","start","end","status","note","updated")
        tree=ttk.Treeview(table_box,columns=cols,show="headings")
        for c,title,w,anchor in [("reason","Reason",110,"w"),("box","Box",90,"e"),("unit","Unit",90,"e"),("start","Start",100,"center"),("end","End",100,"center"),("status","Status",90,"center"),("note","Note",220,"w"),("updated","Updated",145,"center")]:
            tree.heading(c,text=title); tree.column(c,width=w,anchor=anchor)
        tree.pack(fill="both",expand=True)

        def refresh_rows():
            for row in tree.get_children(): tree.delete(row)
            today=datetime.now().strftime("%Y-%m-%d")
            for sid,reason,box,unit,start,end,active,note,updated in self.db.list_item_surcharges(item_id):
                currently_active = bool(active) and start <= today and (not end or end >= today)
                status = "Active" if currently_active else ("Enabled" if active else "Inactive")
                tree.insert("","end",iid=str(sid),values=(reason,money(box),money(unit),start,end or "—",status,note,updated))

        def save_surcharge():
            try:
                self.db.save_item_surcharge(item_id,reason_var.get(),box_var.get(),unit_var.get(),start_var.get(),end_var.get(),note_var.get())
                refresh_rows(); messagebox.showinfo("Saved","Temporary surcharge saved.",parent=win)
            except Exception as exc: messagebox.showerror("Error",str(exc),parent=win)

        def set_active(active):
            selected_row=tree.selection()
            if not selected_row:
                messagebox.showerror("Error","Select a surcharge first.",parent=win); return
            self.db.set_item_surcharge_active(int(selected_row[0]),active); refresh_rows()

        buttons=ttk.Frame(surcharge_content,padding=(12,0,12,12)); buttons.pack(fill="x")
        ttk.Button(buttons,text="Save Surcharge",style="Big.TButton",command=save_surcharge).pack(side="left")
        ttk.Button(buttons,text="Activate Selected",command=lambda:set_active(True)).pack(side="left",padx=8)
        ttk.Button(buttons,text="Deactivate Selected",command=lambda:set_active(False)).pack(side="left")
        ttk.Button(buttons,text="Close",command=win.destroy).pack(side="right")
        refresh_rows()

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
        for row in self.tree.get_children(): self.tree.delete(row)
        for item_id,name,sku,box_price,unit_price,upb,weight,qty,s0,s1,s2,updated in self.db.list_items(self.search_var.get()):
            full_boxes = qty // upb; loose_units = qty % upb
            inventory_value = full_boxes * box_price + loose_units * unit_price
            avg=(s0/30)*.60+(s1/30)*.30+(s2/30)*.10
            if qty<=0: days="0.0"; priority="Out of Stock"; tag="critical"
            elif avg<=0: days="—"; priority="No recent sales"; tag=""
            else:
                doh=qty/avg; days=f"{doh:.1f}"
                if doh<14: priority="Critical"; tag="critical"
                elif doh<30: priority="Low"; tag="low"
                elif doh<=60: priority="Watch"; tag="watch"
                else: priority="Healthy"; tag=""
            self.tree.insert("","end",iid=str(item_id),values=(name,sku,weight,upb,money(box_price),money(unit_price),qty,full_boxes,loose_units,money(inventory_value),f"{avg:.2f}",days,priority,updated),tags=(tag,) if tag else ())
        self._refresh_item_dropdowns()

    def _build_shipment_tab(self):
        box = ttk.LabelFrame(self.tab_shipment, text="Add Shipment (Increase Stock)", padding=12)
        box.pack(fill="x", pady=(0, 10))

        self.ship_ref = tk.StringVar()
        self.ship_qty = tk.StringVar()
        self.ship_note = tk.StringVar()
        self.ship_item = tk.StringVar()
        self.ship_type = tk.StringVar(value="box")

        r1 = ttk.Frame(box); r1.pack(fill="x", pady=6)
        ttk.Label(r1, text="Shipment #:", width=18).pack(side="left")
        ttk.Entry(r1, textvariable=self.ship_ref, width=30).pack(side="left", padx=8)

        r2 = ttk.Frame(box); r2.pack(fill="x", pady=6)
        ttk.Label(r2, text="Item:", width=18).pack(side="left")
        self.ship_item_cb = ttk.Combobox(
            r2, textvariable=self.ship_item, width=40
        )
        self.ship_item_cb.pack(side="left", padx=8)
        self._configure_item_autocomplete(self.ship_item_cb, self.ship_item)
        ttk.Label(r2, text="Qty:").pack(side="left", padx=(12, 0))
        ttk.Entry(r2, textvariable=self.ship_qty, width=10).pack(side="left", padx=8)
        ttk.Combobox(r2,textvariable=self.ship_type,state="readonly",values=("box","unit"),width=8).pack(side="left",padx=4)
        ttk.Button(r2, text="Add Line", style="Big.TButton", command=self.on_add_shipment_line).pack(side="left", padx=(8, 0))

        r3 = ttk.Frame(box); r3.pack(fill="x", pady=6)
        ttk.Label(r3, text="Note (optional):", width=18).pack(side="left")
        ttk.Entry(r3, textvariable=self.ship_note, width=60).pack(side="left", padx=8)

        lines_box = ttk.LabelFrame(self.tab_shipment, text="Shipment Lines To Save", padding=12)
        lines_box.pack(fill="both", expand=True, pady=(0, 10))

        cols = ("item", "qty", "type", "units")
        self.ship_lines_tree = ttk.Treeview(lines_box, columns=cols, show="headings", height=8)
        self.ship_lines_tree.heading("item", text="Item")
        self.ship_lines_tree.heading("qty", text="Quantity")
        self.ship_lines_tree.heading("type", text="Received As")
        self.ship_lines_tree.heading("units", text="Units Added")
        self.ship_lines_tree.column("item", width=450)
        self.ship_lines_tree.column("qty", width=100, anchor="center")
        self.ship_lines_tree.column("type", width=110, anchor="center")
        self.ship_lines_tree.column("units", width=110, anchor="center")

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
            item_name=self.ship_item.get(); item_id=self._selected_item_id(item_name); qty=int(self.ship_qty.get()); typ=self.ship_type.get()
            if qty<=0: raise ValueError("Quantity must be > 0.")
            item=self.db.get_item_details(item_id); units=qty*(item[4] if typ=='box' else 1)
            self.shipment_lines.append((item_id,item_name,qty,typ,units)); self.ship_qty.set(""); self.refresh_shipment_lines()
        except Exception as e: messagebox.showerror("Error",str(e))

    def on_save_shipment(self):
        try:
            ref = self.ship_ref.get().strip()
            if not ref:
                raise ValueError("Shipment number is required.")
            if not self.shipment_lines:
                raise ValueError("Add at least one item to the shipment list.")

            self.db.record_full_shipment(
                ref,
                list(self.shipment_lines),
                self.ship_note.get(),
            )

            self.shipment_lines.clear()
            self.ship_ref.set("")
            self.ship_qty.set("")
            self.ship_note.set("")
            self.refresh_shipment_lines()

            messagebox.showinfo(
                "Saved",
                "Shipment recorded with full line details and stock increased."
            )
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
        for row in self.ship_lines_tree.get_children(): self.ship_lines_tree.delete(row)
        for _id,name,qty,typ,units in self.shipment_lines: self.ship_lines_tree.insert("","end",values=(name,qty,typ.title(),units))

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
        self.inv_sale_type = tk.StringVar(value="box")

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
        self.inv_item_cb = ttk.Combobox(
            add_line, textvariable=self.inv_item
        )
        self.inv_item_cb.grid(row=0, column=1, sticky="ew", padx=8)
        self._configure_item_autocomplete(self.inv_item_cb, self.inv_item)
        ttk.Label(add_line, text="Qty:").grid(row=0, column=2, sticky="w", padx=(8, 0))
        qty_entry = ttk.Entry(add_line, textvariable=self.inv_qty, width=9)
        qty_entry.grid(row=0, column=3, padx=8)
        ttk.Combobox(add_line,textvariable=self.inv_sale_type,state="readonly",values=("box","unit"),width=8).grid(row=0,column=4,padx=4)
        ttk.Button(add_line, text="Add Line", style="Big.TButton", command=self.on_add_invoice_line).grid(row=0, column=5)
        qty_entry.bind("<Return>", lambda event: self.on_add_invoice_line())

        lines_box = ttk.LabelFrame(right, text="Invoice Lines", padding=10)
        lines_box.grid(row=1, column=0, sticky="nsew")
        lines_box.rowconfigure(0, weight=1)
        lines_box.columnconfigure(0, weight=1)

        cols = ("item", "qty", "sale_type", "units", "base_price", "surcharge", "price", "pricing", "line_total")
        self.inv_lines_tree = ttk.Treeview(lines_box, columns=cols, show="headings", height=12)
        self.inv_lines_tree.heading("item", text="Item")
        self.inv_lines_tree.heading("qty", text="Quantity")
        self.inv_lines_tree.heading("sale_type", text="Sold As")
        self.inv_lines_tree.heading("units", text="Stock Units")
        self.inv_lines_tree.heading("base_price", text="Base Price")
        self.inv_lines_tree.heading("surcharge", text="Surcharge")
        self.inv_lines_tree.heading("price", text="Charged Price")
        self.inv_lines_tree.heading("pricing", text="Pricing")
        self.inv_lines_tree.heading("line_total", text="Line Total")
        self.inv_lines_tree.column("item", width=360, anchor="w")
        self.inv_lines_tree.column("qty", width=90, anchor="center", stretch=False)
        self.inv_lines_tree.column("sale_type", width=90, anchor="center", stretch=False)
        self.inv_lines_tree.column("units", width=90, anchor="center", stretch=False)
        self.inv_lines_tree.column("base_price", width=100, anchor="e", stretch=False)
        self.inv_lines_tree.column("surcharge", width=130, anchor="e", stretch=False)
        self.inv_lines_tree.column("price", width=110, anchor="e", stretch=False)
        self.inv_lines_tree.column("pricing", width=95, anchor="center", stretch=False)
        self.inv_lines_tree.column("line_total", width=120, anchor="e", stretch=False)

        vsb = ttk.Scrollbar(lines_box, orient="vertical", command=self.inv_lines_tree.yview)
        self.inv_lines_tree.configure(yscrollcommand=vsb.set)
        self.inv_lines_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        line_buttons = ttk.Frame(right)
        line_buttons.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(line_buttons, text="Remove Selected Line", command=self.on_remove_invoice_line).pack(side="left")
        ttk.Button(line_buttons, text="Clear Lines", command=self.on_clear_invoice_lines).pack(side="left", padx=8)
        ttk.Button(
            line_buttons,
            text="Add Prior Invoice Charge",
            command=self.on_add_prior_invoice_charge
        ).pack(side="left", padx=8)

        adjustment_box = ttk.LabelFrame(
            right, text="Prior Invoice Charges", padding=8
        )
        adjustment_box.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        adjustment_box.columnconfigure(0, weight=1)

        self.inv_adjustments_tree = ttk.Treeview(
            adjustment_box,
            columns=("invoice", "item", "reason", "amount"),
            show="headings",
            height=4,
        )
        for col, title, width, anchor in [
            ("invoice", "Original Invoice", 120, "w"),
            ("item", "Item", 230, "w"),
            ("reason", "Reason", 260, "w"),
            ("amount", "Additional Charge", 130, "e"),
        ]:
            self.inv_adjustments_tree.heading(col, text=title)
            self.inv_adjustments_tree.column(col, width=width, anchor=anchor)
        self.inv_adjustments_tree.grid(row=0, column=0, sticky="ew")

        ttk.Button(
            adjustment_box,
            text="Remove Selected Charge",
            command=self.on_remove_prior_invoice_charge,
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

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

        combined_payment_row = ttk.Frame(right)
        combined_payment_row.grid(
            row=99, column=0, sticky="ew", pady=(10, 0)
        )
        ttk.Button(
            combined_payment_row,
            text="Combined Payment",
            style="Big.TButton",
            command=self.on_combined_invoice_payment,
        ).pack(side="left")


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

    def _current_invoice_customer_id(self):
        customer = self.db.get_customer_by_store_name(self.inv_store_name.get())
        return customer[0] if customer else None

    def on_add_prior_invoice_charge(self):
        customer = self.db.get_customer_by_store_name(
            self.inv_store_name.get().strip()
        )
        if not customer:
            messagebox.showerror(
                "Customer Required",
                "Select a saved customer before adding a prior-invoice charge."
            )
            return

        rows = self.db.list_closed_invoice_lines_for_customer(customer[0])
        if not rows:
            messagebox.showinfo(
                "No Closed Invoices",
                "This customer has no closed invoice item lines available."
            )
            return

        win = tk.Toplevel(self)
        win.title("Add Prior Invoice Charge")
        win.transient(self)
        win.grab_set()
        win.geometry("900x560")
        win.minsize(720, 440)

        if hasattr(self, "_make_scrollable_popup_content"):
            content = self._make_scrollable_popup_content(win, padding=12)
        else:
            content = ttk.Frame(win, padding=12)
            content.pack(fill="both", expand=True)

        ttk.Label(
            content,
            text="Add Charge From a Previously Closed Invoice",
            style="Title.TLabel"
        ).pack(anchor="w")
        ttk.Label(
            content,
            text=(
                "Select the original item, then enter only the additional amount "
                "still owed. This does not remove inventory again."
            ),
            foreground="#666",
            wraplength=820
        ).pack(anchor="w", pady=(4, 10))

        box = ttk.LabelFrame(content, text="Closed Invoice Items", padding=8)
        box.pack(fill="both", expand=True)

        cols = ("invoice", "date", "item", "qty", "type", "charged", "line_total")
        tree = ttk.Treeview(box, columns=cols, show="headings", height=11)
        for col, title, width, anchor in [
            ("invoice", "Invoice", 120, "w"),
            ("date", "Date", 130, "w"),
            ("item", "Item", 260, "w"),
            ("qty", "Qty", 60, "center"),
            ("type", "Type", 70, "center"),
            ("charged", "Charged", 100, "e"),
            ("line_total", "Line Total", 110, "e"),
        ]:
            tree.heading(col, text=title)
            tree.column(col, width=width, anchor=anchor)

        row_map = {}
        for idx, row in enumerate(rows):
            source_invoice_id, source_ref, created_at, source_line_id, item_name, qty, sale_type, unit_price, line_total = row
            iid = f"prior_{idx}"
            row_map[iid] = row
            tree.insert(
                "", "end", iid=iid,
                values=(
                    source_ref, str(created_at)[:10], item_name, qty,
                    sale_type, money(unit_price), money(line_total)
                )
            )

        scroll = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        form = ttk.LabelFrame(content, text="Additional Charge", padding=10)
        form.pack(fill="x", pady=(10, 0))
        form.columnconfigure(1, weight=1)

        amount_var = tk.StringVar()
        reason_var = tk.StringVar(value="Price correction")

        ttk.Label(form, text="Additional amount:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=amount_var, width=14).grid(row=0, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(form, text="Reason:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=reason_var).grid(row=1, column=1, sticky="ew", padx=8, pady=4)

        def add_charge():
            try:
                selected = tree.selection()
                if not selected:
                    raise ValueError("Select an item from a closed invoice.")

                row = row_map[selected[0]]
                source_invoice_id, source_ref, created_at, source_line_id, item_name, qty, sale_type, unit_price, line_total = row

                amount = float(amount_var.get())
                if amount <= 0:
                    raise ValueError("Additional amount must be greater than zero.")

                reason = reason_var.get().strip() or "Price correction"
                self.invoice_adjustments.append((
                    int(source_invoice_id),
                    int(source_line_id),
                    str(source_ref),
                    str(item_name),
                    amount,
                    reason,
                ))
                self.refresh_invoice_adjustments()
                win.destroy()
            except Exception as exc:
                messagebox.showerror("Error", str(exc), parent=win)

        buttons = ttk.Frame(content)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="Add Charge", style="Big.TButton", command=add_charge).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right", padx=8)

    def on_remove_prior_invoice_charge(self):
        selected = self.inv_adjustments_tree.selection()
        if not selected:
            messagebox.showerror("Error", "Select an additional charge to remove.")
            return
        index = self.inv_adjustments_tree.index(selected[0])
        del self.invoice_adjustments[index]
        self.refresh_invoice_adjustments()

    def refresh_invoice_adjustments(self):
        if not hasattr(self, "inv_adjustments_tree"):
            return
        for row in self.inv_adjustments_tree.get_children():
            self.inv_adjustments_tree.delete(row)

        for adjustment in self.invoice_adjustments:
            source_invoice_id, source_line_id, source_ref, item_name, amount, reason = adjustment
            self.inv_adjustments_tree.insert(
                "", "end",
                values=(source_ref, item_name, reason, money(amount))
            )
        self._refresh_invoice_total_labels()

    def on_add_invoice_line(self):
        try:
            item_name=self.inv_item.get(); item_id=self._selected_item_id(item_name); qty=int(self.inv_qty.get()); sale_type=self.inv_sale_type.get()
            if qty<=0: raise ValueError("Quantity must be > 0.")
            customer=self.db.get_customer_by_store_name(self.inv_store_name.get().strip()); customer_id=customer[0] if customer else None
            base_price,surcharge_amount,charged_price,pricing_type,surcharge_reason = self.db.get_invoice_price_details(customer_id,item_id,sale_type)
            item=self.db.get_item_details(item_id); upb=item[4]; unit_qty=qty*(upb if sale_type=='box' else 1)
            self.invoice_lines.append((item_id,item_name,qty,sale_type,unit_qty,upb,base_price,surcharge_amount,charged_price,pricing_type,surcharge_reason)); self.inv_qty.set(""); self.refresh_invoice_lines()
        except Exception as e: messagebox.showerror("Error",str(e))

    def _invoice_totals(self):
        subtotal=sum(price*qty for _id,_name,qty,_type,_units,_upb,_base,_surcharge,price,_pricing,_reason in self.invoice_lines)
        adjustment_total=sum(float(row[4]) for row in self.invoice_adjustments)
        subtotal += adjustment_total
        discount_percent=float(self.inv_discount.get() or 0); credit=float(self.inv_credit.get() or 0)
        if discount_percent<0 or credit<0: raise ValueError("Discount and credit must be >= 0.")
        discount_amount=subtotal*(discount_percent/100); return subtotal,discount_percent,discount_amount,credit,max(0,subtotal-discount_amount-credit)

    def on_combined_invoice_payment(self):
        customer_name = self.inv_store_name.get().strip()
        customer = self.db.get_customer_by_store_name(customer_name)

        if not customer:
            messagebox.showerror(
                "Customer Required",
                "Select a saved customer before combining invoice payments."
            )
            return

        rows = self.db.list_open_invoices_for_customer(customer[0])
        if len(rows) < 2:
            messagebox.showinfo(
                "Combined Payment",
                "This customer needs at least two open invoices to use combined payment."
            )
            return

        win = tk.Toplevel(self)
        win.title(f"Combined Payment — {customer_name}")
        win.transient(self)
        win.grab_set()
        win.geometry("820x560")
        win.minsize(680, 440)

        if hasattr(self, "_make_scrollable_popup_content"):
            content = self._make_scrollable_popup_content(win, padding=12)
        else:
            content = ttk.Frame(win, padding=12)
            content.pack(fill="both", expand=True)

        ttk.Label(
            content,
            text=f"Combined Payment — {customer_name}",
            style="Title.TLabel"
        ).pack(anchor="w")
        ttk.Label(
            content,
            text=(
                "Select the open invoices being paid together. "
                "Each invoice remains a separate record and reference number."
            ),
            foreground="#666",
            wraplength=760,
        ).pack(anchor="w", pady=(4, 10))

        box = ttk.LabelFrame(content, text="Open Invoices", padding=8)
        box.pack(fill="both", expand=True)

        tree = ttk.Treeview(
            box,
            columns=("selected", "ref", "date", "total"),
            show="headings",
            height=12,
        )
        for col, title, width, anchor in [
            ("selected", "Pay", 60, "center"),
            ("ref", "Invoice", 170, "w"),
            ("date", "Date", 180, "w"),
            ("total", "Balance", 130, "e"),
        ]:
            tree.heading(col, text=title)
            tree.column(col, width=width, anchor=anchor)

        selected_ids = set()
        row_map = {}

        for idx, (invoice_id, ref, created_at, total, store) in enumerate(rows):
            iid = f"pay_{idx}"
            row_map[iid] = (int(invoice_id), str(ref), float(total or 0))
            tree.insert("", "end", iid=iid, values=("☐", ref, created_at, money(total)))

        scroll = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        total_var = tk.StringVar(value="Selected total: $0.00")
        ttk.Label(content, textvariable=total_var, style="Title.TLabel").pack(
            anchor="e", pady=(10, 6)
        )

        payment_var = tk.StringVar(value="cash")
        payment_row = ttk.Frame(content)
        payment_row.pack(fill="x", pady=(0, 10))
        ttk.Label(payment_row, text="Payment Method:").pack(side="left")
        ttk.Combobox(
            payment_row,
            textvariable=payment_var,
            state="readonly",
            values=("cash", "check", "card", "ach", "other"),
            width=16,
        ).pack(side="left", padx=8)

        def refresh_selected_total():
            total = sum(row_map[iid][2] for iid in selected_ids)
            total_var.set(
                f"Selected invoices: {len(selected_ids)}    "
                f"Combined total: {money(total)}"
            )

        def toggle_selected(event=None):
            iid = tree.identify_row(event.y) if event is not None else None
            if not iid:
                current = tree.selection()
                iid = current[0] if current else None
            if not iid or iid not in row_map:
                return

            values = list(tree.item(iid, "values"))
            if iid in selected_ids:
                selected_ids.remove(iid)
                values[0] = "☐"
            else:
                selected_ids.add(iid)
                values[0] = "☑"
            tree.item(iid, values=values)
            refresh_selected_total()

        tree.bind("<Double-1>", toggle_selected)

        controls = ttk.Frame(content)
        controls.pack(fill="x", pady=(4, 0))

        def select_all():
            selected_ids.clear()
            selected_ids.update(row_map.keys())
            for iid in row_map:
                values = list(tree.item(iid, "values"))
                values[0] = "☑"
                tree.item(iid, values=values)
            refresh_selected_total()

        def clear_all():
            selected_ids.clear()
            for iid in row_map:
                values = list(tree.item(iid, "values"))
                values[0] = "☐"
                tree.item(iid, values=values)
            refresh_selected_total()

        ttk.Button(controls, text="Select All", command=select_all).pack(side="left")
        ttk.Button(controls, text="Clear Selection", command=clear_all).pack(side="left", padx=8)

        def complete_payment():
            try:
                if len(selected_ids) < 2:
                    raise ValueError("Select at least two invoices for a combined payment.")

                invoice_ids = [row_map[iid][0] for iid in selected_ids]
                payment_method = payment_var.get().strip() or "other"
                selected_total = sum(row_map[iid][2] for iid in selected_ids)
                refs = ", ".join(row_map[iid][1] for iid in selected_ids)

                if not messagebox.askyesno(
                    "Complete Combined Payment",
                    f"Close {len(invoice_ids)} invoices with one payment?\n\n"
                    f"Invoices: {refs}\n"
                    f"Combined total: {money(selected_total)}\n"
                    f"Payment method: {payment_method}",
                    parent=win,
                ):
                    return

                result = self.db.close_multiple_invoices(invoice_ids, payment_method)
                self.refresh_all()
                win.destroy()

                messagebox.showinfo(
                    "Combined Payment Complete",
                    f"{result['invoice_count']} invoices were closed.\n\n"
                    f"Combined payment: {money(result['total'])}\n"
                    f"Payment method: {payment_method}\n\n"
                    "Each invoice remains separate in History.",
                )
            except Exception as exc:
                messagebox.showerror("Error", str(exc), parent=win)

        ttk.Button(
            controls,
            text="Complete Combined Payment",
            style="Big.TButton",
            command=complete_payment,
        ).pack(side="right")
        ttk.Button(controls, text="Cancel", command=win.destroy).pack(side="right", padx=8)

    def on_save_invoice(self):
        try:
            ref = self.inv_ref.get().strip()

            if not ref:
                raise ValueError("Invoice number is required.")

            if not self.invoice_lines and not self.invoice_adjustments:
                raise ValueError("Add at least one item or prior-invoice charge.")

            # Pre-check the full invoice so duplicate item lines cannot accidentally
            # subtract more stock than is available.
            totals = {}
            names = {}
            for item_id, item_name, qty, sale_type, unit_qty, upb, base_price, surcharge_amount, unit_price, pricing_type, surcharge_reason in self.invoice_lines:
                totals[item_id] = totals.get(item_id, 0) + unit_qty
                names[item_id] = item_name

            for item_id, total_qty in totals.items():
                item = self.db.get_item_details(item_id)
                if not item:
                    raise ValueError(f"Item not found: {names[item_id]}")
                current_stock = item[6]
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

            is_internal_use = self.db.is_internal_use_customer(customer_id)
            if is_internal_use:
                printable_lines = [
                    (
                        item_id, item_name, qty, sale_type, unit_qty, upb,
                        0.0, 0.0, 0.0, "Internal Use", ""
                    )
                    for (
                        item_id, item_name, qty, sale_type, unit_qty, upb,
                        base_price, surcharge_amount, unit_price,
                        pricing_type, surcharge_reason
                    ) in printable_lines
                ]
                self.invoice_lines = list(printable_lines)
                subtotal = 0.0
                discount_percent = 0.0
                discount_amount = 0.0
                credit = 0.0
                total = 0.0
                if notes.strip():
                    notes += "\n"
                notes += "INTERNAL USE - inventory withdrawal, no customer charge."

            available_credit = self.db.get_customer_credit(customer_id)
            if credit > available_credit + 0.005:
                raise ValueError(
                    f"Account credit entered is {money(credit)}, but {store_name} only has {money(available_credit)} available."
                )

            for item_id, item_name, qty, sale_type, unit_qty, upb, base_price, surcharge_amount, unit_price, pricing_type, surcharge_reason in self.invoice_lines:
                self.db.record_invoice(ref, item_id, unit_qty, notes)

            printable_path = self.create_printable_invoice(
                ref, printable_lines, self.inv_store_name.get(), self.inv_address.get(),
                self.inv_phone.get(), discount_percent, discount_amount, credit, subtotal, total, notes, self.invoice_adjustments
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

            for item_id, item_name, qty, sale_type, unit_qty, upb, base_price, surcharge_amount, unit_price, pricing_type, surcharge_reason in printable_lines:
                self.db.save_invoice_line(invoice_id, item_id, item_name, qty, sale_type, unit_qty, upb, base_price, surcharge_amount, unit_price, surcharge_reason)

            for (
                source_invoice_id, source_line_id, source_ref,
                adjustment_item_name, adjustment_amount, adjustment_reason
            ) in self.invoice_adjustments:
                self.db.save_invoice_adjustment(
                    invoice_id, source_invoice_id, source_line_id, source_ref,
                    adjustment_item_name, adjustment_amount, adjustment_reason
                )

            if credit > 0:
                self.db.adjust_customer_credit(customer_id, -credit, "APPLIED", f"Invoice {ref}", "Account credit applied to invoice")
            if is_internal_use:
                self.db.mark_invoice_internal_use(invoice_id)

            self.invoice_lines.clear()
            self.invoice_adjustments.clear()
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

    def create_printable_invoice(self, ref, lines, store_name, address, phone, discount_percent, discount_amount, credit, subtotal, total, notes="", adjustments=None):
        invoice_dir = "printable_invoices"
        os.makedirs(invoice_dir, exist_ok=True)

        created_at = now_str()
        file_name = f"invoice_{safe_filename(ref)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        path = os.path.join(invoice_dir, file_name)

        rows_html = []
        for index, (item_id, item_name, qty, sale_type, unit_qty, upb, base_price, surcharge_amount, price, _pricing_type, _surcharge_reason) in enumerate(lines, start=1):
            line_total = price * qty
            rows_html.append(
                "<tr>"
                f"<td>{index}</td>"
                f"<td>{escape(item_name)}</td>"
                f"<td class='qty'>{qty} {escape(sale_type)}(s)</td>"
                f"<td class='money'>{money(base_price)}</td>"
                f"<td class='money'>{money(base_price * qty)}</td>"
                "</tr>"
            )
            if surcharge_amount > 0:
                rows_html.append(
                    "<tr>"
                    "<td></td>"
                    f"<td>Temporary {escape(_surcharge_reason or 'product')} surcharge</td>"
                    f"<td class='qty'>{qty}</td>"
                    f"<td class='money'>{money(surcharge_amount)}</td>"
                    f"<td class='money'>{money(surcharge_amount * qty)}</td>"
                    "</tr>"
                )

        adjustments = adjustments or []
        for source_invoice_id, source_line_id, source_ref, item_name, amount, reason in adjustments:
            rows_html.append(
                "<tr>"
                "<td></td>"
                f"<td><strong>Prior Invoice Charge</strong><br>{escape(item_name)} — Invoice {escape(str(source_ref))}"
                + (f"<br><span class='muted'>{escape(reason)}</span>" if reason else "")
                + "</td>"
                "<td class='qty'>Adjustment</td>"
                f"<td class='money'>{money(amount)}</td>"
                f"<td class='money'>{money(amount)}</td>"
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
            for _item_id, _item_name, qty, _sale_type, _unit_qty, _upb, _base_price, _surcharge, unit_price, _pricing_type, _reason in self.invoice_lines:
                subtotal += unit_price * qty
            discount_amount = 0.0
            credit = 0.0
            total = subtotal

        self.inv_subtotal_label.config(text=f"Subtotal: {money(subtotal)}")
        self.inv_discount_label.config(text=f"Discount: {money(discount_amount)}")
        self.inv_credit_label.config(text=f"Credit: {money(credit)}")
        self.inv_total_label.config(text=f"Total: {money(total)}")

    def refresh_invoice_lines(self):
        for row in self.inv_lines_tree.get_children(): self.inv_lines_tree.delete(row)
        for _id,name,qty,sale_type,unit_qty,_upb,base_price,surcharge_amount,price,pricing,surcharge_reason in self.invoice_lines:
            surcharge_display = money(surcharge_amount)
            if surcharge_amount > 0 and surcharge_reason:
                surcharge_display += f" ({surcharge_reason})"
            self.inv_lines_tree.insert("","end",values=(
                name,qty,sale_type.title(),unit_qty,money(base_price),surcharge_display,
                money(price),pricing,money(price*qty)
            ))
        self._refresh_invoice_total_labels()

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

        pricing_box = ttk.LabelFrame(right, text="Special Pricing", padding=8)
        pricing_box.pack(fill="x", pady=(10, 0))
        pricing_box.columnconfigure(1, weight=1)
        self.special_price_item_var = tk.StringVar()
        self.special_box_price_var = tk.StringVar()
        self.special_unit_price_var = tk.StringVar()
        self.special_price_type_var = tk.StringVar(value="box")
        ttk.Label(pricing_box, text="Item:").grid(row=0, column=0, sticky="w", pady=4)
        self.special_price_item_cb = ttk.Combobox(
            pricing_box, textvariable=self.special_price_item_var
        )
        self.special_price_item_cb.grid(
            row=0, column=1, sticky="ew", padx=(8, 0), pady=4
        )
        self._configure_item_autocomplete(
            self.special_price_item_cb, self.special_price_item_var
        )
        ttk.Label(pricing_box, text="Special Box Price:").grid(row=1,column=0,sticky="w",pady=4)
        ttk.Entry(pricing_box,textvariable=self.special_box_price_var,width=14).grid(row=1,column=1,sticky="w",padx=(8,0),pady=4)
        ttk.Label(pricing_box, text="Special Unit Price:").grid(row=2,column=0,sticky="w",pady=4)
        ttk.Entry(pricing_box,textvariable=self.special_unit_price_var,width=14).grid(row=2,column=1,sticky="w",padx=(8,0),pady=4)
        price_buttons = ttk.Frame(pricing_box)
        price_buttons.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 4))
        ttk.Button(price_buttons, text="Save Special Price", command=self.on_save_special_price).pack(side="left")
        ttk.Button(price_buttons, text="Remove Special Price", command=self.on_remove_special_price).pack(side="left", padx=8)
        sp_cols = ("item", "box_standard", "box_special", "unit_standard", "unit_special")
        self.special_prices_tree = ttk.Treeview(pricing_box, columns=sp_cols, show="headings", height=5)
        for c,title,width,anchor in [("item","Item",150,"w"),("box_standard","Box Std",80,"e"),("box_special","Box Special",85,"e"),("unit_standard","Unit Std",80,"e"),("unit_special","Unit Special",85,"e")]:
            self.special_prices_tree.heading(c, text=title); self.special_prices_tree.column(c, width=width, anchor=anchor)
        self.special_prices_tree.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6,0))
        self.special_prices_tree.bind("<<TreeviewSelect>>", self.on_special_price_selected)

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
        for row in self.special_prices_tree.get_children():
            self.special_prices_tree.delete(row)
        self.special_box_price_var.set("")
        self.special_unit_price_var.set("")
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
        self.refresh_customer_special_prices(self.customer_selected_id)

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

    def refresh_special_price_item_choices(self):
        if not hasattr(self, "special_price_item_cb"):
            return
        names = [row[1] for row in self.db.list_items("")]
        self.special_price_item_cb["values"] = names
        if self.special_price_item_var.get() not in names:
            self.special_price_item_var.set(names[0] if names else "")

    def refresh_customer_special_prices(self, customer_id):
        if not hasattr(self,"special_prices_tree"): return
        for row in self.special_prices_tree.get_children(): self.special_prices_tree.delete(row)
        if customer_id is None: return
        for item_id,name,box_std,unit_std,box_sp,unit_sp,_updated in self.db.list_customer_special_prices(customer_id):
            self.special_prices_tree.insert("","end",iid=str(item_id),values=(name,money(box_std),money(box_sp),money(unit_std),money(unit_sp) if unit_sp is not None else "—"))

    def on_special_price_selected(self,event=None):
        selected=self.special_prices_tree.selection()
        if not selected:return
        v=self.special_prices_tree.item(selected[0],"values"); self.special_price_item_var.set(v[0]); self.special_box_price_var.set(v[2].replace("$","").replace(",","")); self.special_unit_price_var.set("" if v[4]=="—" else v[4].replace("$","").replace(",",""))

    def on_save_special_price(self):
        try:
            if self.customer_selected_id is None: raise ValueError("Select a customer first.")
            item_id=self._selected_item_id(self.special_price_item_var.get())
            if self.special_box_price_var.get().strip(): self.db.set_customer_special_price(self.customer_selected_id,item_id,"box",self.special_box_price_var.get())
            if self.special_unit_price_var.get().strip(): self.db.set_customer_special_price(self.customer_selected_id,item_id,"unit",self.special_unit_price_var.get())
            self.refresh_customer_special_prices(self.customer_selected_id); messagebox.showinfo("Saved","Customer-specific box/unit pricing saved.")
        except Exception as e: messagebox.showerror("Error",str(e))

    def on_remove_special_price(self):
        try:
            if self.customer_selected_id is None:
                raise ValueError("Select a customer first.")

            item_name = self.special_price_item_var.get().strip()
            item_id = self._selected_item_id(item_name)

            dialog = tk.Toplevel(self)
            dialog.title("Remove Special Price")
            dialog.transient(self)
            dialog.grab_set()
            dialog.resizable(False, False)

            result = {"choice": None}

            body = ttk.Frame(dialog, padding=18)
            body.pack(fill="both", expand=True)

            ttk.Label(
                body,
                text=f"Which special price would you like to remove for:\n{item_name}?",
                justify="center",
                wraplength=420,
            ).pack(pady=(0, 16))

            buttons = ttk.Frame(body)
            buttons.pack(fill="x")

            def choose(price_type):
                result["choice"] = price_type
                dialog.destroy()

            ttk.Button(
                buttons,
                text="Remove Box Price",
                command=lambda: choose("box"),
            ).pack(side="left", padx=(0, 8))

            ttk.Button(
                buttons,
                text="Remove Unit Price",
                command=lambda: choose("unit"),
            ).pack(side="left", padx=8)

            ttk.Button(
                buttons,
                text="Cancel",
                command=lambda: choose(None),
            ).pack(side="left", padx=(8, 0))

            dialog.protocol("WM_DELETE_WINDOW", lambda: choose(None))
            dialog.bind("<Escape>", lambda event: choose(None))

            dialog.update_idletasks()
            width = dialog.winfo_reqwidth()
            height = dialog.winfo_reqheight()
            x = self.winfo_rootx() + max(0, (self.winfo_width() - width) // 2)
            y = self.winfo_rooty() + max(0, (self.winfo_height() - height) // 2)
            dialog.geometry(f"+{x}+{y}")

            self.wait_window(dialog)

            if result["choice"] is None:
                return

            self.db.remove_customer_special_price(
                self.customer_selected_id,
                item_id,
                result["choice"],
            )
            self.refresh_customer_special_prices(self.customer_selected_id)

        except Exception as e:
            messagebox.showerror("Error", str(e))

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
        for line_id, item_id, item_name, sold_qty, refunded_qty, unit_price, sale_type, units_per_box in self.db.get_refundable_invoice_lines(invoice_id):
            available = sold_qty - refunded_qty
            self.refund_available_tree.insert("", "end", iid=str(line_id), values=(f"{item_name} ({sale_type})", sold_qty, refunded_qty, available, money(unit_price)))

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
        detail_content = self._make_scrollable_popup_content(win, padding=0)

        summary = ttk.LabelFrame(detail_content, text="Invoice Details", padding=12)
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

        lines_box = ttk.LabelFrame(detail_content, text="Invoice Lines", padding=12)
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
        lines_scroll = ttk.Scrollbar(
            lines_box, orient="vertical", command=lines_tree.yview
        )
        lines_tree.configure(yscrollcommand=lines_scroll.set)
        lines_tree.pack(side="left", fill="both", expand=True)
        lines_scroll.pack(side="right", fill="y")

        for item_name, qty, sale_type, base_price, surcharge_amount, surcharge_reason, unit_price, line_total in self.db.get_invoice_lines(invoice_id):
            price_text = money(unit_price)
            if surcharge_amount > 0:
                price_text += f" (includes {money(surcharge_amount)} surcharge)"
            lines_tree.insert("", "end", values=(item_name, f"{qty} {sale_type}(s)", price_text, money(line_total)))

        totals_box = ttk.LabelFrame(detail_content, text="Totals / Payment", padding=12)
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

        note_box = ttk.LabelFrame(detail_content, text="Notes", padding=12)
        note_box.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Label(note_box, text=notes if notes else "No notes entered.", wraplength=760).pack(anchor="w")

        # Dock detail-window buttons to the bottom so payment/void actions stay visible.
        saved_adjustments = self.db.get_invoice_adjustments(invoice_id)
        if saved_adjustments:
            adj_parent = detail_content if "detail_content" in locals() else win
            adj_box = ttk.LabelFrame(
                adj_parent, text="Prior Invoice Charges", padding=12
            )
            adj_box.pack(fill="x", padx=12, pady=(0, 8))

            for (
                adjustment_id, source_invoice_id, source_line_id,
                source_ref, item_name, amount, reason, adjustment_created
            ) in saved_adjustments:
                text_value = f"{item_name} — from invoice {source_ref}: {money(amount)}"
                if reason:
                    text_value += f" ({reason})"
                ttk.Label(adj_box, text=text_value, wraplength=820).pack(
                    anchor="w", pady=2
                )

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
                messagebox.showerror(
                    "Error",
                    "Closed invoices cannot be voided.",
                    parent=win,
                )
                return

            if status == "void":
                messagebox.showinfo(
                    "Already Voided",
                    "This invoice has already been voided.",
                    parent=win,
                )
                return

            if not messagebox.askyesno(
                "Void Open Invoice",
                "Void this open invoice?\n\n"
                "This will return all items on the invoice to inventory. "
                "Any customer account credit still applied to the invoice "
                "will also be restored.\n\n"
                "This action cannot be undone from this screen.",
                parent=win,
            ):
                return

            try:
                result = self.db.void_invoice(invoice_id)
                self.refresh_all()

                message = (
                    "Invoice marked as void.\n\n"
                    f"Inventory restored: {result['restored_units']} unit(s)."
                )
                if result["restored_credit"] > 0:
                    message += (
                        "\nCustomer credit restored: "
                        f"{money(result['restored_credit'])}."
                    )

                messagebox.showinfo("Invoice Voided", message, parent=win)
                win.destroy()

            except Exception as e:
                messagebox.showerror("Error", str(e), parent=win)

        def open_printable():
            if printable_path and os.path.exists(printable_path):
                webbrowser.open(f"file://{os.path.abspath(printable_path)}")
            else:
                messagebox.showerror("Error", "Printable invoice file was not found.")

        ttk.Button(buttons, text="Save Payment / Close Invoice", style="Big.TButton", command=save_payment).pack(side="left")
        if status == "open":
            ttk.Button(
                buttons, text="Edit Open Invoice", style="Big.TButton",
                command=lambda: (win.destroy(), self.open_invoice_editor(invoice_id)),
            ).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Open Printable Invoice", style="Big.TButton", command=open_printable).pack(side="left", padx=8)
        ttk.Button(buttons, text="Void Invoice", style="Big.TButton", command=void_invoice).pack(side="left", padx=8)
        ttk.Button(buttons, text="Close Window", style="Big.TButton", command=win.destroy).pack(side="right")

    def open_invoice_editor(self, invoice_id):
        invoice = self.db.get_invoice(invoice_id)
        if not invoice:
            messagebox.showerror("Error", "Invoice not found.")
            return

        (
            _id, ref, store, address, phone, discount_percent, credit, notes,
            subtotal, discount_amount, total, status, payment_method,
            printable_path, created_at, paid_at
        ) = invoice

        if status != "open":
            messagebox.showerror("Error", "Only open invoices can be edited.")
            return

        customer_id = self.db.get_invoice_customer_id(invoice_id)
        edit_lines = []
        for (
            line_id, item_id, item_name, qty, sale_type, unit_qty,
            units_per_box, base_price, surcharge_amount, surcharge_reason,
            unit_price, line_total
        ) in self.db.get_invoice_lines_for_edit(invoice_id):
            edit_lines.append((
                item_id, item_name, qty, sale_type, unit_qty, units_per_box,
                base_price, surcharge_amount, unit_price, "saved", surcharge_reason
            ))

        win = tk.Toplevel(self)
        win.title(f"Edit Open Invoice {ref}")
        win.transient(self)
        win.grab_set()
        win.geometry("1000x650")
        win.minsize(760, 480)

        content = self._make_scrollable_popup_content(win, padding=12)

        ttk.Label(
            content, text=f"Edit Open Invoice #{ref} — {store}", style="Title.TLabel"
        ).pack(anchor="w", pady=(0, 6))
        ttk.Label(
            content,
            text="Add, replace, modify, or remove invoice items. Inventory is adjusted only by the difference.",
            foreground="#666", wraplength=900
        ).pack(anchor="w", pady=(0, 12))

        form = ttk.LabelFrame(content, text="Add / Modify Item", padding=12)
        form.pack(fill="x", pady=(0, 10))
        form.columnconfigure(1, weight=1)

        item_var = tk.StringVar()
        qty_var = tk.StringVar()
        sale_type_var = tk.StringVar(value="box")
        selected_index = {"value": None}

        ttk.Label(form, text="Item:").grid(row=0, column=0, sticky="w")
        item_cb = ttk.Combobox(
            form, textvariable=item_var,
            values=[row[1] for row in self.db.list_items("")]
        )
        item_cb.grid(row=0, column=1, sticky="ew", padx=8)
        if hasattr(self, "_configure_item_autocomplete"):
            self._configure_item_autocomplete(item_cb, item_var)

        ttk.Label(form, text="Qty:").grid(row=0, column=2, sticky="w")
        ttk.Entry(form, textvariable=qty_var, width=10).grid(row=0, column=3, padx=8)
        ttk.Combobox(
            form, textvariable=sale_type_var, state="readonly",
            values=("box", "unit"), width=9
        ).grid(row=0, column=4, padx=(0, 8))

        table_box = ttk.LabelFrame(content, text="Invoice Items", padding=10)
        table_box.pack(fill="both", expand=True, pady=(0, 10))

        columns = ("item", "qty", "type", "units", "price", "total")
        tree = ttk.Treeview(table_box, columns=columns, show="headings", height=13)
        for col, title, width, anchor in [
            ("item", "Item", 360, "w"),
            ("qty", "Qty", 80, "center"),
            ("type", "Sold As", 90, "center"),
            ("units", "Stock Units", 100, "center"),
            ("price", "Charged Price", 120, "e"),
            ("total", "Line Total", 120, "e"),
        ]:
            tree.heading(col, text=title)
            tree.column(col, width=width, anchor=anchor)

        scroll = ttk.Scrollbar(table_box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        totals_var = tk.StringVar()

        def refresh_editor():
            for row in tree.get_children():
                tree.delete(row)
            running = 0.0
            for idx, line in enumerate(edit_lines):
                item_id, item_name, qty, sale_type, unit_qty, upb, base_price, surcharge_amount, charged_price, pricing_type, surcharge_reason = line
                line_total = float(charged_price) * int(qty)
                running += line_total
                tree.insert(
                    "", "end", iid=str(idx),
                    values=(item_name, qty, sale_type, unit_qty, money(charged_price), money(line_total))
                )
            discount_value = running * (float(discount_percent or 0) / 100.0)
            usable_credit = min(float(credit or 0), max(0.0, running - discount_value))
            final_total = max(0.0, running - discount_value - usable_credit)
            totals_var.set(
                f"Subtotal: {money(running)}    Discount: {money(discount_value)}    "
                f"Credit: {money(usable_credit)}    Total: {money(final_total)}"
            )

        def clear_form():
            selected_index["value"] = None
            item_var.set("")
            qty_var.set("")
            sale_type_var.set("box")
            for row in tree.selection():
                tree.selection_remove(row)

        def load_selected(event=None):
            selected = tree.selection()
            if not selected:
                return
            idx = int(selected[0])
            selected_index["value"] = idx
            line = edit_lines[idx]
            item_var.set(line[1])
            qty_var.set(str(line[2]))
            sale_type_var.set(line[3])

        def add_or_update():
            try:
                item_name = item_var.get().strip()
                item_id = self._selected_item_id(item_name)
                qty = int(qty_var.get())
                sale_type = sale_type_var.get()
                if qty <= 0:
                    raise ValueError("Quantity must be greater than zero.")

                base_price, surcharge_amount, charged_price, pricing_type, surcharge_reason = self.db.get_invoice_price_details(
                    customer_id, item_id, sale_type
                )
                item = self.db.get_item_details(item_id)
                upb = int(item[4])
                unit_qty = qty * (upb if sale_type == "box" else 1)

                new_line = (
                    item_id, item_name, qty, sale_type, unit_qty, upb,
                    base_price, surcharge_amount, charged_price, pricing_type, surcharge_reason
                )

                idx = selected_index["value"]
                if idx is None:
                    edit_lines.append(new_line)
                else:
                    edit_lines[idx] = new_line

                clear_form()
                refresh_editor()
            except Exception as exc:
                messagebox.showerror("Error", str(exc), parent=win)

        def remove_selected():
            selected = tree.selection()
            if not selected:
                messagebox.showerror("Error", "Select an invoice item first.", parent=win)
                return
            del edit_lines[int(selected[0])]
            clear_form()
            refresh_editor()

        def save_changes():
            try:
                if not edit_lines:
                    raise ValueError(
                        "An invoice must contain at least one item. Use Void Invoice if the order was cancelled."
                    )
                if not messagebox.askyesno(
                    "Save Invoice Changes",
                    "Save these item changes and adjust inventory?",
                    parent=win
                ):
                    return

                new_subtotal, new_discount_amount, new_credit, new_total = self.db.update_open_invoice_lines(
                    invoice_id, edit_lines
                )

                new_printable = self.create_printable_invoice(
                    ref, list(edit_lines), store, address, phone,
                    discount_percent, new_discount_amount, new_credit,
                    new_subtotal, new_total, notes
                )
                self.db.update_invoice_printable_path(invoice_id, new_printable)

                self.refresh_all()
                win.destroy()
                messagebox.showinfo(
                    "Invoice Updated",
                    "The open invoice and inventory were updated successfully."
                )
                self.open_transaction_detail(invoice_id)
            except Exception as exc:
                messagebox.showerror("Error", str(exc), parent=win)

        tree.bind("<<TreeviewSelect>>", load_selected)
        tree.bind("<Double-1>", load_selected)

        form_buttons = ttk.Frame(form)
        form_buttons.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(10, 0))
        ttk.Button(
            form_buttons, text="Add / Update Line",
            style="Big.TButton", command=add_or_update
        ).pack(side="left")
        ttk.Button(
            form_buttons, text="Clear Selection", command=clear_form
        ).pack(side="left", padx=8)

        actions = ttk.Frame(content)
        actions.pack(fill="x")
        ttk.Button(
            actions, text="Remove Selected Item", command=remove_selected
        ).pack(side="left")
        ttk.Label(actions, textvariable=totals_var, style="Title.TLabel").pack(
            side="left", padx=18
        )
        ttk.Button(
            actions, text="Save Invoice Changes",
            style="Big.TButton", command=save_changes
        ).pack(side="right")
        ttk.Button(actions, text="Cancel", command=win.destroy).pack(side="right", padx=8)

        refresh_editor()

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
                _subtotal, _discount, _credit, _surcharge, total_sales, paid_total, open_total, _void_total,
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
        printable_path = statement[16]
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
        <tr><th>Temporary surcharges</th><td class="money">{money(data['surcharge_amount'])}</td></tr>
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
        <tr><th>Temporary surcharges</th><td class="money">{money(data['surcharge_amount'])}</td></tr>
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
        self.history_search_var = tk.StringVar()
        self.history_type_var = tk.StringVar(value="All")
        self.history_status_var = tk.StringVar(value="All")
        self.history_from_var = tk.StringVar()
        self.history_to_var = tk.StringVar()
        self.history_record_map = {}

        filters = ttk.LabelFrame(self.tab_history, text="History Filters", padding=10)
        filters.pack(fill="x", pady=(0, 10))
        filters.columnconfigure(1, weight=1)

        ttk.Label(filters, text="Search:").grid(row=0, column=0, sticky="w")
        search_entry = ttk.Entry(filters, textvariable=self.history_search_var)
        search_entry.grid(row=0, column=1, sticky="ew", padx=(6, 12))
        search_entry.bind("<Return>", lambda event: self.refresh_history())

        ttk.Label(filters, text="Type:").grid(row=0, column=2, sticky="w")
        ttk.Combobox(
            filters,
            textvariable=self.history_type_var,
            state="readonly",
            width=16,
            values=("All", "Shipment", "Invoice", "Internal Use", "Refund"),
        ).grid(row=0, column=3, padx=(6, 12))

        ttk.Label(filters, text="Status:").grid(row=0, column=4, sticky="w")
        ttk.Combobox(
            filters,
            textvariable=self.history_status_var,
            state="readonly",
            width=14,
            values=("All", "Open", "Closed", "Void", "Completed", "Corrected"),
        ).grid(row=0, column=5, padx=(6, 0))

        ttk.Label(filters, text="From:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(
            filters, textvariable=self.history_from_var, width=14
        ).grid(row=1, column=1, sticky="w", padx=(6, 12), pady=(8, 0))

        ttk.Label(filters, text="To:").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(
            filters, textvariable=self.history_to_var, width=14
        ).grid(row=1, column=3, sticky="w", padx=(6, 12), pady=(8, 0))

        ttk.Label(
            filters, text="YYYY-MM-DD", foreground="#666"
        ).grid(row=1, column=4, sticky="w", pady=(8, 0))

        actions = ttk.Frame(self.tab_history)
        actions.pack(fill="x", pady=(0, 10))
        ttk.Button(
            actions, text="Apply Filters", style="Big.TButton",
            command=self.refresh_history
        ).pack(side="left")
        ttk.Button(
            actions, text="Clear Filters", command=self.clear_history_filters
        ).pack(side="left", padx=8)
        ttk.Button(
            actions, text="Open Selected", style="Big.TButton",
            command=self.on_open_history_record
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            actions, text="View Raw Inventory Log",
            command=self.open_raw_inventory_log
        ).pack(side="left", padx=8)
        ttk.Button(
            actions, text="Export History CSV",
            command=lambda: self.export_treeview_csv(self.hist, "history")
        ).pack(side="left", padx=8)

        ttk.Label(
            actions,
            text="One row per invoice, shipment, internal-use withdrawal, or refund.",
            foreground="#666",
        ).pack(side="left", padx=12)

        table_frame = ttk.Frame(self.tab_history)
        table_frame.pack(fill="both", expand=True)

        cols = ("time", "type", "ref", "party", "summary", "status")
        self.hist = ttk.Treeview(
            table_frame, columns=cols, show="headings", height=16
        )
        for col, title, width, anchor in [
            ("time", "Date / Time", 160, "w"),
            ("type", "Type", 110, "center"),
            ("ref", "Reference", 150, "w"),
            ("party", "Customer / Source", 230, "w"),
            ("summary", "Summary", 300, "w"),
            ("status", "Status", 110, "center"),
        ]:
            self.hist.heading(col, text=title)
            self.hist.column(col, width=width, anchor=anchor)

        vsb = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.hist.yview
        )
        self.hist.configure(yscrollcommand=vsb.set)
        self.hist.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.hist.bind("<Double-1>", self.on_open_history_record)

    def clear_history_filters(self):
        self.history_search_var.set("")
        self.history_type_var.set("All")
        self.history_status_var.set("All")
        self.history_from_var.set("")
        self.history_to_var.set("")
        self.refresh_history()

    def refresh_history(self):
        for row in self.hist.get_children():
            self.hist.delete(row)
        self.history_record_map = {}

        search = self.history_search_var.get().strip().casefold()
        wanted_type = self.history_type_var.get().strip()
        wanted_status = self.history_status_var.get().strip()
        from_text = self.history_from_var.get().strip()
        to_text = self.history_to_var.get().strip()

        try:
            from_date = (
                datetime.strptime(from_text, "%Y-%m-%d").date()
                if from_text else None
            )
            to_date = (
                datetime.strptime(to_text, "%Y-%m-%d").date()
                if to_text else None
            )
            if from_date and to_date and from_date > to_date:
                raise ValueError("From date cannot be later than To date.")
        except ValueError as exc:
            messagebox.showerror(
                "Invalid History Date",
                "Use YYYY-MM-DD for History dates.\n\n" + str(exc),
            )
            return

        displayed = 0
        for key, created_at, typ, ref, party, summary, status in self.db.list_document_history(1000):
            if wanted_type != "All" and typ != wanted_type:
                continue
            if wanted_status != "All" and status != wanted_status:
                continue

            if search:
                haystack = " ".join(
                    str(value or "")
                    for value in (created_at, typ, ref, party, summary, status)
                ).casefold()
                if search not in haystack:
                    continue

            if from_date or to_date:
                try:
                    row_date = datetime.strptime(
                        str(created_at)[:10], "%Y-%m-%d"
                    ).date()
                except ValueError:
                    row_date = None
                if row_date:
                    if from_date and row_date < from_date:
                        continue
                    if to_date and row_date > to_date:
                        continue

            iid = f"history_{displayed}"
            self.history_record_map[iid] = key
            self.hist.insert(
                "", "end", iid=iid,
                values=(created_at, typ, ref, party, summary, status)
            )
            displayed += 1
            if displayed >= 500:
                break

    def on_open_history_record(self, event=None):
        selected = self.hist.selection()
        if not selected:
            if event is None:
                messagebox.showerror("Error", "Select a history record first.")
            return

        key = self.history_record_map.get(selected[0], "")
        if not key:
            return

        kind, value = key.split(":", 1)

        if kind == "invoice":
            self.open_transaction_detail(int(value))
        elif kind == "shipment":
            if hasattr(self, "open_shipment_detail"):
                self.open_shipment_detail(value)
            else:
                messagebox.showinfo(
                    "Shipment",
                    "Detailed shipment viewing is not available in this build."
                )
        elif kind == "refund":
            self.open_refund_history_detail(int(value))

    def open_refund_history_detail(self, refund_id):
        header, lines = self.db.get_refund_history_detail(refund_id)
        if not header:
            messagebox.showerror("Error", "Refund record not found.")
            return

        (
            _refund_id, created_at, refund_method, total, note,
            invoice_id, invoice_ref, store
        ) = header

        win = tk.Toplevel(self)
        win.title(f"Refund #{refund_id}")
        win.transient(self)
        win.geometry("760x500")
        win.minsize(620, 380)

        content = ttk.Frame(win, padding=12)
        content.pack(fill="both", expand=True)

        ttk.Label(
            content, text=f"Refund #{refund_id}", style="Title.TLabel"
        ).pack(anchor="w")
        ttk.Label(
            content,
            text=(
                f"Date: {created_at}    Customer: {store or '—'}    "
                f"Invoice: {invoice_ref or '—'}"
            ),
        ).pack(anchor="w", pady=(4, 2))
        ttk.Label(
            content,
            text=f"Method: {refund_method}    Total: {money(total)}",
        ).pack(anchor="w", pady=(0, 8))
        if note:
            ttk.Label(
                content, text=f"Note: {note}", wraplength=700
            ).pack(anchor="w", pady=(0, 8))

        box = ttk.LabelFrame(content, text="Returned Items", padding=8)
        box.pack(fill="both", expand=True)

        tree = ttk.Treeview(
            box,
            columns=("item", "qty", "price", "total"),
            show="headings",
            height=10,
        )
        for col, title, width, anchor in [
            ("item", "Item", 360, "w"),
            ("qty", "Qty", 80, "center"),
            ("price", "Unit Price", 110, "e"),
            ("total", "Line Total", 110, "e"),
        ]:
            tree.heading(col, text=title)
            tree.column(col, width=width, anchor=anchor)

        for item_name, qty, unit_price, line_total in lines:
            tree.insert(
                "", "end",
                values=(item_name, qty, money(unit_price), money(line_total))
            )

        scroll = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        buttons = ttk.Frame(content)
        buttons.pack(fill="x", pady=(10, 0))
        if invoice_id:
            ttk.Button(
                buttons,
                text="Open Original Invoice",
                command=lambda: (
                    win.destroy(),
                    self.open_transaction_detail(int(invoice_id))
                ),
            ).pack(side="left")
        ttk.Button(
            buttons, text="Close", command=win.destroy
        ).pack(side="right")

    def open_raw_inventory_log(self):
        win = tk.Toplevel(self)
        win.title("Raw Inventory Log")
        win.transient(self)
        win.geometry("1050x620")
        win.minsize(760, 420)

        outer = ttk.Frame(win, padding=12)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="Raw Inventory Movement Log",
            style="Title.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "Low-level item movements used for inventory auditing. "
                "This includes shipment/invoice adjustments and reversals."
            ),
            foreground="#666",
            wraplength=980,
        ).pack(anchor="w", pady=(4, 10))

        table = ttk.Frame(outer)
        table.pack(fill="both", expand=True)

        cols = ("time", "type", "ref", "item", "change", "note")
        raw_tree = ttk.Treeview(
            table, columns=cols, show="headings", height=16
        )
        for col, title, width, anchor in [
            ("time", "Time", 155, "w"),
            ("type", "Type", 90, "center"),
            ("ref", "Reference", 135, "w"),
            ("item", "Item", 235, "w"),
            ("change", "Qty Change", 100, "center"),
            ("note", "Note", 300, "w"),
        ]:
            raw_tree.heading(col, text=title)
            raw_tree.column(col, width=width, anchor=anchor)

        for row in self.db.list_history(limit=1000):
            raw_tree.insert("", "end", values=row)

        vsb = ttk.Scrollbar(table, orient="vertical", command=raw_tree.yview)
        raw_tree.configure(yscrollcommand=vsb.set)
        raw_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(
            actions,
            text="Export Raw Log CSV",
            command=lambda: self.export_treeview_csv(raw_tree, "raw_inventory_log"),
        ).pack(side="left")
        ttk.Button(
            actions, text="Close", command=win.destroy
        ).pack(side="right")

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
        self.refresh_special_price_item_choices()
        self._update_hints()

    def _configure_item_autocomplete(self, combo, variable, filter_inventory=False):
        combo._item_filter_inventory = bool(filter_inventory)

        combo.bind(
            "<KeyRelease>",
            lambda event, c=combo, v=variable: self._autocomplete_keyrelease(c, v, event),
        )
        combo.bind(
            "<Down>",
            lambda event, c=combo, v=variable: self._autocomplete_move(c, v, 1),
        )
        combo.bind(
            "<Up>",
            lambda event, c=combo, v=variable: self._autocomplete_move(c, v, -1),
        )
        combo.bind(
            "<Return>",
            lambda event, c=combo, v=variable: self._autocomplete_accept(c, v),
        )
        combo.bind("<Escape>", lambda event: self._hide_item_suggestions())

    def _ensure_item_suggestion_popup(self):
        popup = getattr(self, "_item_suggestion_popup", None)
        if popup is not None and popup.winfo_exists():
            return

        popup = tk.Toplevel(self)
        popup.withdraw()
        popup.overrideredirect(True)

        frame = ttk.Frame(popup, relief="solid", borderwidth=1)
        frame.pack(fill="both", expand=True)

        listbox = tk.Listbox(
            frame,
            exportselection=False,
            activestyle="dotbox",
            highlightthickness=0,
            borderwidth=0,
        )
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=scrollbar.set)

        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        listbox.bind("<ButtonRelease-1>", self._autocomplete_click)

        self._item_suggestion_popup = popup
        self._item_suggestion_list = listbox
        self._item_suggestion_combo = None
        self._item_suggestion_variable = None

    def _autocomplete_keyrelease(self, combo, variable, event=None):
        if event and event.keysym in {
            "Up", "Down", "Return", "Escape", "Tab",
            "Left", "Right", "Home", "End",
        }:
            return

        typed = variable.get().strip()
        matches = self.db.list_items(typed)
        names = [row[1] for row in matches[:50]]
        combo["values"] = names

        if getattr(combo, "_item_filter_inventory", False):
            self.refresh_inventory()

        if typed and names:
            self._show_item_suggestions(combo, variable, names)
        else:
            self._hide_item_suggestions()

    def _show_item_suggestions(self, combo, variable, names):
        self._ensure_item_suggestion_popup()

        popup = self._item_suggestion_popup
        listbox = self._item_suggestion_list
        self._item_suggestion_combo = combo
        self._item_suggestion_variable = variable

        listbox.delete(0, "end")
        for name in names:
            listbox.insert("end", name)

        if names:
            listbox.selection_clear(0, "end")
            listbox.selection_set(0)
            listbox.activate(0)

        combo.update_idletasks()

        width = max(combo.winfo_width(), 220)
        rows = min(len(names), 8)
        height = rows * 22 + 4
        x = combo.winfo_rootx()
        y = combo.winfo_rooty() + combo.winfo_height()

        if y + height > self.winfo_screenheight():
            y = combo.winfo_rooty() - height

        popup.geometry(f"{width}x{height}+{x}+{y}")
        popup.deiconify()
        popup.lift()

        # Keep keyboard focus in the item box while suggestions stay visible.
        combo.focus_set()

    def _autocomplete_move(self, combo, variable, direction):
        self._ensure_item_suggestion_popup()

        if (
            self._item_suggestion_popup.state() == "withdrawn"
            or self._item_suggestion_combo is not combo
        ):
            typed = variable.get().strip()
            names = [row[1] for row in self.db.list_items(typed)[:50]]
            if names:
                self._show_item_suggestions(combo, variable, names)
            return "break"

        lb = self._item_suggestion_list
        if lb.size() == 0:
            return "break"

        selected = lb.curselection()
        current = selected[0] if selected else 0
        target = max(0, min(lb.size() - 1, current + direction))

        lb.selection_clear(0, "end")
        lb.selection_set(target)
        lb.activate(target)
        lb.see(target)
        return "break"

    def _autocomplete_accept(self, combo, variable):
        popup = getattr(self, "_item_suggestion_popup", None)
        if popup is None or popup.state() == "withdrawn":
            return

        if self._item_suggestion_combo is not combo:
            return

        selected = self._item_suggestion_list.curselection()
        if not selected:
            return "break"

        name = self._item_suggestion_list.get(selected[0])
        self._choose_item_suggestion(combo, variable, name)
        return "break"

    def _autocomplete_click(self, event=None):
        combo = getattr(self, "_item_suggestion_combo", None)
        variable = getattr(self, "_item_suggestion_variable", None)
        if combo is None or variable is None:
            return

        selected = self._item_suggestion_list.curselection()
        if not selected:
            return

        name = self._item_suggestion_list.get(selected[0])
        self._choose_item_suggestion(combo, variable, name)

    def _choose_item_suggestion(self, combo, variable, name):
        variable.set(name)
        self._hide_item_suggestions()
        combo.focus_set()

        try:
            combo.icursor("end")
        except tk.TclError:
            pass

        if getattr(combo, "_item_filter_inventory", False):
            self.refresh_inventory()
            self.on_inventory_search_selected()

    def _hide_item_suggestions(self):
        popup = getattr(self, "_item_suggestion_popup", None)
        if popup is not None and popup.winfo_exists():
            popup.withdraw()
        return "break"

    def _filter_item_choices(self, combo, variable, event=None):
        return self._autocomplete_keyrelease(combo, variable, event)

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
