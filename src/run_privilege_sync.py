"""
Privilege sync — for ERPHUB-style target DBs that expose `privileges`,
`emp_roles`, and `role_privileges` tables, this maps each privilege code to
the most likely table by fuzzy-matching on its module prefix, then resolves
each employee's roles → privileges → tables and writes the result to
`emp_table_access` in the metadata DB.

Public surface:
    sync_one_db(creds, meta_conn, db_id, apply_threshold=0.85)
        Run the full sync for one target DB whose credentials live in
        `registered_databases` (decrypted upstream). Used by the orchestrator.

Re-run policy:
    Each call truncates `emp_table_access` for the given db_id and re-inserts.
    Privilege rows that match below `apply_threshold` (default 0.85) are
    logged for review but not applied. Lower-confidence matches are visible
    so the admin can manually grant access if they trust the match.

Legacy CLI:
    `python run_privilege_sync.py [--apply]` — kept for ad-hoc dev. Reads
    target-DB credentials from POSTGRES_* env vars and assumes a single DB.
"""
import os
import sys
import difflib
from collections import defaultdict
from dataclasses import dataclass

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ENV_PATH = os.path.join(PROJECT_ROOT, "env", ".env")

from logger import get_logger
from metadata_store import get_metadata_connection, get_db_id_by_name


# Action suffixes to strip — longer ones first (order matters)
_ACTION_SUFFIXES = ["_VIEW_DETAIL", "_VIEW", "_ADD", "_EDIT", "_DELETE"]

# Below this score, matches are logged but not applied to emp_table_access.
_DEFAULT_APPLY_THRESHOLD = 0.85

# Below this score, matches are not even considered. Used for the
# "review" log so the admin sees medium-confidence rows.
_LOG_THRESHOLD = 0.6


# ── Credential dataclass ──────────────────────────────────────────────────────

@dataclass
class TargetDBCredentials:
    db_name: str
    host: str
    port: int
    schema_name: str
    db_user: str
    db_password: str


def _connect_target(creds: TargetDBCredentials) -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host=creds.host,
        port=creds.port,
        database=creds.db_name,
        user=creds.db_user,
        password=creds.db_password,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


# ── Fuzzy matching helpers ────────────────────────────────────────────────────

def extract_prefix(privilege_code: str) -> str:
    """Strip trailing action suffix to get the module prefix."""
    code = privilege_code.upper()
    for suffix in _ACTION_SUFFIXES:
        if code.endswith(suffix):
            return privilege_code[: -len(suffix)]
    return privilege_code


def normalize(s: str) -> str:
    return s.lower().replace("_", "").replace("&", "and").replace(" ", "")


def match_prefix_to_tables(prefix: str, table_names: list[str]) -> list[tuple[str, float]]:
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

        if score >= _LOG_THRESHOLD:
            results.append((table, round(score, 3)))

    return sorted(results, key=lambda x: -x[1])


# ── Target DB queries (ERPHUB schema assumed) ─────────────────────────────────

def _list_tables(target_conn) -> list[str]:
    with target_conn.cursor() as cur:
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name;
        """)
        return [r["table_name"] for r in cur.fetchall()]


def _fetch_privileges(target_conn) -> list[dict]:
    with target_conn.cursor() as cur:
        cur.execute("""
            SELECT privilege_id, privilege_code FROM privileges
            WHERE is_active = 1 AND is_archive = false
            ORDER BY privilege_id;
        """)
        return [{"privilege_id": r["privilege_id"], "privilege_code": r["privilege_code"]}
                for r in cur.fetchall()]


def _fetch_all_emp_ids(target_conn) -> list[int]:
    with target_conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT emp_id FROM emp_roles
            WHERE is_active = 1 AND is_archive = false;
        """)
        return [r["emp_id"] for r in cur.fetchall()]


def _fetch_emp_privilege_ids(target_conn, emp_id: int) -> list[int]:
    with target_conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT p.privilege_id
            FROM emp_roles er
            JOIN role_privileges rp
              ON rp.role_id = er.role_id
             AND rp.is_active = 1 AND rp.is_archive = false
            JOIN privileges p
              ON p.privilege_id = rp.privilege_id
             AND p.is_active = 1 AND p.is_archive = false
            WHERE er.emp_id = %s
              AND er.is_active = 1 AND er.is_archive = false;
        """, (emp_id,))
        return [r["privilege_id"] for r in cur.fetchall()]


# ── Metadata DB writes ────────────────────────────────────────────────────────

def _truncate_for_db(meta_conn, db_id: int) -> None:
    """Wipe both privilege_table_access and emp_table_access for one db_id."""
    with meta_conn.cursor() as cur:
        cur.execute("DELETE FROM privilege_table_access WHERE db_id = %s", (db_id,))
        cur.execute("DELETE FROM emp_table_access WHERE db_id = %s", (db_id,))
    meta_conn.commit()


def _store_privilege_table_access(meta_conn, db_id: int, privilege_id: int,
                                  table_name: str, score: float) -> None:
    with meta_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO privilege_table_access (privilege_id, db_id, table_name, match_score)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (privilege_id, db_id, table_name) DO UPDATE
                SET match_score = EXCLUDED.match_score;
        """, (privilege_id, db_id, table_name, score))


