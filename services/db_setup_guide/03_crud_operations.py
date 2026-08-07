#!/usr/bin/env python
"""
03_crud_operations.py
Example: Basic CRUD operations (Create, Read, Update, Delete).

Usage:
    python 03_crud_operations.py

Prerequisites:
    Run 02_create_table_example.py first to create the sample table.
"""

from db_utils import get_db_cursor

TABLE_NAME = "my_sample_table"


def insert_records():
    """INSERT: Add new records to the table."""
    print("\n[INSERT] Adding sample records...")
    
    records = [
        ("Widget A", "A basic widget", 100, 9.99, True),
        ("Widget B", "An advanced widget", 50, 19.99, True),
        ("Gadget X", "A cool gadget", 25, 49.99, False),
    ]
    
    with get_db_cursor() as cur:
        for record in records:
            cur.execute(f"""
                INSERT INTO {TABLE_NAME} (name, description, quantity, price, is_active)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id;
            """, record)
            result = cur.fetchone()
            if result:
                print(f"    ✓ Inserted: {record[0]} (id={result[0]})")
            else:
                print(f"    - Skipped: {record[0]} (may already exist)")
    
    print("    Done!")


def select_records():
    """SELECT: Query records from the table."""
    print("\n[SELECT] Reading all records...")
    
    with get_db_cursor(commit=False) as cur:
        cur.execute(f"""
            SELECT id, name, description, quantity, price, is_active, created_at
            FROM {TABLE_NAME}
            ORDER BY id;
        """)
        
        rows = cur.fetchall()
        
        if not rows:
            print("    No records found.")
            return
        
        print(f"    {'ID':<5} {'Name':<12} {'Qty':<6} {'Price':<8} {'Active':<8}")
        print("    " + "-" * 45)
        for row in rows:
            id_, name, desc, qty, price, active, created = row
            print(f"    {id_:<5} {name:<12} {qty:<6} ${price:<7} {str(active):<8}")
        
        print(f"\n    Total: {len(rows)} records")


def update_record():
    """UPDATE: Modify an existing record."""
    print("\n[UPDATE] Updating 'Widget A' quantity...")
    
    with get_db_cursor() as cur:
        cur.execute(f"""
            UPDATE {TABLE_NAME}
            SET quantity = quantity + 50,
                updated_at = CURRENT_TIMESTAMP
            WHERE name = %s
            RETURNING id, name, quantity;
        """, ("Widget A",))
        
        result = cur.fetchone()
        if result:
            print(f"    ✓ Updated: {result[1]} (id={result[0]}, new quantity={result[2]})")
        else:
            print("    - No record found to update.")


def delete_record():
    """DELETE: Remove a record from the table."""
    print("\n[DELETE] Removing 'Gadget X'...")
    
    with get_db_cursor() as cur:
        cur.execute(f"""
            DELETE FROM {TABLE_NAME}
            WHERE name = %s
            RETURNING id, name;
        """, ("Gadget X",))
        
        result = cur.fetchone()
        if result:
            print(f"    ✓ Deleted: {result[1]} (id={result[0]})")
        else:
            print("    - No record found to delete.")


def main():
    """Run all CRUD examples."""
    print("=" * 60)
    print("CRUD Operations Example")
    print("=" * 60)
    
    # Check if table exists
    with get_db_cursor(commit=False) as cur:
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = %s
            );
        """, (TABLE_NAME,))
        exists = cur.fetchone()[0]
    
    if not exists:
        print(f"\n⚠ Table '{TABLE_NAME}' does not exist!")
        print("  Please run 02_create_table_example.py first.")
        return
    
    # Run CRUD operations
    insert_records()
    select_records()
    update_record()
    select_records()
    delete_record()
    select_records()
    
    print("\n" + "=" * 60)
    print("✓ CRUD operations completed!")
    print("=" * 60)


if __name__ == "__main__":
    main()
