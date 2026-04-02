"""
Generates table and column descriptions using an LLM (vLLM / OpenAI-compatible endpoint).

One API call per table: sends the table name, column names, types, and FK hints,
and expects a JSON response with a table description and per-column descriptions.

Thinking mode is disabled via extra_body for Qwen3 models to get fast, direct output.
"""
import json
import re

from openai import OpenAI


_SYSTEM_PROMPT = (
    "You are a database documentation assistant. "
    "When given a table name and column list, respond with ONLY valid JSON — "
    "no markdown fences, no explanation, no extra text."
)

_USER_TEMPLATE = """\
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
    # strip markdown code fences if present
    text = re.sub(r'^```(?:json)?\s*', '', text.strip())
    text = re.sub(r'\s*```$', '', text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


class LLMDescriber:
    def __init__(self, base_url: str, model: str, api_key: str = "dummy"):
        """
        base_url — vLLM endpoint, e.g. "http://1.2.3.4:8000/v1"
        model    — model name as registered in vLLM
        api_key  — vLLM accepts any non-empty string
        """
        self._client = OpenAI(base_url=base_url, api_key=api_key)
        self._model = model

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
        user_msg = _USER_TEMPLATE.format(
            table_name=table_name,
            column_lines=column_lines,
        )

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
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
