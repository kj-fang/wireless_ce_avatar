"""
Database Utilities Module
Reusable functions for PostgreSQL database connection.
"""

import os
from contextlib import contextmanager
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Database configuration
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "10-108-27-113.dbaas.intel.com"),
    "port": os.environ.get("DB_PORT", "5432"),
    "database": os.environ.get("DB_NAME", "wirelesscustomerengineering"),
    "user": os.environ.get("DB_USER", "wirelesscustomerengi_so"),
    "password": os.environ.get("DB_PASS"),
}


def get_connection():
    """
    Create and return a new database connection.
    
    Returns:
        psycopg2.connection: Database connection object
        
    Example:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        conn.close()
    """
    import psycopg2
    return psycopg2.connect(**DB_CONFIG)


@contextmanager
def get_db_cursor(commit=True):
    """
    Context manager for database operations.
    Automatically handles connection and cursor lifecycle.
    
    Args:
        commit: If True, commits the transaction on success.
        
    Yields:
        psycopg2.cursor: Database cursor
        
    Example:
        with get_db_cursor() as cur:
            cur.execute("SELECT * FROM my_table")
            rows = cur.fetchall()
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def print_config():
    """Print current database configuration (password masked)."""
    print("Database Configuration:")
    print(f"  Host:     {DB_CONFIG['host']}")
    print(f"  Port:     {DB_CONFIG['port']}")
    print(f"  Database: {DB_CONFIG['database']}")
    print(f"  User:     {DB_CONFIG['user']}")
    print(f"  Password: {'*' * len(DB_CONFIG['password'] or '')}")