def _store_emp_table_access(meta_conn, db_id: int, emp_id: int, table_names: list[str]) -> None:
    with meta_conn.cursor() as cur:
        for table_name in table_names:
            cur.execute("""
                INSERT INTO emp_table_access (emp_id, db_id, table_name)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING;
            """, (emp_id, db_id, table_name))


# ── Public entry point ────────────────────────────────────────────────────────

def sync_one_db(
    creds: TargetDBCredentials,
    meta_conn,
    db_id: int,
    apply_threshold: float = _DEFAULT_APPLY_THRESHOLD,
) -> tuple[int, int, int]:
    """
    Run the full privilege-sync flow for one target DB.

    Returns (n_applied_mappings, n_unmatched_prefixes, n_employees_synced).
    Always truncates the existing privilege_table_access + emp_table_access
    rows for `db_id` before writing — re-running the sync gives a clean
    snapshot of the current ERPHUB role state.
    """
    logger = get_logger(__name__)

    logger.info("[priv-sync] '%s' — connecting to target DB", creds.db_name)
    target_conn = _connect_target(creds)

    try:
        table_names = _list_tables(target_conn)
        privileges = _fetch_privileges(target_conn)
        logger.info(
            "[priv-sync] '%s' — %d tables, %d active privileges",
            creds.db_name, len(table_names), len(privileges),
        )

        # Group privileges by their module prefix.
        prefix_to_privs: dict[str, list[dict]] = defaultdict(list)
        for priv in privileges:
            prefix_to_privs[extract_prefix(priv["privilege_code"])].append(priv)

        # Fuzzy-match each prefix.
        priv_id_to_match: dict[int, tuple[str, float] | None] = {}
        n_applied = 0
        n_unmatched = 0

        for prefix, privs in sorted(prefix_to_privs.items()):
            matches = match_prefix_to_tables(prefix, table_names)
            priv_codes = ", ".join(p["privilege_code"] for p in privs)

            if not matches:
                n_unmatched += 1
                logger.warning(
                    "[priv-sync] [NO MATCH] %-35s (privs: %s)",
                    prefix, priv_codes,
                )
                for priv in privs:
                    priv_id_to_match[priv["privilege_id"]] = None
                continue

            best_table, best_score = matches[0]

            if best_score >= apply_threshold:
                logger.info(
                    "[priv-sync] [HIGH %.2f] %-35s → %s",
                    best_score, prefix, best_table,
                )
                for priv in privs:
                    priv_id_to_match[priv["privilege_id"]] = (best_table, best_score)
                    n_applied += 1
            else:
                logger.warning(
                    "[priv-sync] [LOW  %.2f] %-35s ~ %s (skipped — below %.2f)",
                    best_score, prefix, best_table, apply_threshold,
                )
                for priv in privs:
                    priv_id_to_match[priv["privilege_id"]] = None

        # Wipe old rows and write fresh ones.
        _truncate_for_db(meta_conn, db_id)

        for priv_id, match in priv_id_to_match.items():
            if match is not None:
                table_name, score = match
                _store_privilege_table_access(meta_conn, db_id, priv_id, table_name, score)
        meta_conn.commit()

        # Resolve emp_id → tables and write emp_table_access.
        emp_ids = _fetch_all_emp_ids(target_conn)
        logger.info("[priv-sync] '%s' — %d employees with active roles",
                    creds.db_name, len(emp_ids))

        for emp_id in emp_ids:
            priv_ids = _fetch_emp_privilege_ids(target_conn, emp_id)
            allowed_tables = set()
            for pid in priv_ids:
                m = priv_id_to_match.get(pid)
                if m is not None:
                    allowed_tables.add(m[0])
            _store_emp_table_access(meta_conn, db_id, emp_id, list(allowed_tables))
        meta_conn.commit()

        logger.info(
            "[priv-sync] '%s' — done: %d applied mappings, %d unmatched prefixes, %d employees",
            creds.db_name, n_applied, n_unmatched, len(emp_ids),
        )
        return n_applied, n_unmatched, len(emp_ids)
    finally:
        target_conn.close()


# ── Legacy CLI: env-driven, single DB. Kept for ad-hoc local dev. ─────────────

def _run_from_env(env_path: str = DEFAULT_ENV_PATH, apply: bool = False) -> None:
    logger = get_logger(__name__)
    load_dotenv(env_path)

    creds = TargetDBCredentials(
        db_name     = os.getenv("POSTGRES_DB_NAME"),
        host        = os.getenv("DATABASE_IP"),
        port        = int(os.getenv("DATABASE_PORT", "5432")),
        schema_name = "public",
        db_user     = os.getenv("POSTGRES_USER"),
        db_password = os.getenv("POSTGRES_PASSWORD"),
    )

    meta_conn = get_metadata_connection(env_path=env_path)
    try:
        db_id = get_db_id_by_name(meta_conn, creds.db_name)
        if db_id is None:
            raise ValueError(
                f"DB '{creds.db_name}' is not registered in registered_databases. "
                "Insert it first or use the new docker flow."
            )

        if not apply:
            logger.info(
                "Dry-run mode is no longer supported in the new flow. "
                "Pass --apply to run the sync (which truncates and re-inserts)."
            )
            return

        sync_one_db(creds, meta_conn, db_id)
    finally:
        meta_conn.close()


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    env_args = [a for a in sys.argv[1:] if not a.startswith("--")]
    env = env_args[0] if env_args else DEFAULT_ENV_PATH
    _run_from_env(env_path=env, apply=apply)
