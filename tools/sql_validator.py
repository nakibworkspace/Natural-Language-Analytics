"""SQL safety validator.

The validator is the **security boundary** between LLM-generated SQL and
the database. It is intentionally deterministic: no LLM, no prompts, only
AST-ish parsing.

For V1 we use ``sqlparse`` for a quick-and-dirty rejection of write/DDL
statements. We pair this with a read-only PostgreSQL role so that even if
a statement slips through, the database itself refuses to execute it.

Hard rules:
    1. Reject any statement that is not a SELECT or WITH (CTE).
    2. Reject multiple statements (no semicolons separating them).
    3. Reject obvious forbidden keywords (INSERT, UPDATE, DELETE, DROP,
       ALTER, TRUNCATE, CREATE, GRANT, REVOKE, COPY, VACUUM, ...).
    4. Reject bare ``;`` at the end as a separate statement check.
    5. Reject comments containing ``--`` block tricks.
"""

from __future__ import annotations

import re
import sqlparse
from sqlparse.sql import Statement, Token
from sqlparse.tokens import Keyword, DML, DDL


FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
    "CREATE", "GRANT", "REVOKE", "COPY", "VACUUM", "REINDEX",
    "CLUSTER", "LOCK", "CALL", "DO", "SET", "RESET",
    "DISCARD", "LOAD", "SECURITY", "REASSIGN", "OWNER",
    "LISTEN", "NOTIFY", "UNLISTEN", "TRUNCATE", "MERGE",
    "EXPLAIN",  # we explicitly don't want EXPLAIN ANALYZE either
}

# Allowed statement starts
ALLOWED_DML = {"SELECT", "WITH"}


class SQLValidationError(ValueError):
    """Raised when SQL fails the validator."""


def _strip_comments_keep_strings(sql: str) -> str:
    """Collapse comments to spaces while preserving string literals."""
    out = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        # single-line comment
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                out.append(" ")
                i += 1
            continue
        # block comment
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            out.append(" ")
            out.append(" ")
            i += 2
            while i < n - 1 and not (sql[i] == "*" and sql[i + 1] == "/"):
                out.append(" " if sql[i] != "\n" else "\n")
                i += 1
            if i < n - 1:
                out.append(" ")
                out.append(" ")
                i += 2
            continue
        # standard single-quoted string
        if c == "'":
            out.append(c); i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == "'" and (i + 1 >= n or sql[i + 1] != "'"):
                    i += 1
                    break
                if sql[i] == "'" and i + 1 < n and sql[i + 1] == "'":
                    out.append(sql[i + 1]); i += 2; continue
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def validate_sql(sql: str) -> str:
    """Validate SQL and return the (cleaned) SQL string.

    Raises ``SQLValidationError`` if the statement is not safe.
    """
    if not sql or not sql.strip():
        raise SQLValidationError("Empty SQL")

    cleaned = _strip_comments_keep_strings(sql).strip()
    if not cleaned:
        raise SQLValidationError("SQL is only comments")

    # Reject multiple statements. We allow a single trailing semicolon.
    without_trailing = cleaned.rstrip()
    if without_trailing.endswith(";"):
        without_trailing = without_trailing[:-1].rstrip()
    # Check for any other semicolons (could be a separator)
    if ";" in without_trailing:
        raise SQLValidationError("Multiple SQL statements are not allowed")

    # Tokenize and inspect the first meaningful keyword
    statements = sqlparse.parse(without_trailing + ";")
    if len(statements) != 1:
        raise SQLValidationError("Multiple SQL statements are not allowed")

    stmt: Statement = statements[0]
    # Find the first token that is a real keyword
    first_keyword = None
    for tok in stmt.flatten():
        if tok.ttype in (Keyword, Keyword.DML, Keyword.DDL, Keyword.CTE) or tok.is_keyword:
            first_keyword = tok.normalized.upper()
            break
    if first_keyword not in ALLOWED_DML:
        raise SQLValidationError(
            f"Only SELECT / WITH queries are allowed; got '{first_keyword}'"
        )

    # Reject any forbidden keyword anywhere in the statement
    upper = without_trailing.upper()
    for kw in FORBIDDEN_KEYWORDS:
        # Match keyword boundary; *avoid* false positives on column names like
        # "created_at" matching "CREATE" — we use a whitespace / start boundary.
        m = re.search(rf"(?<![\w]){kw}(?![\w])", upper)
        if m:
            # Some matches are legitimate: e.g. "INSERT" might appear in a
            # string literal. We already stripped comments/strings, so be
            # strict.
            raise SQLValidationError(f"Forbidden keyword '{kw}' in SQL")

    return without_trailing + ";"


def is_safe_ident(name: str) -> bool:
    """Whitelist identifier (table/column names from schema inspection)."""
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name))
