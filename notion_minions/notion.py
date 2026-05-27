import json
import os
import time
import logging
from pathlib import Path
from typing import Any, Optional

import requests
from requests import Response
from requests.exceptions import RequestException
from dotenv import load_dotenv

load_dotenv()

_CONFIG_DIR = Path(__file__).resolve().parent / "config"


def _load_config() -> dict:
    with open(_CONFIG_DIR / "notion_config.json") as f:
        return json.load(f)


_MAX_RICH_TEXT_ITEMS = 100
_CHUNK_SIZE = 1900


def _chunk_text(text: str, *, max_items: int = _MAX_RICH_TEXT_ITEMS) -> list[str]:
    """Split *text* into <=1900-char chunks, capped at *max_items*.

    If truncation is needed the last chunk is replaced with a notice.
    """
    remaining = text or ""
    chunks: list[str] = []
    while remaining:
        chunks.append(remaining[:_CHUNK_SIZE])
        remaining = remaining[_CHUNK_SIZE:]
    if not chunks:
        return [""]
    if len(chunks) > max_items:
        kept = max_items - 1
        truncated_chars = sum(len(c) for c in chunks[kept:])
        chunks = chunks[:kept]
        chunks.append(f"\n\n... truncated ({truncated_chars:,} chars omitted, "
                       f"rich_text limit is {max_items} items) ...")
    return chunks


