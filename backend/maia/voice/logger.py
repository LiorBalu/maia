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
        self._write_text_header()
        self._write_json_log()

    def _write_text_header(self):
        """Start the text log with the conversation id and the start time."""
        with open(self.text_log_path, "w", encoding="utf-8") as f:
            f.write(f"=== Conversation Started: {self.conversation_id} ===\n")
            f.write(f"Time: {self.start_time.isoformat()}\n\n")

    def _write_json_log(self):
        """Rewrite the JSON log with the whole event history."""
        with open(self.json_log_path, "w", encoding="utf-8") as f:
            json.dump(self.conversation_history, f, ensure_ascii=False, indent=2)

    def log_event(self, event_type: str, content: str, include_in_txt: bool = False):
        """Record an event (type + content) in the JSON log, and in the text log when include_in_txt is set."""
        timestamp = datetime.datetime.now()

        if include_in_txt:
            # Write to text file only for conversation events
            with open(self.text_log_path, "a", encoding="utf-8") as f:
                f.write(f"{timestamp.strftime('%H:%M:%S')} - {content}\n")

        # Add to JSON events
        self.conversation_history["events"].append(
            {"timestamp": timestamp.isoformat(), "type": event_type, "content": content}
        )
        self._write_json_log()
