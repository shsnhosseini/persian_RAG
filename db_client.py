# ---------------------------------------------------------------------------
# SQL Server database client (pyodbc + ODBC Driver 17/18 for SQL Server)
# Provides connection, schema discovery, and query execution.
# ---------------------------------------------------------------------------
import os
from typing import List, Dict, Any, Optional
import pyodbc


class SQLServerConnectionError(Exception):
    """Raised when connection to SQL Server fails."""
    pass

class SQLServerClient:
    """
    SQL Server client using pyodbc with ODBC Driver 17/18.
    """

    def __init__(
        self,
        server: str = "localhost",
        port: int = None,
        database: str = None,
        username: str = "sa",
        password: str = "YourStrong!Passw0rd",
        driver: Optional[str] = None,
    ):
        self.server = server
        self.port = int(os.getenv("MSSQL_PORT", "1433")) if port is None else port
        self.database = os.getenv("MSSQL_DATABASE", "master") if database is None else database
        self.username = username
        self.password = password
        self.driver = driver or os.getenv(
            "MSSQL_DRIVER", "ODBC Driver 17 for SQL Server"
        )
        self.connection: Optional[pyodbc.Connection] = None

    def connect(self) -> None:
        """Establishes a connection to the SQL Server."""
        conn_str = (
            f"DRIVER={{{self.driver}}};"
            f"SERVER={self.server},{self.port};"
            f"DATABASE={self.database};"
            f"UID={self.username};"
            f"PWD={self.password};"
            f"TrustServerCertificate=yes;"
        )
        try:
            self.connection = pyodbc.connect(conn_str, timeout=15)
            self.connection.setencoding("utf-8")
        except pyodbc.Error as e:
            raise SQLServerConnectionError(f"Connection failed: {e}") from e

    def reconnect(self) -> None:
        """Close and re-establish connection."""
        self.close()
        self.connect()

    def close(self) -> None:
        """Close the connection."""
        if self.connection:
            try:
                self.connection.close()
            except Exception:
                pass
            self.connection = None

    def is_connected(self) -> bool:
        """Check if connection is active."""
        if not self.connection:
            return False
        try:
            self.connection.cursor()
            return True
        except pyodbc.Error:
            return False

    # ------------------------------------------------------------------
    # Schema discovery
    # ------------------------------------------------------------------
    def list_databases(self) -> List[str]:
        """
        Returns all accessible database names.
        """
        if not self.is_connected():
            raise SQLServerConnectionError("Not connected to SQL Server")

        cursor = self.connection.cursor()
        cursor.execute("SELECT name FROM sys.databases WHERE state = 0 ORDER BY name")
        db_names = [row[0] for row in cursor.fetchall()]
        cursor.close()
        return db_names

    def get_current_database(self) -> str:
        """Returns the currently selected database name."""
        if not self.is_connected():
            raise SQLServerConnectionError("Not connected to SQL Server")
        cursor = self.connection.cursor()
        cursor.execute("SELECT DB_NAME() AS db_name")
        row = cursor.fetchone()
        cursor.close()
        return row[0] if row else "master"

    def list_tables(self) -> List[str]:
        """
        Returns table names in the currently selected database.
        """
        if not self.is_connected():
            raise SQLServerConnectionError("Not connected to SQL Server")

        cursor = self.connection.cursor()
        cursor.execute(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME"
        )
        tables = [row[0] for row in cursor.fetchall()]
        cursor.close()
        return tables

    def list_views(self) -> List[str]:
        """Returns view names in the currently selected database."""
        if not self.is_connected():
            raise SQLServerConnectionError("Not connected to SQL Server")

        cursor = self.connection.cursor()
        cursor.execute(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.VIEWS ORDER BY TABLE_NAME"
        )
        views = [row[0] for row in cursor.fetchall()]
        cursor.close()
        return views

    def get_table_columns(self, table_name: str) -> List[Dict[str, Any]]:
        """
        Returns column metadata for a given table.
        """
        if not self.is_connected():
            raise SQLServerConnectionError("Not connected to SQL Server")

        cursor = self.connection.cursor()
        cursor.execute(
            "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, "
            "NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
            table_name,
        )
        columns = []
        for row in cursor.fetchall():
            columns.append({
                "name": row[0],
                "type": row[1],
                "max_length": row[2],
                "numeric_precision": row[3],
                "numeric_scale": row[4],
                "is_nullable": row[5].upper() == "YES",
            })
        cursor.close()
        return columns

    def get_schema(self) -> Dict[str, Any]:
        """
        Returns full schema (tables + columns) for the current database.
        """
        tables = self.list_tables()
        schema = {
            "database": self.get_current_database(),
            "tables": {},
        }
        for table in tables:
            schema["tables"][table] = self.get_table_columns(table)
        return schema

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------
    def is_read_only_query(self, query: str) -> bool:
        """
        Heuristic to check if the query is a SELECT statement (read-only).
        We reject INSERT/UPDATE/DELETE/CREATE/DROP/ALTER etc. for safety.
        """
    # Handle case where query might be a list (e.g., from LLM response)
        if isinstance(query, list):
            query = " ".join(str(q) for q in query)
        elif not isinstance(query, str):
            query = str(query)
        if not query or not query.strip():
            return False
        upper_query = query.strip().upper()
        
        # Remove any leading semicolons
        while upper_query.startswith(";"):
            upper_query = upper_query[1:].strip()
        
        # Allow: SELECT, WITH ... AS (CTE), UNION, INTERSECT, EXCEPT
        # Reject: INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, etc.
        
        # upper_query already stripped above
        
        # Check if query starts with allowed read-only keywords
        allowed_start = ("SELECT", "WITH",)
        
        # Check if query starts with an allowed keyword
        is_allowed = any(upper_query.startswith(keyword) for keyword in allowed_start)
        
        # Additional safety: reject if query contains dangerous keywords at the start
        dangerous_keywords = ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "TRUNCATE")
        has_dangerous_start = any(
            upper_query.startswith(f"{kw} ") or upper_query.startswith(f"{kw}(") for kw in dangerous_keywords
        )
        
        return is_allowed and not has_dangerous_start

    def execute_query(
        self,
        query: str,
        params: Optional[tuple] = None,
        fetch_size: int = 500,
    ) -> Dict[str, Any]:
        """
        Executes a SQL query and returns results.
        Supports both SELECT (returns data) and non-SELECT (returns rowcount).
        """
        if not self.is_connected():
            raise SQLServerConnectionError("Not connected to SQL Server")

        if not self.is_read_only_query(query):
            raise ValueError(
                "Only SELECT (read-only) queries are allowed for safety. "
                "The query must start with SELECT."
            )

        cursor = self.connection.cursor()
        try:
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)

            # Fetch all rows
            rows = cursor.fetchall()
            # Get column names from description
            columns = [desc[0] for desc in cursor.description] if cursor.description else []

            result = {
                "columns": columns,
                "rows": [list(row) for row in rows],
                "row_count": len(rows),
            }
            return result

        except pyodbc.Error as e:
            msg = f"Query execution failed: {e}\nQuery: {query}"
            if "1038" in str(e) or "105" in str(e):
                msg += "\nHint: Verify the table and column names exist in your database schema."
            raise ValueError(msg) from e
        finally:
            cursor.close()
