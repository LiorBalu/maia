"""SimpleLogger: timestamped event log of one phone call session."""

import os
import json
import datetime


class SimpleLogger:
    """Writes the events of one phone call to call_logs/<conversation_id>.txt (transcript) and .json (all events)."""
    def __init__(self):
        """Create the call_logs folder and the two log files with a header."""
        self.start_time = datetime.datetime.now()
        self.conversation_id = (
            f"conversation_{self.start_time.strftime('%Y%m%d_%H%M%S')}"
        )

        # Create logs directory if it doesn't exist
        os.makedirs("call_logs", exist_ok=True)

        # Initialize log files
        self.text_log_path = f"call_logs/{self.conversation_id}.txt"
        self.json_log_path = f"call_logs/{self.conversation_id}.json"

        # Initialize conversation history
        self.conversation_history = {
            "conversation_id": self.conversation_id,
            "start_time": self.start_time.isoformat(),
            "events": [],  # For conversation events
            "technical_events": [],  # For important technical events
        }

        # Write initial log files
        self._create_transcript_file()
        self._persist_event_history()

    def _create_transcript_file(self):
        """Start the text log with the conversation id and the start time."""
        with open(self.text_log_path, "w", encoding="utf-8") as transcript_file:
            transcript_file.write(f"=== Conversation Started: {self.conversation_id} ===\n")
            transcript_file.write(f"Time: {self.start_time.isoformat()}\n\n")

    def _persist_event_history(self):
        """Rewrite the JSON log with the whole event history."""
        with open(self.json_log_path, "w", encoding="utf-8") as history_file:
            json.dump(self.conversation_history, history_file, ensure_ascii=False, indent=2)

    def _append_transcript_line(self, recorded_at: datetime.datetime, event_text: str):
        """Append one ``HH:MM:SS - text`` line to the text log."""
        with open(self.text_log_path, "a", encoding="utf-8") as transcript_file:
            transcript_file.write(f"{recorded_at.strftime('%H:%M:%S')} - {event_text}\n")

    def record_call_event(self, event_kind: str, event_text: str, should_append_to_transcript: bool = False):
        """Record an event (type + content) in the JSON log, and in the text log when should_append_to_transcript is
        set.
        """
        recorded_at = datetime.datetime.now()

        if should_append_to_transcript:
            # Write to the text file only when the caller asks for it
            self._append_transcript_line(recorded_at, event_text)

        # Add to JSON events
        self.conversation_history["events"].append(
            {"timestamp": recorded_at.isoformat(), "type": event_kind, "content": event_text}
        )
        self._persist_event_history()
