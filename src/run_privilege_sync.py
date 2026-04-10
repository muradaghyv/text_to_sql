"""
Privilege sync — maps ERPHUB privilege codes to table names, then resolves
per-employee table access and stores the result in nl2sql_metadata.

How it works:
  1. Fetch all table names from target DB (ERPHUB)
  2. Fetch all active, non-archived privileges from target DB
  3. Strip action suffixes (_VIEW, _ADD, _EDIT, _DELETE, _VIEW_DETAIL)
     to extract module prefixes  e.g. BUDGET_EXPENSE_VIEW → BUDGET_EXPENSE
  4. Fuzzy-match each prefix against table names
  5. Print all matches for review
  6. If --apply:
       a. Store privilege → table mapping in privilege_table_access
       b. Resolve emp_id → roles → privileges → tables
       c. Store results in emp_table_access

Usage:
    python -m src.run_privilege_sync              # dry run (print matches only)
    python -m src.run_privilege_sync --apply      # write to metadata DB
"""
import sys
import os
import difflib
from collections import defaultdict

from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ENV_PATH = os.path.join(PROJECT_ROOT, "env", ".env")

from logger import get_logger
from schema_extractor.list_tables import get_connection, list_table_names
from metadata_store import get_metadata_connection

# Action suffixes to strip — longer ones first (order matters)
_ACTION_SUFFIXES = ["_VIEW_DETAIL", "_VIEW", "_ADD", "_EDIT", "_DELETE"]

# Only store matches above this score
_MATCH_THRESHOLD = 0.6


def extract_prefix(privilege_code: str) -> str:
    """Strip trailing action suffix to get the module prefix.

    Examples:
        BUDGET_EXPENSE_VIEW   → BUDGET_EXPENSE
        PAYMENTS_VIEW_DETAIL  → PAYMENTS
        KPI_TYPE_ADD          → KPI_TYPE
        CODE                  → CODE  (no known suffix)
    """
    code = privilege_code.upper()
    for suffix in _ACTION_SUFFIXES:
        if code.endswith(suffix):
            return privilege_code[: -len(suffix)]
    return privilege_code


def normalize(s: str) -> str:
    """Lowercase and remove underscores/special chars for fuzzy comparison."""
    return s.lower().replace("_", "").replace("&", "and").replace(" ", "")


def match_prefix_to_tables(
    prefix: str, table_names: list[str]
) -> list[tuple[str, float]]:
    """
    Fuzzy-match a module prefix against all table names.

    Scoring:
      1.0   — exact match after normalization
      0.85+ — one string starts with the other (handles plurals, _types suffix)
      <0.85 — SequenceMatcher ratio

    Returns list of (table_name, score) sorted by score desc,
    filtered to scores >= _MATCH_THRESHOLD.
    """
    pn = normalize(prefix)
    results = []
    for table in table_names:
        tn = normalize(table)
        if pn == tn:
            score = 1.0
        elif tn.startswith(pn) or pn.startswith(tn):
            shorter = min(len(pn), len(tn))
            longer = max(len(pn), len(tn))
            score = 0.85 + 0.15 * (shorter / longer)
        else:
            score = difflib.SequenceMatcher(None, pn, tn).ratio()

        if score >= _MATCH_THRESHOLD:
            results.append((table, round(score, 3)))

    return sorted(results, key=lambda x: -x[1])


# ── Target DB queries ─────────────────────────────────────────────────────────

def fetch_privileges(conn) -> list[dict]:
    """Fetch all active, non-archived privileges from target DB."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT privilege_id, privilege_code
            FROM privileges
            WHERE is_active = 1
              AND is_archive = false
            ORDER BY privilege_id;
        """)
        return [{"privilege_id": r["privilege_id"], "privilege_code": r["privilege_code"]} for r in cur.fetchall()]


def fetch_all_emp_ids(conn) -> list[int]:
    """Fetch all distinct emp_ids that have at least one active role."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT emp_id
            FROM emp_roles
            WHERE is_active = 1
              AND is_archive = false;
        """)
        return [r["emp_id"] for r in cur.fetchall()]


def fetch_emp_privilege_ids(conn, emp_id: int) -> list[int]:
    """Resolve emp_id → active roles → active privileges, return privilege_ids."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT p.privilege_id
            FROM emp_roles er
            JOIN role_privileges rp
              ON rp.role_id = er.role_id
             AND rp.is_active = 1
             AND rp.is_archive = false
            JOIN privileges p
              ON p.privilege_id = rp.privilege_id
             AND p.is_active = 1
             AND p.is_archive = false
            WHERE er.emp_id = %s
              AND er.is_active = 1
              AND er.is_archive = false;
        """, (emp_id,))
        return [r["privilege_id"] for r in cur.fetchall()]


# ── Metadata DB queries ───────────────────────────────────────────────────────

def get_db_id(meta_conn, db_name: str) -> int:
    with meta_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM registered_databases WHERE db_name = %s", (db_name,)
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"DB '{db_name}' not found in registered_databases")
        return row["id"]


