# ---------------------------------------------------------------------------
# SQL RAG: Natural language to SQL query generation and answer with citation
# Flow: User question -> LLM generates SQL -> Execute SQL -> LLM answers with citation
# ---------------------------------------------------------------------------
import ast
import os
import re
from typing import Dict, Any, List, Optional


def _safe_strip(value):
    """Strip a value, handling list, dict, and dict-like strings gracefully."""
    if isinstance(value, dict):
        return value.get("text", str(value)).strip()
    if isinstance(value, list):
        parts = []
        for v in value:
            if isinstance(v, dict):
                parts.append(v.get("text", str(v)).strip())
            else:
                parts.append(str(v).strip())
        return " ".join(parts)

    stripped = str(value).strip()
    # Try to parse dict-like string: {'type': 'text', 'text': '...', ...}
    if stripped.startswith("{") and "'text'" in stripped:
        try:
            parsed = ast.literal_eval(stripped)
            if isinstance(parsed, dict) and "text" in parsed:
                return parsed["text"].strip()
        except Exception:
            pass
    return stripped


def _extract_sql(text: str) -> str:
    """Extract a clean leading SQL statement from an LLM response.

    Strips markdown fences, comments, explanatory text, garbage signatures,
    empty string literals, and trailing garbage. Balances quotes/parens and
    moves UNION ORDER BY to the end so SQL Server accepts the query.
    """
    if isinstance(text, list):
        text = " ".join(str(t) for t in text)
    if not isinstance(text, str):
        text = str(text)

    # Remove non-printable / zero-width Unicode chars that break SQL Server
    text = "".join(ch for ch in text if ch.isprintable() or ch in "\t\n\r")
    text = text.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    text = text.replace("\ufeff", "")

    # Remove markdown code fences
    text = re.sub(r"```\w*\n?", "", text).replace("```", "")

    # Drop comment-only lines (--) that appear before the query
    text = "\n".join(
        line for line in text.splitlines()
        if line.strip() and not line.strip().startswith("--")
    )

    # Locate the first read-only keyword and cut everything before it
    match = re.search(r"\b(SELECT|WITH)\b", text, re.IGNORECASE)
    if match:
        text = text[match.start():]

    # Keep only up to the first semicolon
    semicolon_idx = text.find(";")
    if semicolon_idx != -1:
        text = text[: semicolon_idx + 1]

    # ---- quote / empty-literal cleanup ----
    text = _balance_quotes(text)
    text = _remove_empty_quotes(text)
    text = text.strip()
    while text.endswith("''"):
        text = text[:-2].rstrip()

    # ---- trailing garbage signatures ----
    text = _truncate_at_garbage_signature(text)

    # ---- trailing incomplete SQL (dangling comma, AND/OR, WHERE, etc.) ----
    text = _trim_trailing_incomplete(text)

    # ---- trailing invalid characters (never strip ')' or ''' here) ----
    text = re.sub(r'[\"\";\[\]]+$', "", text.rstrip())

    # ---- balance quotes/parens LAST (nothing strips them after) ----
    text = _balance_sql(text)

    # ---- UNION: ORDER BY must be at end, not per-part ----
    text = _fix_union_order_by(text)

    # Final safety: remove any remaining trailing '' pairs
    text = text.rstrip()
    while text.endswith("''"):
        text = text[:-2].rstrip()

    return text.strip()


def _balance_quotes(sql: str) -> str:
    """Return sql as-is if quotes are unbalanced; _balance_sql closes them."""
    return sql


