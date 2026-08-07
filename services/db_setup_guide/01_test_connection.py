#!/usr/bin/env python
"""
01_test_connection.py
Test PostgreSQL database connectivity.

Usage:
    python 01_test_connection.py
"""

import sys
from db_utils import get_connection, print_config


def test_connection():
    """Test database connection and print server info."""
    print("=" * 60)
    print("PostgreSQL Connection Test")
    print("=" * 60)
    
    # Show configuration
    print("\n[1] Configuration:")
    print_config()
    
    # Test connection
    print("\n[2] Testing connection...")
    try:
        conn = get_connection()
        print("    ✓ Connection successful!")
    except Exception as e:
        print(f"    ✗ Connection failed: {e}")
        sys.exit(1)
    
    # Get server info
    print("\n[3] Server Information:")
    cur = conn.cursor()
    
    try:
        # PostgreSQL version
        cur.execute("SELECT version();")
        version = cur.fetchone()[0]
        print(f"    Version: {version.split(',')[0]}")
        
        # Current user and database
        cur.execute("SELECT current_user, current_database();")
        user, database = cur.fetchone()
        print(f"    User: {user}")
        print(f"    Database: {database}")
        
        # Current schema
        cur.execute("SELECT current_schema();")
        schema = cur.fetchone()[0]
        print(f"    Schema: {schema}")
        
        # Server time
        cur.execute("SELECT NOW();")
        server_time = cur.fetchone()[0]
        print(f"    Server Time: {server_time}")
        
    finally:
        cur.close()
        conn.close()
    
    print("\n" + "=" * 60)
    print("✓ All tests passed! Database connection is working.")
    print("=" * 60)


if __name__ == "__main__":
    test_connection()
