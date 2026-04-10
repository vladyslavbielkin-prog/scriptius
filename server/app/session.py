import time
import logging

logger = logging.getLogger("scriptius.session")

SPEAKER_MAP = {"client": "Client", "sales": "Sales Rep"}

PROFILE_FIELDS = ("name", "role", "company", "industry", "experience", "painPoints", "goal", "course")
TAG_FIELDS = ("industry", "experience", "company", "painPoints", "goal")
# Demographic fields (about who the client is)
DEMOGRAPHIC_FIELDS = ("role", "industry", "company", "experience")
# Pain/goal fields (what the client needs)
PAIN_FIELDS = ("painPoints", "goal")


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
        self.forced_language: str | None = None  # AI language name e.g. "English", "Ukrainian"
        self.country: str = "UA"  # ISO country code
        self.stt_engine: str | None = None  # "chirp_v2", "latest_long_v1", "deepgram"
        self.pending_partial: dict[str, str] = {}  # speaker → latest interim text

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

    def get_transcript_text(self, max_lines: int | None = None) -> str:
        """Get conversation transcript. If max_lines set, returns only the most recent N lines."""
        convo = self.conversation
        if max_lines is not None and len(convo) > max_lines:
            convo = convo[-max_lines:]
        lines = [f"[{e['speaker']}]: {e['text']}" for e in convo]
        # Append pending interim transcripts so AI can react before finals arrive
        for sp, text in self.pending_partial.items():
            if text:
                mapped = SPEAKER_MAP.get(sp, sp)
                lines.append(f"[{mapped}]: {text}")
        return "\n".join(lines)

    def update_profile(self, fields: dict) -> None:
        for k, v in fields.items():
            if k in self.client_profile and v is not None:
                self.client_profile[k] = v

    def get_filled_profile_fields(self) -> int:
        return sum(1 for v in self.client_profile.values() if v is not None)

    def get_filled_tag_fields(self) -> int:
        return sum(1 for f in TAG_FIELDS if self.client_profile.get(f) is not None)

    def is_ready_for_value_questions(self) -> bool:
        """Check if we have enough info to generate specialized value questions.
        Need: 2+ demographic fields (role/industry/company/experience)
              AND 1+ pain/goal field (painPoints/goal)."""
        demo_count = sum(1 for f in DEMOGRAPHIC_FIELDS if self.client_profile.get(f))
        pain_count = sum(1 for f in PAIN_FIELDS if self.client_profile.get(f))
        # Also check locked needs as a fallback for pain
        if pain_count == 0 and self.locked_summary:
            pain_count = 1
        return demo_count >= 2 and pain_count >= 1
