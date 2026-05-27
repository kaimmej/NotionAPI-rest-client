import io
import sys
import logging
from datetime import datetime, timezone
from time import monotonic

from notion_minions import notion


class _TeeStream:
    """Write-through stream: forwards everything to a real stream AND a buffer."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for stream in self._streams:
            try:
                stream.write(s)
            except Exception:
                pass
        return len(s) if isinstance(s, str) else 0

    def flush(self):
        for stream in self._streams:
            try:
                stream.flush()
            except Exception:
                pass


class _BufferingHandler(logging.Handler):
    """Logging handler that pipes formatted records into a StringIO buffer."""

    def __init__(self, buffer: io.StringIO):
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._buffer.write(msg + "\n")
        except Exception:
            self.handleError(record)


class NotionMinion:
    """
    Base class for all minion services.
    Subclasses override `_do_work()` with their actual task logic.
    `run()` orchestrates timing, logging to Notion, and updating
    the "Date of last successful run" on success.
    """

    def __init__(self, *, service_key: str, service_display_name: str):
        self._service_key = service_key
        self._service_display_name = service_display_name
        self._logger = logging.getLogger(service_display_name)

        self._notion_minions = notion.NotionMinionClient()
        self._notion_logs = notion.NotionMinionLoggingClient()
        self._minion_relation_id = self._notion_minions._config["minions"][service_key]

        self._run_notes: list[str] = []
        self._run_had_errors: bool = False
        self._status_code: int = 200
        self._log_buffer: io.StringIO = io.StringIO()
        self._runtime_seconds: float = 0.0

    def _append_run_note(self, msg: str) -> None:
        if msg is None:
            return
        line = str(msg).strip()
        if not line:
            return
        if len(line) > 800:
            line = line[:800] + "…"
        self._run_notes.append(line)

    def _finalize_notes(self, *, runtime_seconds: float) -> str:
        status = "ERROR" if self._run_had_errors else "OK"
        runtime_str = f"{runtime_seconds:.3f}"
        lines = [f"Status: {status}", f"RuntimeSeconds: {runtime_str}"]
        lines.extend(self._run_notes)
        seen: set[str] = set()
        out: list[str] = []
        for line in lines:
            if line in seen:
                continue
            seen.add(line)
            out.append(line)
        return "\n".join(out).strip()

    def _do_work(self) -> None:
        """Subclasses override this with their actual task logic."""
        pass

    def run(self) -> None:
        self._run_notes = []
        self._run_had_errors = False
        self._status_code = 200
        self._log_buffer = io.StringIO()

        real_stdout = sys.stdout
        tee = _TeeStream(real_stdout, self._log_buffer)
        sys.stdout = tee

        log_handler = _BufferingHandler(self._log_buffer)
        log_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        log_handler.setLevel(logging.DEBUG)
        root_logger = logging.getLogger()
        previous_level = root_logger.level
        root_logger.addHandler(log_handler)
        if previous_level > logging.INFO or previous_level == logging.NOTSET:
            root_logger.setLevel(logging.INFO)

        self._logger.info(f"Running minion: {self._service_display_name}")
        start = monotonic()

        try:
            try:
                self._do_work()
            except Exception as e:
                self._run_had_errors = True
                self._status_code = 500
                self._append_run_note(f"_do_work raised: {e}")
                self._logger.error(f"_do_work raised: {e}")
        finally:
            self._runtime_seconds = max(0.0, monotonic() - start)
            sys.stdout = real_stdout
            root_logger.removeHandler(log_handler)
            root_logger.setLevel(previous_level)

        runtime_seconds_value = round(self._runtime_seconds, 3)
        today = datetime.now(timezone.utc)
        log_name = f"{self._service_display_name} - {today.strftime('%B %-d, %Y')}"

        page_id = None
        try:
            page_id = self._notion_logs.create_log_entry(
                name=log_name,
                minion_relation_id=self._minion_relation_id,
                date_iso=today.isoformat(),
                error_status="ERROR" if self._run_had_errors else None,
                status_code=str(self._status_code),
                notes=self._finalize_notes(runtime_seconds=self._runtime_seconds),
                runtime_seconds=runtime_seconds_value,
            )
        except Exception as e:
            self._logger.error(f"Failed to write Notion log entry: {e}")

        if page_id:
            try:
                log_text = self._log_buffer.getvalue().rstrip() or "(no output captured)"
                self._notion_logs.populate_log_codeblock(
                    page_id, log_text, has_errors=self._run_had_errors
                )
            except Exception as e:
                self._logger.error(f"Failed to populate LOGS code block: {e}")

        if not self._run_had_errors:
            try:
                self._notion_minions.update_last_successful_run(
                    self._minion_relation_id, today.isoformat()
                )
            except Exception as e:
                self._logger.error(f"Failed to update last successful run: {e}")

        self._logger.info(
            f"Minion finished: {self._service_display_name} "
            f"({'ERROR' if self._run_had_errors else 'OK'}) "
            f"in {self._runtime_seconds:.2f}s"
        )