class Notion:

    def __init__(self):
        self.NOTION_TOKEN = os.environ["NOTION_TOKEN"]
        self._config = _load_config()

        self.DATABASE_ID: str = ""

        self.headers = {
            "Authorization": "Bearer " + self.NOTION_TOKEN,
            "Content-Type": "application/json",
            "Notion-Version": "2026-03-11",
        }
        self._logger = logging.getLogger("NotionClient")

    # ----------------------------
    # HTTP helpers (requests)
    # ----------------------------
    @staticmethod
    def _request_with_retry(
        method: str,
        url: str,
        *,
        headers: dict,
        json: Any | None = None,
        timeout: tuple[float, float] = (5.0, 20.0),
        max_attempts: int = 3,
        backoff_seconds: float = 0.8,
        retry_statuses: set[int] | None = None,
    ) -> Response:
        """Make an HTTP request with explicit timeouts and bounded retry."""
        retry_statuses = retry_statuses or {429, 500, 502, 503, 504}
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                resp = requests.request(method, url, headers=headers, json=json, timeout=timeout)
                if resp.status_code in retry_statuses and attempt < max_attempts:
                    retry_after = resp.headers.get("Retry-After")
                    sleep_s = None
                    if retry_after:
                        try:
                            sleep_s = float(retry_after)
                        except Exception:
                            sleep_s = None
                    time.sleep(max(0.0, sleep_s if sleep_s is not None else backoff_seconds * attempt))
                    continue
                return resp
            except RequestException as e:
                last_exc = e
                if attempt >= max_attempts:
                    raise
                time.sleep(backoff_seconds * attempt)

        if last_exc:
            raise last_exc
        raise RuntimeError("request failed without exception")

    # ----------------------------
    # Data formatters
    # ----------------------------
    def HELPER_data_formatter_TITLE(self, title: str):
        return {"title": [{"text": {"content": title}}]}

    def HELPER_data_formatter_TEXT(self, text: str):
        return {"rich_text": [{"text": {"content": text}}]}

    def HELPER_data_formatter_DATE(self, date_start: str, date_end: str = None):
        return {"date": {"start": date_start, "end": date_end}}

    def HELPER_data_formatter_SELECT(self, name: str):
        return {"select": {"name": name}}

    def HELPER_data_formatter_NUMBER(self, number: float | int):
        return {"number": number}

    def HELPER_data_formatter_RELATION(self, relation_id: str):
        return {"relation": [{"id": relation_id}]}

    def HELPER_data_formatter_ICONS(self, icon_emoji: str):
        return {"type": "emoji", "emoji": icon_emoji}

    # ----------------------------
    # Notion API wrappers
    # ----------------------------
    def NOTION_create_page(self, data: dict, *, icon: dict | None = None):
        self._logger.info("NOTION_create_page()")
        create_url = "https://api.notion.com/v1/pages"
        payload: dict[str, Any] = {"parent": {"database_id": self.DATABASE_ID}, "properties": data}
        if icon:
            payload["icon"] = icon
        response = self._request_with_retry("POST", create_url, headers=self.headers, json=payload)
        if response.status_code != 200:
            self._logger.error(f"Error response: {response.text}")
        return response

    def NOTION_update_page_properties(self, page_id: str, properties: dict):
        if not page_id:
            raise ValueError("page_id is required")
        url = f"https://api.notion.com/v1/pages/{page_id}"
        payload = {"properties": properties}
        resp = self._request_with_retry("PATCH", url, headers=self.headers, json=payload)
        if resp.status_code != 200:
            self._logger.error(f"Error response: {resp.text}")
        return resp

    # ----------------------------
    # Block helpers
    # ----------------------------
    def NOTION_list_block_children(self, block_id: str) -> list[dict]:
        url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
        resp = self._request_with_retry("GET", url, headers=self.headers)
        if resp.status_code != 200:
            self._logger.error(f"Error listing children: {resp.text}")
            return []
        return resp.json().get("results", []) or []

    def NOTION_update_code_block(self, block_id: str, text: str, *, language: str = "plain text"):
        """
        Replace the contents of a `code` block. Notion caps each rich_text item at 2000 chars
        and the array at 100 items, so we chunk and truncate via `_chunk_text`.
        """
        rich_text = [{"type": "text", "text": {"content": c}} for c in _chunk_text(text)]

        url = f"https://api.notion.com/v1/blocks/{block_id}"
        payload = {"code": {"rich_text": rich_text, "language": language}}
        resp = self._request_with_retry("PATCH", url, headers=self.headers, json=payload)
        if resp.status_code != 200:
            self._logger.error(f"Error updating code block: {resp.text}")
        return resp

    def NOTION_append_block_children(self, parent_block_id: str, children: list[dict]):
        url = f"https://api.notion.com/v1/blocks/{parent_block_id}/children"
        payload = {"children": children}
        resp = self._request_with_retry("PATCH", url, headers=self.headers, json=payload)
        if resp.status_code != 200:
            self._logger.error(f"Error appending children: {resp.text}")
        return resp

    # ----------------------------
    # Database / data source helpers
    # ----------------------------
    def NOTION_get_database(self, database_id: str) -> dict:
        """GET /v1/databases/{id}. Used (among other things) to resolve a database's data_source ids."""
        url = f"https://api.notion.com/v1/databases/{database_id}"
        resp = self._request_with_retry("GET", url, headers=self.headers)
        if resp.status_code != 200:
            self._logger.error(f"Error fetching database {database_id}: {resp.text}")
            raise RuntimeError(f"Failed to fetch database {database_id} (HTTP {resp.status_code})")
        self._logger.info(f"Successfully fetched database {database_id}")
        return resp.json()

    def NOTION_query_data_source(
        self,
        data_source_id: str,
        *,
        filter: dict | None = None,
        sorts: list[dict] | None = None,
        page_size: int = 100,
    ) -> list[dict]:
        """
        POST /v1/data_sources/{id}/query with auto-pagination. Returns the flat list of result pages.
        Raises RuntimeError on the first non-200 response.
        """
        url = f"https://api.notion.com/v1/data_sources/{data_source_id}/query"
        results: list[dict] = []
        start_cursor: str | None = None

        while True:
            payload: dict[str, Any] = {"page_size": page_size}
            if filter is not None:
                payload["filter"] = filter
            if sorts is not None:
                payload["sorts"] = sorts
            if start_cursor is not None:
                payload["start_cursor"] = start_cursor

            resp = self._request_with_retry("POST", url, headers=self.headers, json=payload)
            if resp.status_code != 200:
                self._logger.error(f"Error querying data source {data_source_id}: {resp.text}")
                raise RuntimeError(
                    f"Data source query failed (HTTP {resp.status_code}): {resp.text}"
                )

            body = resp.json() if resp.content else {}
            results.extend(body.get("results", []) or [])
            if not body.get("has_more"):
                self._logger.info(
                    f"Successfully queried data source {data_source_id}: {len(results)} result(s)"
                )
                return results
            start_cursor = body.get("next_cursor")
            if not start_cursor:
                self._logger.info(
                    f"Successfully queried data source {data_source_id}: {len(results)} result(s)"
                )
                return results

    def NOTION_archive_page(self, page_id: str) -> Response:
        """PATCH /v1/pages/{id} with in_trash=true. Moves the page to Notion trash (recoverable 30 days)."""
        if not page_id:
            raise ValueError("page_id is required")
        url = f"https://api.notion.com/v1/pages/{page_id}"
        payload = {"in_trash": True}
        resp = self._request_with_retry("PATCH", url, headers=self.headers, json=payload)
        if resp.status_code != 200:
            self._logger.error(f"Error archiving page {page_id}: {resp.text}")
        return resp

    def NOTION_archive_pages(
        self,
        page_ids: list[str],
        *,
        batch_size: int = 20,
        per_request_delay: float = 0.35,
        batch_pause: float = 2.0,
    ) -> tuple[int, int]:
        """Archive pages in batches to respect Notion's rate limits.

        Processes *batch_size* pages, pausing *per_request_delay* seconds between
        each PATCH and *batch_pause* seconds between batches. Returns (ok, fail).
        """
        ok = fail = 0
        total = len(page_ids)
        for i, pid in enumerate(page_ids):
            if i > 0 and i % batch_size == 0:
                self._logger.info(
                    f"Archived {i}/{total} so far ({ok} ok, {fail} failed), "
                    f"pausing {batch_pause}s before next batch"
                )
                time.sleep(batch_pause)
            resp = self.NOTION_archive_page(pid)
            if getattr(resp, "status_code", None) == 200:
                ok += 1
            else:
                fail += 1
            if per_request_delay > 0 and i < total - 1:
                time.sleep(per_request_delay)
        return ok, fail

    def NOTION_get_data_source(self, data_source_id: str) -> dict:
        """GET /v1/data_sources/{id}. Returns the data source object (includes property schema)."""
        url = f"https://api.notion.com/v1/data_sources/{data_source_id}"
        resp = self._request_with_retry("GET", url, headers=self.headers)
        if resp.status_code != 200:
            self._logger.error(f"Error fetching data source {data_source_id}: {resp.text}")
            raise RuntimeError(f"Failed to fetch data source {data_source_id} (HTTP {resp.status_code})")
        self._logger.info(f"Successfully fetched data source {data_source_id}")
        return resp.json()


