"""
Generates human-readable descriptions for tables and their columns.

All descriptions are derived purely from structure: table name, column names,
data types, constraints, and FK relationships. No LLM required.

snake_case identifiers are split into plain words so the embedding model
sees natural language rather than raw database identifiers.

Main output: build_embedding_text() — a single structured text blob per
table that is passed to the BGE-M3 embedding model.
"""
import re


# ── Utilities ─────────────────────────────────────────────────────────────────

def snake_to_words(name: str) -> str:
    """
    Convert snake_case or camelCase identifier to space-separated words.

    Examples:
        employee_contact_info → employee contact info
        createdAt             → created at
        ERPHubUser            → ERP hub user
    """
    # split on camelCase boundaries
    name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)
    name = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])', ' ', name)
    # replace underscores / hyphens
    name = name.replace('_', ' ').replace('-', ' ')
    # collapse multiple spaces
    return re.sub(r'\s+', ' ', name).strip().lower()


# ── Column description ────────────────────────────────────────────────────────

# Common suffix → human label mappings
_SUFFIX_HINTS: list[tuple[str, str]] = [
    ('_at',      'timestamp'),
    ('_date',    'date'),
    ('_time',    'time'),
    ('_count',   'count'),
    ('_total',   'total amount'),
    ('_amount',  'monetary amount'),
    ('_price',   'price'),
    ('_rate',    'rate'),
    ('_url',     'URL'),
    ('_path',    'file path'),
    ('_code',    'code'),
    ('_hash',    'hash'),
    ('_token',   'token'),
    ('_key',     'key'),
    ('_flag',    'boolean flag'),
    ('_order',   'sort order'),
    ('_index',   'index'),
]

_EXACT_HINTS: dict[str, str] = {
    'id':          'primary identifier',
    'uuid':        'unique identifier (UUID)',
    'name':        'display name',
    'title':       'title',
    'description': 'text description',
    'notes':       'freeform notes',
    'status':      'status classification',
    'state':       'state classification',
    'type':        'type classification',
    'kind':        'kind classification',
    'category':    'category classification',
    'is_active':   'boolean — record is active',
    'is_deleted':  'boolean — soft-delete flag',
    'is_enabled':  'boolean — feature is enabled',
    'active':      'boolean — record is active',
    'enabled':     'boolean — feature is enabled',
    'deleted':     'boolean — soft-delete flag',
    'created_at':  'timestamp when the record was created',
    'updated_at':  'timestamp when the record was last updated',
    'deleted_at':  'timestamp when the record was soft-deleted',
    'created_by':  'user who created the record',
    'updated_by':  'user who last updated the record',
    'email':       'email address',
    'phone':       'phone number',
    'address':     'physical address',
    'latitude':    'geographic latitude',
    'longitude':   'geographic longitude',
    'password':    'hashed password',
    'token':       'authentication token',
    'avatar':      'profile picture URL',
    'thumbnail':   'thumbnail image URL',
    'image':       'image URL',
    'file':        'file reference',
    'currency':    'currency code (ISO 4217)',
    'locale':      'locale code',
    'timezone':    'timezone identifier',
    'ip_address':  'IP address',
    'user_agent':  'browser / client user-agent string',
    'sort_order':  'display sort order',
    'position':    'display position',
    'rank':        'rank or order',
    'priority':    'priority level',
    'weight':      'numeric weight or importance',
    'metadata':    'arbitrary JSON metadata',
    'config':      'configuration JSON',
    'settings':    'settings JSON',
    'data':        'raw data payload',
    'payload':     'request/event payload',
    'response':    'response payload',
    'error':       'error message',
    'message':     'human-readable message',
    'comment':     'comment or note',
    'body':        'main text body',
    'content':     'main content',
    'html':        'HTML content',
    'json':        'JSON data',
    'xml':         'XML data',
    'slug':        'URL-friendly slug',
    'sku':         'stock-keeping unit code',
    'barcode':     'barcode value',
    'quantity':    'quantity',
    'stock':       'stock quantity',
    'balance':     'account balance',
    'score':       'numeric score',
    'rating':      'rating value',
    'version':     'version number',
    'revision':    'revision number',
    'sequence':    'sequence number',
    'index':       'index value',
    'level':       'hierarchy level',
    'depth':       'nesting depth',
    'width':       'width',
    'height':      'height',
    'size':        'size',
    'length':      'length',
    'duration':    'duration in seconds',
    'timeout':     'timeout in seconds',
    'attempts':    'number of attempts',
    'retries':     'number of retries',
    'start_date':  'start date',
    'end_date':    'end date',
    'due_date':    'due date',
    'birth_date':  'date of birth',
    'expire_at':   'expiration timestamp',
    'expired_at':  'expiration timestamp',
    'published_at':'publish timestamp',
    'first_name':  'first name',
    'last_name':   'last name',
    'full_name':   'full name',
    'middle_name': 'middle name',
    'gender':      'gender',
    'age':         'age',
    'salary':      'salary amount',
    'tax':         'tax amount',
    'discount':    'discount amount',
    'fee':         'fee amount',
    'cost':        'cost amount',
    'revenue':     'revenue amount',
    'profit':      'profit amount',
    'budget':      'budget amount',
    'limit':       'upper limit',
    'min':         'minimum value',
    'max':         'maximum value',
}


