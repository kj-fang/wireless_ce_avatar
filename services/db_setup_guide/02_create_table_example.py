#!/usr/bin/env python
"""
02_create_table_example.py
Example: Create a new table in PostgreSQL.

Usage:
    python 02_create_table_example.py

This script demonstrates:
    - Creating a table with various column types
    - Checking if table already exists
    - Verifying table structure
    - Optional: dropping the table
"""

import sys
from db_utils import get_connection, get_db_cursor


# Define your table name here
TABLE_NAME = "my_sample_table"

# Define table schema (modify as needed)
CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    quantity INTEGER DEFAULT 0,
    price NUMERIC(10, 2),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def check_table_exists(table_name):
    """Check if a table exists in the database."""
    with get_db_cursor(commit=False) as cur:
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_name = %s
            );
        """, (table_name,))
        return cur.fetchone()[0]


def create_table():
    """Create the sample table."""
    print("=" * 60)
    print(f"Creating Table: {TABLE_NAME}")
    print("=" * 60)
    
    # Check if table already exists
    print(f"\n[1] Checking if table '{TABLE_NAME}' exists...")
    if check_table_exists(TABLE_NAME):
        print(f"    ⚠ Table '{TABLE_NAME}' already exists!")
        response = input("    Do you want to drop and recreate it? (y/N): ")
        if response.lower() == 'y':
            with get_db_cursor() as cur:
                cur.execute(f"DROP TABLE {TABLE_NAME};")
                print(f"    ✓ Table dropped.")
        else:
            print("    Skipping table creation.")
            return
    else:
        print(f"    Table does not exist. Proceeding to create...")
    
    # Create the table
    print(f"\n[2] Creating table '{TABLE_NAME}'...")
    try:
        with get_db_cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
        print(f"    ✓ Table '{TABLE_NAME}' created successfully!")
    except Exception as e:
        print(f"    ✗ Failed to create table: {e}")
        sys.exit(1)
    
    # Verify table structure
    print(f"\n[3] Table structure:")
    with get_db_cursor(commit=False) as cur:
        cur.execute("""
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = %s
            ORDER BY ordinal_position;
        """, (TABLE_NAME,))
        
        columns = cur.fetchall()
        print(f"    {'Column':<15} {'Type':<20} {'Nullable':<10} {'Default':<20}")
        print("    " + "-" * 65)
        for col_name, data_type, nullable, default in columns:
            default_str = str(default)[:18] if default else ""
            print(f"    {col_name:<15} {data_type:<20} {nullable:<10} {default_str:<20}")
    
    print("\n" + "=" * 60)
    print("✓ Table creation complete!")
    print("=" * 60)


def drop_table():
    """Drop the sample table (cleanup)."""
    print(f"\nDropping table '{TABLE_NAME}'...")
    try:
        with get_db_cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {TABLE_NAME};")
        print(f"✓ Table '{TABLE_NAME}' dropped.")
    except Exception as e:
        print(f"✗ Failed to drop table: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--drop":
        drop_table()
    else:
        create_table()
        print("\nTip: Run 'python 02_create_table_example.py --drop' to remove the table.")