class NotionMinionClient(Notion):
    """Writes to the main Minions database (updates 'Date of last successful run')."""

    def __init__(self):
        super().__init__()
        self.DATABASE_ID = self._config["databases"]["minion_db_id"]
        self._logger = logging.getLogger("MINION DB")

    def update_last_successful_run(self, minion_page_id: str, when_iso: str):
        properties = {
            "Date of last successful run": self.HELPER_data_formatter_DATE(when_iso),
        }
        return self.NOTION_update_page_properties(minion_page_id, properties)


class NotionMinionLoggingClient(Notion):
    """Writes log entries to the Minion logs database using the 'minion log' template."""

    def __init__(self):
        super().__init__()
        self.DATABASE_ID = self._config["databases"]["minion_logging_db_id"]
        self._data_source_id = self._config["databases"]["minion_logging_data_source_id"]
        self._template_id = self._config["databases"]["minion_logging_template_id"]
        self._logger = logging.getLogger("MINION LOG DB")

    def create_log_entry(
        self,
        *,
        name: str,
        minion_relation_id: str,
        date_iso: str,
        error_status: Optional[str] = None,
        status_code: str = "200",
        notes: str = "",
        runtime_seconds: float = 0.0,
    ) -> Optional[str]:
        """
        Create a log page from the 'minion log' template (small text + full width + LOGS code block).
        When a template is used, `children` is forbidden, so the page body is populated by
        Notion asynchronously. Returns the new page id, or None on failure.
        """
        self._logger.info("create_log_entry()")
        properties: dict[str, Any] = {
            "Name": self.HELPER_data_formatter_TITLE(name),
            "Minion Service": self.HELPER_data_formatter_RELATION(minion_relation_id),
            "Date": self.HELPER_data_formatter_DATE(date_iso),
            "StatusCode": self.HELPER_data_formatter_TEXT(status_code),
            "Runtime (Seconds)": self.HELPER_data_formatter_NUMBER(runtime_seconds),
        }
        if notes:
            properties["Notes"] = self.HELPER_data_formatter_TEXT(notes)
        if error_status:
            properties["Error"] = self.HELPER_data_formatter_SELECT(error_status)

        icon = self.HELPER_data_formatter_ICONS("🚧" if error_status else "✅")

        payload: dict[str, Any] = {
            "parent": {"type": "data_source_id", "data_source_id": self._data_source_id},
            "properties": properties,
            "icon": icon,
            "template": {"type": "template_id", "template_id": self._template_id},
        }
        resp = self._request_with_retry("POST", "https://api.notion.com/v1/pages", headers=self.headers, json=payload)
        if resp.status_code != 200:
            self._logger.error(f"Error creating templated log page: {resp.text}")
            return None
        try:
            return resp.json().get("id")
        except Exception:
            return None

    def populate_log_codeblock(
        self, page_id: str, log_text: str, *, has_errors: bool = False
    ) -> bool:
        """
        Append a `LOGS` heading + a code block containing `log_text` as children of the page.
        When *has_errors* is true, a callout block with bold orange error lines is prepended.
        The 'minion log' template's body is empty, so we always write the body ourselves; this
        avoids the async template-application race and is reliable on every run.
        """
        if not page_id or log_text is None:
            return False

        new_blocks: list[dict] = []

        if has_errors:
            error_lines = [
                line for line in log_text.splitlines() if "[ERROR]" in line
            ]
            if error_lines:
                error_chunks = _chunk_text("\n".join(error_lines))
                new_blocks.append({
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "icon": {"type": "emoji", "emoji": "⚠️"},
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": chunk},
                                "annotations": {"bold": True, "color": "orange"},
                            }
                            for chunk in error_chunks
                        ],
                    },
                })

        log_chunks = _chunk_text(log_text)
        new_blocks.extend([
            {
                "object": "block",
                "type": "heading_3",
                "heading_3": {
                    "rich_text": [{"type": "text", "text": {"content": "LOGS"}}],
                },
            },
            {
                "object": "block",
                "type": "code",
                "code": {
                    "rich_text": [{"type": "text", "text": {"content": c}} for c in log_chunks],
                    "language": "shell",
                },
            },
        ])
        resp = self.NOTION_append_block_children(page_id, new_blocks)
        return getattr(resp, "status_code", None) == 200
