"""
Example: a cleanup minion that queries a Notion database for "done" items
older than a threshold, then batch-archives them.

This is a generic template — adapt the filter, property names, and
data-source ID to match your own Notion database.

Setup:
  1. Add a "cleanup_target_data_source_id" key to the "databases" block
     in notion_minions/config/notion_config.json.
  2. Register the minion under "minions" with key "cleanup_minion".
  3. Run:  python -m examples.cleanup_minion
"""

from datetime import datetime, timedelta, timezone

from notion_minions import NotionMinion
from notion_minions.notion import Notion


class CleanupMinion(NotionMinion):
    """Archive checked-off items that haven't been touched in a while."""

    DONE_PROPERTY = "Done"
    MIN_AGE_DAYS = 14

    def __init__(self):
        super().__init__(
            service_key="cleanup_minion",
            service_display_name="Cleanup Minion",
        )
        self._client = Notion()
        self._data_source_id = self._client._config["databases"][
            "cleanup_target_data_source_id"
        ]

    def _do_work(self) -> None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self.MIN_AGE_DAYS)
        ).isoformat()

        filt = {
            "and": [
                {"property": self.DONE_PROPERTY, "checkbox": {"equals": True}},
                {
                    "timestamp": "last_edited_time",
                    "last_edited_time": {"before": cutoff},
                },
            ]
        }

        pages = self._client.NOTION_query_data_source(
            self._data_source_id, filter=filt
        )
        self._logger.info(f"Found {len(pages)} done item(s) older than {self.MIN_AGE_DAYS} days")

        if not pages:
            self._append_run_note("Nothing to archive.")
            return

        page_ids = [p["id"] for p in pages]
        ok, fail = self._client.NOTION_archive_pages(page_ids)
        self._append_run_note(f"Archived {ok}, failed {fail}")
        self._logger.info(f"Archive results: {ok} ok, {fail} failed")

        if fail:
            self._run_had_errors = True
            self._status_code = 207


if __name__ == "__main__":
    CleanupMinion().run()
