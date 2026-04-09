import time
import logging

logger = logging.getLogger("scriptius.session")

SPEAKER_MAP = {"client": "Client", "sales": "Sales Rep"}

PROFILE_FIELDS = ("name", "role", "company", "industry", "experience", "painPoints", "goal", "course")
TAG_FIELDS = ("industry", "experience", "company", "painPoints", "goal")


class CallSession:

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.conversation: list[dict] = []
        self.client_profile: dict = {f: None for f in PROFILE_FIELDS}
        self.qualification_status: dict = {}
        self.value_questions: list[dict] = []
        self.value_status: dict = {}
        self.value_batch_generated: int = 0
        self.call_start_time: float = time.time()
        self.notes: list = []
        self.locked_summary: list[str] = []

    def add_transcript(self, speaker: str, text: str) -> None:
        mapped = SPEAKER_MAP.get(speaker, speaker)
        text_lower = text.lower().strip()

        # Check recent entries from same speaker for substring overlap
        for entry in reversed(self.conversation[-10:]):
            if entry["speaker"] != mapped:
                continue
            existing_lower = entry["text"].lower().strip()

            if text_lower == existing_lower:
                return  # exact duplicate — skip
            if text_lower in existing_lower:
                return  # new text is subset of existing — skip
            if existing_lower in text_lower:
                # existing is subset of new — replace with longer version
                entry["text"] = text
                entry["timestamp"] = time.time()
                logger.info(f"[{self.session_id}] Replaced transcript: [{mapped}]: \"{text[:120]}\"")
                return
            break  # only check most recent entry from this speaker

        # No overlap — append as new
        new_entry = {"speaker": mapped, "text": text, "timestamp": time.time()}
        self.conversation.append(new_entry)
        logger.info(f"[{self.session_id}] Added transcript: [{mapped}]: \"{text[:120]}\"")

    def get_transcript_text(self) -> str:
        return "\n".join(
            f"[{e['speaker']}]: {e['text']}" for e in self.conversation
        )

    def update_profile(self, fields: dict) -> None:
        for k, v in fields.items():
            if k in self.client_profile:
                self.client_profile[k] = v

    def get_filled_profile_fields(self) -> int:
        return sum(1 for v in self.client_profile.values() if v is not None)

    def get_filled_tag_fields(self) -> int:
        return sum(1 for f in TAG_FIELDS if self.client_profile.get(f) is not None)