def store_privilege_table_access(
    meta_conn, db_id: int, privilege_id: int, table_name: str, score: float
) -> None:
    with meta_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO privilege_table_access (privilege_id, db_id, table_name, match_score)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (privilege_id, db_id, table_name) DO UPDATE
                SET match_score = EXCLUDED.match_score;
        """, (privilege_id, db_id, table_name, score))


def store_emp_table_access(
    meta_conn, db_id: int, emp_id: int, table_names: list[str]
) -> None:
    with meta_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM emp_table_access WHERE emp_id = %s AND db_id = %s",
            (emp_id, db_id),
        )
        for table_name in table_names:
            cur.execute("""
                INSERT INTO emp_table_access (emp_id, db_id, table_name)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING;
            """, (emp_id, db_id, table_name))


# ── Main ──────────────────────────────────────────────────────────────────────

def run(env_path: str = DEFAULT_ENV_PATH, apply: bool = False) -> None:
    logger = get_logger(__name__)
    load_dotenv(env_path)

    db_name = os.getenv("POSTGRES_DB_NAME")

    logger.info("Connecting to target DB (%s)...", db_name)
    target_conn = get_connection(env_path=env_path)

    logger.info("Connecting to metadata DB...")
    meta_conn = get_metadata_connection(env_path=env_path)

    db_id = get_db_id(meta_conn, db_name)
    logger.info("db_id for '%s' = %d", db_name, db_id)

    # ── Step 1: table names ───────────────────────────────────────────────────
    table_names = list_table_names(target_conn)
    logger.info("Found %d tables in target DB", len(table_names))

    # ── Step 2: active privileges ─────────────────────────────────────────────
    privileges = fetch_privileges(target_conn)
    logger.info("Found %d active privileges", len(privileges))

    # ── Step 3: deduplicate by prefix ─────────────────────────────────────────
    # prefix → list of privilege dicts sharing that prefix
    prefix_to_privs: dict[str, list[dict]] = defaultdict(list)
    for priv in privileges:
        prefix = extract_prefix(priv["privilege_code"])
        prefix_to_privs[prefix].append(priv)

    logger.info("Found %d unique module prefixes", len(prefix_to_privs))

    # ── Step 4: fuzzy match ───────────────────────────────────────────────────
    # privilege_id → best matching table_name (or None)
    priv_id_to_table: dict[int, tuple[str, float] | None] = {}
    unmatched: list[str] = []

    print("\n── Privilege → Table Matching ───────────────────────────────────────")
    for prefix, privs in sorted(prefix_to_privs.items()):
        matches = match_prefix_to_tables(prefix, table_names)
        priv_codes = ", ".join(p["privilege_code"] for p in privs)

        if matches:
            best_table, best_score = matches[0]
            confidence = "HIGH" if best_score >= 0.85 else "MED "
            print(f"  [{confidence}] {prefix:35s} → {best_table} ({best_score:.2f})")
            if len(matches) > 1:
                for alt_table, alt_score in matches[1:3]:
                    print(f"  {'     ':35s}     {alt_table} ({alt_score:.2f})")
            for priv in privs:
                priv_id_to_table[priv["privilege_id"]] = (best_table, best_score)
        else:
            unmatched.append(prefix)
            print(f"  [NONE] {prefix:35s} → no match found")
            for priv in privs:
                priv_id_to_table[priv["privilege_id"]] = None

    matched_count = sum(1 for v in priv_id_to_table.values() if v is not None)
    print(f"\n  Matched:   {matched_count} / {len(privileges)} privileges")
    print(f"  Unmatched prefixes ({len(unmatched)}): {unmatched}")
    print("─────────────────────────────────────────────────────────────────────\n")

    if not apply:
        logger.info("Dry run complete — pass --apply to write to metadata DB")
        target_conn.close()
        meta_conn.close()
        return

    # ── Step 5: store privilege_table_access ──────────────────────────────────
    logger.info("Storing privilege_table_access...")
    stored = 0
    for priv_id, match in priv_id_to_table.items():
        if match:
            table_name, score = match
            store_privilege_table_access(meta_conn, db_id, priv_id, table_name, score)
            stored += 1
    meta_conn.commit()
    logger.info("  Stored %d privilege → table mappings", stored)

    # ── Step 6: resolve and store emp_table_access ────────────────────────────
    emp_ids = fetch_all_emp_ids(target_conn)
    logger.info("Found %d employees with active roles", len(emp_ids))

    if not emp_ids:
        logger.warning("emp_roles is empty — no employee access stored (re-run --apply after roles are assigned)")
    else:
        for emp_id in emp_ids:
            priv_ids = fetch_emp_privilege_ids(target_conn, emp_id)
            allowed_tables = set()
            for priv_id in priv_ids:
                match = priv_id_to_table.get(priv_id)
                if match:
                    allowed_tables.add(match[0])
            store_emp_table_access(meta_conn, db_id, emp_id, list(allowed_tables))
            logger.debug("  emp_id=%d → %d tables", emp_id, len(allowed_tables))
        meta_conn.commit()
        logger.info("Stored table access for %d employees", len(emp_ids))

    target_conn.close()
    meta_conn.close()
    logger.info("Privilege sync complete.")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    env_args = [a for a in sys.argv[1:] if not a.startswith("--")]
    env = env_args[0] if env_args else DEFAULT_ENV_PATH
    run(env_path=env, apply=apply)
