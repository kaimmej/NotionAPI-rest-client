"""Minimal minion: prints a greeting and logs the run to Notion."""

from notion_minions import NotionMinion


class HelloMinion(NotionMinion):
    def __init__(self):
        super().__init__(
            service_key="hello_minion",
            service_display_name="Hello Minion",
        )

    def _do_work(self) -> None:
        print("Hello from a self-logging minion!")
        self._append_run_note("Said hello.")


if __name__ == "__main__":
    HelloMinion().run()