def describe_column(col: dict, col_fk_map: dict[str, tuple]) -> str:
    """
    Produce a short English description of a single column.

    col         — dict from columns_info JSONB
    col_fk_map  — {col_name: (target_table, target_col)} for this table's FKs
    """
    name = col['name']

    # FK reference takes priority
    if name in col_fk_map:
        tgt_table, tgt_col = col_fk_map[name]
        return f"foreign key referencing {snake_to_words(tgt_table)}.{tgt_col}"

    # Primary key
    if col.get('is_primary_key'):
        return "primary key"

    # Exact name match
    if name in _EXACT_HINTS:
        return _EXACT_HINTS[name]

    # Suffix heuristics
    for suffix, label in _SUFFIX_HINTS:
        if name.endswith(suffix):
            prefix = snake_to_words(name[: -len(suffix)]).strip()
            return f"{prefix} {label}".strip() if prefix else label

    # Fallback: humanise the identifier
    return snake_to_words(name)


# ── Table description ─────────────────────────────────────────────────────────

def generate_table_description(table_name: str) -> str:
    """One-sentence description of a table derived from its name."""
    return f"Stores {snake_to_words(table_name)} records"


# ── Embedding text blob ───────────────────────────────────────────────────────

def build_embedding_text(
    table_name: str,
    table_description: str,
    columns: list[dict],
    col_fk_map: dict[str, tuple],
    related_tables: list[str],
) -> str:
    """
    Build the full text blob for one table.

    This is the exact string that gets fed to the embedding model.
    The format deliberately mirrors natural language so BGE-M3 retrieves
    it accurately when the user asks a question mentioning these concepts.

    Example output:
        Table: employee_contacts
        Description: Stores employee contacts records
        Columns:
          id (integer) [PK]: primary key
          employee_id (integer) [FK→employees.id]: foreign key referencing employees.id
          contact_type (character varying) [NOT NULL]: contact type classification
          contact_value (character varying) [NOT NULL]: contact value
          created_at (timestamp without time zone): timestamp when the record was created
        Related tables: employees
    """
    lines = [
        f"Table: {table_name}",
        f"Description: {table_description}",
        "Columns:",
    ]

    for col in columns:
        flags = []
        if col.get('is_primary_key'):
            flags.append("PK")
        if col['name'] in col_fk_map:
            tgt_table, tgt_col = col_fk_map[col['name']]
            flags.append(f"FK→{tgt_table}.{tgt_col}")
        if col.get('is_unique') and not col.get('is_primary_key'):
            flags.append("UNIQUE")
        if col.get('is_nullable') in (False, 'NO'):
            flags.append("NOT NULL")

        flag_str = f" [{', '.join(flags)}]" if flags else ""
        col_desc = describe_column(col, col_fk_map)
        lines.append(f"  {col['name']} ({col['data_type']}){flag_str}: {col_desc}")

    if related_tables:
        lines.append(f"Related tables: {', '.join(related_tables)}")

    return "\n".join(lines)


def enrich_columns_with_descriptions(
    columns: list[dict],
    col_fk_map: dict[str, tuple],
) -> list[dict]:
    """
    Return a new list of column dicts, each with an added 'description' key.
    This is stored back into the columns_info JSONB in table_metadata.
    """
    enriched = []
    for col in columns:
        enriched_col = dict(col)
        enriched_col['description'] = describe_column(col, col_fk_map)
        enriched.append(enriched_col)
    return enriched
