# PostgreSQL Database Connection Guide

## Database: wirelesscustomerengineering

This folder contains connection information and sample scripts to connect to and work with the PostgreSQL database.

---

## Quick Start

1. **Install dependencies:**
   ```bash
   pip install psycopg2-binary python-dotenv
   ```

2. **Set up environment:**
   - Copy `.env.template` to `.env`
   - Fill in your credentials (or use the default values if you have access)

3. **Test connection:**
   ```bash
   python 01_test_connection.py
   ```

4. **Create a table:**
   ```bash
   python 02_create_table_example.py
   ```

---

## Files in This Folder

| File | Description |
|------|-------------|
| `README.md` | This guide |
| `.env.template` | Environment variable template |
| `01_test_connection.py` | Test database connectivity |
| `02_create_table_example.py` | Example: create a new table |
| `03_crud_operations.py` | Example: insert, select, update, delete |
| `db_utils.py` | Reusable database connection utilities |

---

## Connection Details

| Parameter | Value |
|-----------|-------|
| **Host** | `10-108-27-113.dbaas.intel.com` |
| **Port** | `5432` |
| **Database** | `wirelesscustomerengineering` |
| **User** | `wirelesscustomerengi_so` |
| **Schema** | `public` |

---

## Notes

- The user `wirelesscustomerengi_so` has **CREATE TABLE** permission in the `public` schema.
- Always use parameterized queries to prevent SQL injection.
- Remember to commit transactions after INSERT/UPDATE/DELETE operations.