def _remove_empty_quotes(sql: str) -> str:
    """Remove empty string literals '' that cause SQL Server error 1038/105."""
    if not sql:
        return sql

    # Protect valid comparisons: operator '' keyword/end
    protected: List[str] = []
    pattern = re.compile(
        r"(\s*(?:=|!=|<>|>=|<=|>|<|LIKE|NOT\s+LIKE)\s*)''\s*"
        r"((?:FROM|WHERE|GROUP|ORDER|HAVING|ASC|DESC|AND|OR)\b|$)",
        re.IGNORECASE,
    )

    def repl(m):
        protected.append(m.group(2))
        return m.group(1) + f" __EP{len(protected) - 1}__ "

    sql = pattern.sub(repl, sql)

    # Remove remaining '' literals
    sql = sql.replace("''", "")

    # Restore protected comparisons
    for i, kw in enumerate(protected):
        sql = sql.replace(f"__EP{i}__", kw)

    # Fix AS '' aliases and stray commas left behind
    sql = re.sub(r"\s+AS\s+''\s*", " ", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\s+N''\s*", " ", sql, flags=re.IGNORECASE)
    sql = re.sub(r",\s*,", ",", sql)
    sql = re.sub(r"\s*,\s*$", "", sql)

    # Remove trailing dangling operators left by '' removal
    sql = sql.rstrip()
    for op in ("<>", "!=", ">=", "<=", "=", ">", "<"):
        if sql.endswith(op):
            sql = sql[: -len(op)].rstrip()

    return sql


def _trim_trailing_incomplete(sql: str) -> str:
    """Trim dangling comma, AND/OR/WHERE/ORDER BY/GROUP BY/HAVING at end."""
    sql = sql.strip()
    if not sql:
        return sql
    upper = sql.upper()
    for tail in (",", "AND", "OR", "WHERE", "ORDER BY", "GROUP BY", "HAVING"):
        if upper.endswith(tail):
            sql = sql[: -len(tail)].rstrip()
            upper = sql.upper()
    return sql


def _truncate_at_garbage_signature(sql: str, max_len: int = 128) -> str:
    """Cut at LLM garbage patterns (signatures, long identifiers, base64)."""
    patterns = [
        r",\s*'extras'", r'"extras"', r"'extras'",
        r'signature\s*:', r'\}\s*\}', r'\}\s*,', r'\}\s*$',
    ]
    for p in patterns:
        m = re.search(p, sql, re.IGNORECASE)
        if m:
            return sql[: m.start()].rstrip()

    for m in re.finditer(r"[A-Za-z0-9+/=]{60,}", sql):
        return sql[: m.start()].rstrip()

    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", sql):
        if m.end() - m.start() > max_len:
            return sql[: m.start()].rstrip()

    return sql


def _balance_sql(sql: str) -> str:
    """Close missing quotes and parentheses at end of statement.

    ...for example: IN (N'مشتری', N'زحل'  -> ...IN (N'مشتری', N'زحل')
    """
    if not sql:
        return sql

    # Count single quotes ('' is an escaped pair)
    quote_count = 0
    i = 0
    n = len(sql)
    while i < n:
        if sql[i] == "'":
            if i + 1 < n and sql[i + 1] == "'":
                i += 2
                continue
            quote_count += 1
        i += 1

    if quote_count % 2 == 1:
        sql = sql.rstrip() + "'"

    open_parens = sql.count("(") - sql.count(")")
    if open_parens > 0:
        sql = sql.rstrip() + ")" * open_parens

    return sql


def _fix_union_order_by(sql: str) -> str:
    """Move ORDER BY to end of UNION (SQL Server forbids per-part ORDER BY)."""
    if "UNION" not in sql.upper():
        return sql

    # Extract trailing ORDER BY first
    order_by = ""
    m = re.search(r"\s+ORDER\s+BY\s+.+$", sql, flags=re.IGNORECASE)
    if m:
        order_by = m.group(0).strip()
        sql = sql[: m.start()].strip()

    is_union_all = bool(re.search(r"\bUNION\s+ALL\b", sql, flags=re.IGNORECASE))

    # Split on UNION (consumes whitespace around it)
    parts = re.split(r"\s+UNION\s+(?:ALL\s+)?", sql, flags=re.IGNORECASE)
    if len(parts) < 2:
        return sql + (" " + order_by if order_by else "")

    cleaned = []
    for part in parts:
        part = part.strip()
        # Remove wrapping parens around each part: (SELECT ...) -> SELECT ...
        part = re.sub(r"^\s*\(\s*", "", part)
        part = re.sub(r"\s*\)\s*$", "", part)
        # Remove ORDER BY from each part
        part = re.sub(r"\s+ORDER\s+BY\s+.+$", "", part, flags=re.IGNORECASE).strip()
        cleaned.append(part)

    kw = " UNION ALL " if is_union_all else " UNION "
    sql = kw.join(cleaned)
    if order_by:
        sql = sql + " " + order_by
    return sql


from db_client import SQLServerClient, SQLServerConnectionError
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate


def _get_llm():
    return ChatGoogleGenerativeAI(
        model="gemini-3.5-flash-lite",
        google_api_key=os.getenv("GOOGLE_API_KEY"),
        temperature=0.1,
    )

# The LLM receives: "Database Schema: [tables + columns]" along with the user question, and generates SQL using those exact names
SQL_GENERATION_PROMPT = """You are an expert SQL query generator. Your task is to convert a natural language question into a valid SQL query for SQL Server.

Database Schema:
{schema}

Question: {question}

Instructions:
1. Generate ONLY a SELECT query (read-only, no INSERT/UPDATE/DELETE/DROP).
2. Use proper SQL Server syntax (T-SQL).
3. Use the exact table and column names from the schema.
4. For Persian text comparisons, use N'' prefix for string literals (e.g., N'فارسی').
5. If the question cannot be answered with the given schema, return: SELECT 'NOT_ANSWERABLE' AS result
6. Return ONLY the SQL query, no explanation, no markdown, no code blocks.
7. If no data is needed, return: SELECT 'NO_DATA_NEEDED' AS result

SQL Query:"""


ANSWER_GENERATION_PROMPT = """You are a helpful assistant answering questions in Persian.

Question: {question}

SQL Query Executed: {sql_query}

Query Results:
{results}

Instructions:
1. Answer the question in Persian (فارسی) based ONLY on the query results.
2. If the results are empty or no data found, say: "متاسفانه اطلاعات کافی در پایگاه داده برای پاسخ به این سوال یافت نشد."
3. Be concise and accurate.
4. Do not add information outside the query results.
5. If the query returned 'NOT_ANSWERABLE', say: "متاسفانه این سوال با داده‌های موجود در پایگاه داده پاسخ داده نمی‌شود."

Answer:"""


class SQLRAG:
    """Natural language to SQL RAG system for SQL Server with Persian answer generation and citation.
    context: User question -> LLM generates SQL -> Execute SQL -> LLM answers with citation."""
    

    def __init__(self, client: Optional[SQLServerClient] = None):
        self.client = client or SQLServerClient()
        self.llm = _get_llm()
        self.sql_prompt = PromptTemplate(
            template=SQL_GENERATION_PROMPT,
            input_variables=["schema", "question"],
        )
        self.answer_prompt = PromptTemplate(
            template=ANSWER_GENERATION_PROMPT,
            input_variables=["question", "sql_query", "results"],
        )

    def connect(self) -> None:
        self.client.connect()

    def disconnect(self) -> None:
        self.client.close()

    def is_connected(self) -> bool:
        return self.client.is_connected()

    def get_database_name(self) -> str:
        return self.client.get_current_database()

    def get_schema_text(self) -> str:
        schema = self.client.get_schema()
        db_name = schema["database"]
        lines = [f"Database: {db_name}"]
        for table_name, columns in schema["tables"].items():
            lines.append(f"\nTable: {table_name}")
            for col in columns:
                col_desc = f"  - {col['name']} ({col['type']})"
                if col.get("max_length"):
                    col_desc += f" max_length={col['max_length']}"
                lines.append(col_desc)
        return "\n".join(lines)

    def generate_sql(self, question: str) -> str:
        schema_text = self.get_schema_text()
        prompt = self.sql_prompt.format(schema=schema_text, question=question)
        response = self.llm.invoke(prompt)
        sql = _safe_strip(response.content) if hasattr(response, "content") else _safe_strip(str(response))
        sql = _extract_sql(sql)
        return sql

    def execute_sql(self, sql: str) -> Dict[str, Any]:
        return self.client.execute_query(sql)

    def format_results(self, result: Dict[str, Any]) -> str:
        columns = result.get("columns", [])
        rows = result.get("rows", [])
        if not columns or not rows:
            return "No data returned (empty result set)."
        lines = [", ".join(columns)]
        for row in rows:
            lines.append(", ".join(str(v) for v in row))
        return "\n".join(lines)

    def generate_answer(self, question: str, sql: str, result: Dict[str, Any]) -> str:
        results_text = self.format_results(result)
        prompt = self.answer_prompt.format(
            question=question, sql_query=sql, results=results_text,
        )
        response = self.llm.invoke(prompt)
        answer = _safe_strip(response.content) if hasattr(response, "content") else _safe_strip(str(response))
        return answer

    def query(self, question: str) -> Dict[str, Any]:
        """Full flow: Question -> SQL -> Execute -> Answer with Citation."""
        if not self.is_connected():
            raise SQLServerConnectionError("Not connected to SQL Server")

        db_name = self.get_database_name()

        sql = self.generate_sql(question)
        result = self.execute_sql(sql)
        answer = self.generate_answer(question, sql, result)
        citation = self._build_citation(db_name, sql, result)

        return {
            "answer": answer,
            "sql": sql,
            "result": result,
            "citation": citation,
            "database": db_name,
        }

    def _build_citation(
        self, db_name: str, sql: str, result: Dict[str, Any]
    ) -> str:
        """Build citation string with database name, table name, and records."""
        citation_parts = [f"**پایگاه داده:** `{db_name}`"]

        tables = self._extract_tables_from_sql(sql)
        if tables:
            citation_parts.append(f"**جدول‌ها:** {', '.join(tables)}")

        row_count = result.get("row_count", 0)
        citation_parts.append(f"**تعداد رکوردهای استخراج‌شده:** {row_count}")

        rows = result.get("rows", [])
        columns = result.get("columns", [])
        if rows and columns:
            citation_parts.append("**رکورد:**")
            for i, row in enumerate(rows[:5], 1):
                record_str = ", ".join(f"{col}: {val}" for col, val in zip(columns, row))
                citation_parts.append(f"  {i}. {record_str}")
            if len(rows) > 5:
                citation_parts.append(f"  ... و {len(rows) - 5} رکورد دیگر")

        return "\n".join(citation_parts)

    def _extract_tables_from_sql(self, sql: str) -> List[str]:
        """Extract table names from SQL query (best effort)."""
        tables = set()
        for pattern in (r"FROM\s+(\w+)", r"JOIN\s+(\w+)"):
            for m in re.findall(pattern, sql, re.IGNORECASE):
                if m.upper() not in ("SELECT", "WHERE", "ORDER", "GROUP", "HAVING"):
                    tables.add(m)
        return sorted(tables)
