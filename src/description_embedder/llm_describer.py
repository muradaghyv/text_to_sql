"""
Generates table and column descriptions using an LLM (vLLM / OpenAI-compatible).

One API call per table: sends the table name, column names, types, and FK
hints, and expects a JSON response with a table description and per-column
descriptions.

Two output languages are supported:
    "az" — Azerbaijani (default — the project's primary deployment audience)
    "en" — English

Thinking mode is disabled via extra_body for Qwen3 models so we get fast,
direct output.
"""
import json
import re

from openai import OpenAI


# ── Prompts: English ──────────────────────────────────────────────────────────

_SYSTEM_PROMPT_EN = (
    "You are a database documentation assistant. "
    "When given a table name and column list, respond with ONLY valid JSON — "
    "no markdown fences, no explanation, no extra text."
)

_USER_TEMPLATE_EN = """\
Table: {table_name}
Columns:
{column_lines}

Return this exact JSON structure:
{{
  "table_description": "<one sentence: what records this table stores>",
  "columns": {{
    "<column_name>": "<brief phrase describing the column>",
    ...
  }}
}}
"""

# ── Prompts: Azerbaijani ──────────────────────────────────────────────────────

_SYSTEM_PROMPT_AZ = (
    "Sən verilənlər bazası sənədləşmə köməkçisisən. "
    "Cədvəl adı və sütunlar verildikdə YALNIZ etibarlı JSON cavabı qaytar — "
    "markdown bloku, izahat və ya əlavə mətn yoxdur. "
    "Bütün təsvirləri Azərbaycan dilində yaz."
)

_USER_TEMPLATE_AZ = """\
Cədvəl: {table_name}
Sütunlar:
{column_lines}

Tam olaraq bu JSON strukturunu qaytar:
{{
  "table_description": "<bir cümlə: bu cədvəl hansı qeydləri saxlayır>",
  "columns": {{
    "<sütun_adı>": "<sütunu təsvir edən qısa ifadə>",
    ...
  }}
}}
"""

_PROMPTS = {
    "en": (_SYSTEM_PROMPT_EN, _USER_TEMPLATE_EN),
    "az": (_SYSTEM_PROMPT_AZ, _USER_TEMPLATE_AZ),
}


def _build_column_lines(columns: list[dict], col_fk_map: dict[str, tuple]) -> str:
    lines = []
    for col in columns:
        flags = []
        if col.get('is_primary_key'):
            flags.append("PK")
        if col['name'] in col_fk_map:
            tgt_table, tgt_col = col_fk_map[col['name']]
            flags.append(f"FK→{tgt_table}.{tgt_col}")
        if col.get('is_nullable') in (False, 'NO'):
            flags.append("NOT NULL")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"  {col['name']} ({col['data_type']}){flag_str}")
    return "\n".join(lines)


def _parse_response(text: str) -> dict | None:
    """Parse JSON from LLM response. Returns None if unparseable."""
    text = re.sub(r'^```(?:json)?\s*', '', text.strip())
    text = re.sub(r'\s*```$', '', text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


class LLMDescriber:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "dummy",
        lang: str = "az",
    ):
        """
        base_url — vLLM endpoint, e.g. "http://1.2.3.4:8000/v1"
        model    — model name as registered in vLLM
        api_key  — vLLM accepts any non-empty string
        lang     — "az" (default) or "en". Controls the system prompt and the
                   language the model is asked to write descriptions in.
        """
        if lang not in _PROMPTS:
            raise ValueError(f"Unsupported lang {lang!r}; choose from {list(_PROMPTS)}")
        self._client = OpenAI(base_url=base_url, api_key=api_key)
        self._model  = model
        self._system_prompt, self._user_template = _PROMPTS[lang]
        self._lang = lang

    def describe_table(
        self,
        table_name: str,
        columns: list[dict],
        col_fk_map: dict[str, tuple],
    ) -> dict | None:
        """
        Call LLM for one table.

        Returns {"table_description": str, "columns": {col_name: str}}
        or None if the call fails or the response cannot be parsed.
        """
        column_lines = _build_column_lines(columns, col_fk_map)
        user_msg = self._user_template.format(
            table_name=table_name,
            column_lines=column_lines,
        )

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.0,
                max_tokens=1024,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
            raw = response.choices[0].message.content or ""
        except Exception as exc:
            print(f"        [LLM error] {exc}")
            return None

        result = _parse_response(raw)
        if result is None:
            print(f"        [parse error] could not parse JSON for {table_name!r}")
            print(f"        raw: {raw[:300]!r}")
        return result
