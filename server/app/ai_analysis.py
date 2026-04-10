import os
import re
import json
import asyncio
import logging

from google import genai
from fastapi import WebSocket

from app.session import CallSession, PROFILE_FIELDS

logger = logging.getLogger("scriptius.ai")

# ── Models & timing ──────────────────────────────────────────────────────────

FAST_ANALYSIS_MODEL = "gemini-2.5-flash-lite"
FULL_MODEL = "gemini-2.5-flash"
VALUE_GEN_MODEL = "gemini-2.5-flash-lite"

FAST_DEBOUNCE_S = 0.05  # ~50ms — enough to coalesce bursts of partials
FULL_DEBOUNCE_S = 1.5

# ── Qualification questions ──────────────────────────────────────────────────

DEFAULT_QUALIFICATION_QUESTIONS = [
    {"id": "q-available", "text": "Чи зручно вам зараз розмовляти?"},
    {"id": "q-role", "text": "Яка ваша посада та в якій індустрії ви працюєте?"},
    {"id": "q-experience", "text": "Скільки років ви уже працюєте у цій сфері?"},
    {"id": "q-pain", "text": "Скажіть, а чим зацікавив вас наш курс? Чим він міг би бути вам корисним?"},
]


def build_qualification_questions(profile: dict) -> list[dict]:
    """Build dynamic qualification questions based on available profile data."""
    known = {}
    field_map = {
        "role": "посада",
        "experience": "досвід",
        "company": "компанія",
        "industry": "індустрія",
    }
    for field, label in field_map.items():
        val = profile.get(field)
        if val:
            known[field] = val

    # Always start with availability
    questions = [{"id": "q-available", "text": "Чи зручно вам зараз розмовляти?"}]

    if known:
        # Build natural-sounding confirmation question
        text = "Бачу що ви вказали, що"
        parts = []
        if known.get("role"):
            parts.append(f"працюєте {known['role']}")
        if known.get("industry"):
            parts.append(f"в {known['industry']} індустрії")
        if known.get("company"):
            parts.append(f"в компанії {known['company']}")
        if known.get("experience"):
            exp = known["experience"]
            # Check if it's a level (Junior/Mid/Senior) or years
            if any(lvl in exp.lower() for lvl in ["junior", "mid", "senior", "lead", "head", "джуніор", "мідл", "сеніор"]):
                parts.append(f"на рівні {exp}")
            elif exp.isdigit():
                parts.append(f"уже {exp} років")
            else:
                parts.append(f"уже {exp}")
        text += " " + " ".join(parts) + ". Скажіть, все вірно?"
        questions.append({
            "id": "q-confirm",
            "text": text,
        })

    # Add questions for missing fields (leave 1 slot for q-pain at the end)
    missing_qs = {
        "role": {"id": "q-role", "text": "Яка ваша посада та в якій індустрії ви працюєте?"},
        "experience": {"id": "q-experience", "text": "Скільки років ви уже працюєте у цій сфері?"},
        "company": {"id": "q-company", "text": "В якій компанії ви працюєте?"},
        "industry": {"id": "q-industry", "text": "В якій індустрії ви працюєте?"},
    }
    for field, q in missing_qs.items():
        if field not in known and len(questions) < 3:  # Reserve slot 4 for q-pain
            questions.append(q)

    # ALWAYS add pain question — it's critical
    questions.append({
        "id": "q-pain",
        "text": "Скажіть, а чим зацікавив вас наш курс? Чим він міг би бути вам корисним?",
    })

    return questions[:4]


def build_fast_prompt(qualification_questions: list[dict]) -> str:
    """Build the fast analysis prompt with dynamic qualification questions."""
    q_list = "\n".join(
        f'   {i}. [id="{q["id"]}"] "{q["text"]}"'
        for i, q in enumerate(qualification_questions)
    )

    return f"""You are analyzing a live sales call transcript. Be FAST and concise.

Transcript lines are prefixed with [Sales Rep] or [Client].

Do THREE things:

1. **Qualification Tracking** — check these questions:
{q_list}

   For each, return status:
   - "asked" — rep asked this OR any question covering the same info (match by MEANING, any language). Example: "What do you do?" covers q-role. "Бачу ви вказали..." covers q-confirm.
   - "answered" — client mentioned the info, REGARDLESS of whether it was asked. This is the MOST IMPORTANT status. Mark as "answered" whenever the client provides relevant info — even if it came up in response to a different question, even if mentioned naturally in conversation, even if the rep never asked.
   - null — not yet asked AND not yet mentioned.

   CRITICAL: One client answer can cover multiple qualification questions at once. Example: rep asks "What's your position?" and client says "I'm a senior marketer at TechCorp in the IT industry for 5 years" — this answer covers q-role, q-experience, q-company, AND q-industry. Mark ALL of them as "answered" in this case.

   Always prefer "answered" over "asked" when the client has actually provided the info — "answered" means the question is RESOLVED and the rep doesn't need to ask it again.

2. **Client Profile** — extract any info about the CLIENT mentioned ANYWHERE in the transcript (from client statements OR from rep paraphrasing/confirming client info). Return null if truly unknown.
   Fields: name, role, company, industry, experience, painPoints, goal, course

   IMPORTANT: Extract even if the info comes from [Sales Rep] lines — the rep often repeats/confirms what the client said, or speaks on behalf of client in single-mic test mode.

   "course" is the course/product being discussed in the sales call.

3. **Client Needs & Problems** — extract CONFIRMED client needs/problems/goals. ONLY add if the CLIENT confirmed or admitted the need. If the sales rep just ASKS a question ("do you have problems with X?") — that is NOT a confirmed need. Wait for the client's answer. Write in the SAME LANGUAGE as the conversation (not English). Short phrases, lowercase.
   - Return an empty list if no NEW confirmed needs found.
   - Do NOT repeat or paraphrase anything already in the existing list.

Reply ONLY in JSON: {{ "qualificationStatus": [{{id, status}}], "clientProfile": {{name, role, company, industry, experience, painPoints, goal, course}}, "newNeeds": ["need1", "need2"] }}"""

FULL_PROMPT = """You are Scriptius — an AI sales assistant analyzing a live call in real time.

Transcript lines are prefixed with [Sales Rep] or [Client].

Your job:
1. **Recommended Offer** — suggest the most fitting product/service to pitch based on the client's needs. Available courses and pricing:
   - "Управління командою" — $500
   - "Excel для бізнесу" — $500
   Always include the price in your recommendation. Explain briefly WHY this course fits the client's situation.

Write in the same language the conversation is in. Keep it SHORT (2-3 sentences max).
Reply in valid JSON with keys: recommendedOffer."""


def _value_prompt(language: str) -> str:
    return f"""You are a world-class sales consultant with 20+ years of experience and deep expertise across industries. You're generating 5 highly specialized discovery questions in {language} for a sales rep to ask their client.

You have access to the client's profile (role, industry, company, experience) AND their stated problems/goals. Use ALL of this to craft questions that ONLY a true expert in their field could ask.

CORE PRINCIPLE: Each question should make the client think "this person REALLY understands my world." The client should feel like they're talking to an industry expert, not a salesperson.

EXPERT THINKING — what makes questions specialized:

1. **Industry-specific language and scenarios**: Reference real pain points unique to their industry. A marketer in fintech has different problems than a marketer in retail. Show you know the difference.

2. **Role-specific second-order effects**: Connect their stated problem to consequences they haven't fully thought through yet:
   - For managers: how the problem affects team morale, retention, your reputation with leadership
   - For individual contributors: how it affects career progression, recognition, daily stress
   - For executives: how it affects board reporting, strategic initiatives, investor confidence

3. **Quantification questions**: Force them to think in numbers/time/money:
   - "How many hours per week does your team spend fixing X?"
   - "When was the last time this problem caused a missed deadline?"
   - "How much of your monthly budget goes to working around this?"

4. **Reveal hidden costs**: Surface costs they're paying but haven't recognized:
   - Lost opportunities they didn't pursue because of this problem
   - Top performers who left because of related frustrations
   - Strategic decisions delayed because data isn't reliable

5. **Reference experience level**: Junior people care about different things than seniors. Ask accordingly.

STRUCTURE OF EACH QUESTION:
- Connect to a SPECIFIC detail from their profile (industry/role/pain point/goal)
- Force them to recall a CONCRETE situation, number, or moment
- Reveal a consequence they haven't fully processed

STYLE RULES:
- SHORT — under 18 words per question
- Simple conversational language in {language}
- NO jargon ("ROI", "synergy", "optimization", "KPI", "leverage", "scalability")
- NO yes/no questions — ask "how", "when", "what happens when"
- DON'T be generic. Each question must reference something specific from THEIR profile

TONE:
- Curious, not pushy
- Like you're trying to understand their world, not sell them something
- Respectful of their expertise

LANGUAGE: Write in {language} using natural conversational vocabulary.

Reply ONLY in JSON: {{ "valueQuestions": ["question1", "question2", "question3", "question4", "question5"] }}"""


def _value_prompt_batch2(language: str) -> str:
    return f"""You are a world-class sales consultant. The rep already asked the first 5 discovery questions. Now generate 5 DEEPER specialized follow-ups in {language} that take the conversation to the next level.

You now know MORE about the client because they've answered the first round. Use that new info to dig deeper.

WHAT TO PROBE FOR (each question should target ONE):

1. **Quantify the cost**: Force them to put a number on the problem
   - Money: "How much does X cost you each month?"
   - Time: "How many hours does your team lose to X per week?"
   - People: "How many people on your team are affected?"

2. **Past failures**: Expose what they've tried that didn't work
   - "What did you try last time to fix this?"
   - "Why didn't [previous solution] work for you?"
   - This reveals barriers and builds your case

3. **Stakeholder impact**: Connect their pain to people around them
   - Their team's frustration
   - Their boss's expectations
   - Their clients' experience
   - Their family/personal life

4. **Imagine the solution**: Make them visualize success specifically
   - "If this problem was solved tomorrow, what would change first?"
   - "What would you do with that extra time/money?"
   - This creates emotional commitment

5. **Reveal the real blocker**: Why haven't they fixed this yet?
   - Budget concerns?
   - Lack of skills/knowledge?
   - Internal politics?
   - Don't know where to start?
   - Failed past attempts?

CRITICAL: Don't repeat what was already asked. Reference what the client said in their answers and dig deeper into those specific things.

STYLE:
- SHORT — under 18 words per question
- Simple everyday words, NO jargon
- But expert thinking — reference what the client actually said, ask about specifics of THEIR situation
- Each question should feel like a natural follow-up to the conversation, not a new topic

Rules:
- DO NOT repeat or paraphrase any previous question listed below
- Write in {language} using conversational vocabulary

Reply ONLY in JSON: {{ "valueQuestions": ["question1", "question2", "question3", "question4", "question5"] }}"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_json(text: str):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"```json\s*([\s\S]*?)\s*```", text or "")
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


def detect_conversation_language(conversation: list[dict], forced: str | None = None) -> str:
    if forced:
        return forced
    if not conversation:
        return "Ukrainian"
    full_text = " ".join(e["text"] for e in conversation)
    if re.search(r"[іїєґІЇЄҐ''ʼ]", full_text):
        return "Ukrainian"
    return "Ukrainian"


# ── CallAnalyzer ──────────────────────────────────────────────────────────────

class CallAnalyzer:

    def __init__(self, session: CallSession, websocket: WebSocket):
        self.session = session
        self.ws = websocket
        self._fast_task: asyncio.Task | None = None
        self._full_task: asyncio.Task | None = None
        self._is_fast_running = False
        self._is_full_running = False
        self._is_generating_batch = False
        self._client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        # Build dynamic qualification questions based on prefilled profile
        self._qual_questions = build_qualification_questions(session.client_profile)
        self._fast_prompt = build_fast_prompt(self._qual_questions)
        self._fast_pending = False  # Track if a new fast run is needed after current finishes
        self._full_pending = False
        self._validator_running = False
        # Per-speaker debouncing for reflex on partials (waits for natural pause)
        self._reflex_partial_tasks: dict[str, asyncio.Task] = {}

    def update_qualification_questions(self):
        """Rebuild qualification questions after profile update (e.g. HubSpot prefill)."""
        self._qual_questions = build_qualification_questions(self.session.client_profile)
        self._fast_prompt = build_fast_prompt(self._qual_questions)

    # ── Public API ────────────────────────────────────────────────────────

    def on_new_transcript(self, speaker: str, text: str, is_final: bool = True) -> None:
        # ── REFLEX (limbic) ────────────────────────────────────────────
        if is_final:
            # Final = complete thought → fire reflex instantly
            # Cancel any pending partial reflex (final supersedes it)
            existing = self._reflex_partial_tasks.pop(speaker, None)
            if existing and not existing.done():
                existing.cancel()
            asyncio.create_task(self._reflex_check(speaker, text, is_final=True))
        else:
            # Partial = keep waiting for the speaker to pause
            # Cancel any pending reflex for this speaker, schedule new one with delay
            existing = self._reflex_partial_tasks.get(speaker)
            if existing and not existing.done():
                existing.cancel()
            self._reflex_partial_tasks[speaker] = asyncio.create_task(
                self._delayed_partial_reflex(speaker, text)
            )

        # ── REFLECTIVE (prefrontal): bigger context, periodic updates ───
        if self._is_full_running:
            self._full_pending = True
        else:
            if self._full_task and not self._full_task.done():
                self._full_task.cancel()
            self._full_task = asyncio.create_task(self._debounced_full())

        # Fire needs extraction only when CLIENT speaks AND it's a final transcript
        if speaker == "client" and is_final:
            asyncio.create_task(self._extract_needs_immediate(text))

    async def _delayed_partial_reflex(self, speaker: str, text: str):
        """Wait for natural pause before firing reflex on partial — simulates brain waiting for complete thought."""
        try:
            await asyncio.sleep(0.6)  # 600ms of no new partials = natural pause
            await self._reflex_check(speaker, text, is_final=False)
        except asyncio.CancelledError:
            pass

    def trigger_fast(self) -> None:
        """Trigger fast analysis (e.g. after clientInfo update)."""
        if self._fast_task and not self._fast_task.done():
            self._fast_task.cancel()
        self._fast_task = asyncio.create_task(self._debounced_fast())

    def cancel(self) -> None:
        for task in (self._fast_task, self._full_task):
            if task and not task.done():
                task.cancel()

    # ── Debounce wrappers ─────────────────────────────────────────────────

    async def _debounced_fast(self):
        try:
            await asyncio.sleep(FAST_DEBOUNCE_S)
            await self._run_fast_analysis()
        except asyncio.CancelledError:
            pass

    async def _debounced_full(self):
        try:
            await asyncio.sleep(FULL_DEBOUNCE_S)
            await self._run_full_analysis()
        except asyncio.CancelledError:
            pass

    # ── Immediate needs extraction (no debounce) ──────────────────────────

    async def _extract_needs_immediate(self, new_text: str):
        """Fire immediately on every final transcript to detect client needs."""
        if len(self.session.locked_summary) >= 20:
            return
        sid = self.session.session_id

        try:
            existing_lines = ""
            if self.session.locked_summary:
                existing_lines = "\nExisting needs (DO NOT repeat):\n" + "\n".join(
                    f"- {n}" for n in self.session.locked_summary
                )

            # Get recent conversation for context (last 10 lines)
            recent = self.session.conversation[-10:]
            context_lines = "\n".join(f"[{e['speaker']}]: {e['text']}" for e in recent)

            language = detect_conversation_language(self.session.conversation, self.session.forced_language)

            prompt = f"""You analyze a sales call. Extract CONFIRMED client needs/problems/goals.

CRITICAL RULES:
- ONLY add a need if the CLIENT confirmed or admitted it. The client must have AGREED or STATED the problem themselves.
- If the SALES REP asks a question like "do you have problems with X?" — that is NOT a confirmed need. Wait for the client's answer.
- If the sales rep says "so you need X" and the client agrees — THEN it's a confirmed need.
- Write in {language} (same language as the conversation). Do NOT write in English.
- Short phrases, lowercase.
- Return empty list if no NEW confirmed needs found.
- Do NOT repeat or paraphrase existing needs.{existing_lines}

Reply ONLY in JSON: {{ "newNeeds": ["need1", "need2"] }}"""

            response = await asyncio.to_thread(self._client.models.generate_content,
                model=FAST_ANALYSIS_MODEL,
                contents=[prompt, f"Recent conversation:\n{context_lines}"],
                config={"response_mime_type": "application/json", "temperature": 0.1},
            )

            parsed = _parse_json(response.text)
            if not parsed:
                return

            new_needs = parsed.get("newNeeds", [])
            if not isinstance(new_needs, list) or not new_needs:
                return

            added = []
            for need in new_needs:
                if not need or not isinstance(need, str):
                    continue
                need = need.strip().lstrip("•-– ")
                if not need or len(self.session.locked_summary) >= 20:
                    continue
                need_lower = need.lower()
                is_dup = any(
                    need_lower in ex.lower() or ex.lower() in need_lower
                    for ex in self.session.locked_summary
                )
                if not is_dup:
                    self.session.locked_summary.append(need)
                    added.append(need)

            if added:
                logger.info(f"[{sid}][AI] Immediate needs: {added}")
                try:
                    await self.ws.send_json({
                        "type": "analysis",
                        "data": {"summary": list(self.session.locked_summary)},
                    })
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"[{sid}][AI] Needs extraction error: {e}")

    # ── REFLEX (limbic): instant tiny-context check ──────────────────────

    async def _reflex_check(self, speaker: str, latest_text: str, is_final: bool = True):
        """Tiny instant Gemini call: track qualification + profile + value Qs + needs.
        On partials: only update qualification + value status (reversible).
        On finals: also update profile + needs (high confidence)."""
        sid = self.session.session_id
        try:
            # Tiny context: last 3 lines + the new text
            recent = self.session.conversation[-3:]
            context_lines = "\n".join(f"[{e['speaker']}]: {e['text']}" for e in recent)
            mapped_speaker = "Sales Rep" if speaker == "sales" else "Client"
            new_line = f"[{mapped_speaker}]: {latest_text}"
            full_context = (context_lines + "\n" + new_line) if context_lines else new_line

            # Existing tracked status
            qual_lines = "\n".join(f"{k}: {v}" for k, v in self.session.qualification_status.items())
            qual_ctx = f"\n\nAlready tracked qual (only UPGRADES):\n{qual_lines}" if qual_lines else ""

            # Existing value status
            val_lines = "\n".join(f"{k}: {v}" for k, v in self.session.value_status.items())
            val_ctx = f"\n\nAlready tracked value (only UPGRADES):\n{val_lines}" if val_lines else ""

            # Existing profile
            prefilled = {k: v for k, v in self.session.client_profile.items() if v is not None}
            profile_ctx = ""
            if prefilled:
                profile_ctx = "\n\nKnown profile (don't override, only add new fields):\n" + "\n".join(
                    f"{k}: {v}" for k, v in prefilled.items()
                )

            # Existing needs
            needs_ctx = ""
            if self.session.locked_summary:
                needs_lines = "\n".join(f"- {n[:80]}" for n in self.session.locked_summary[-10:])
                needs_ctx = f"\n\nExisting needs (DO NOT repeat):\n{needs_lines}"

            q_list = "\n".join(
                f'{q["id"]}: {q["text"][:80]}'
                for q in self._qual_questions
            )

            value_q_list = ""
            if self.session.value_questions:
                value_q_list = "\n\nValue justification questions to track:\n" + "\n".join(
                    f'{q["id"]}: {q["text"][:80]}'
                    for q in self.session.value_questions
                )

            language = detect_conversation_language(self.session.conversation, self.session.forced_language)
            transcript_kind = "FINAL (complete sentence)" if is_final else "PARTIAL (may be incomplete)"

            prompt = f"""Quick reflex check on a sales call. The latest text is a {transcript_kind}.

Qualification questions to track:
{q_list}{value_q_list}

For EACH question, return status:
- "asked" — rep asked it (match by meaning, any language)
- "answered" — client provided info, OR confirmed it (e.g. "так", "вірно", "yes")
- null — not yet

EXTRACT new client profile fields ONLY IF clearly stated: name, role, company, industry, experience, painPoints, goal, course
Return null for fields not clearly mentioned. DO NOT GUESS — when in doubt, return null.

EXTRACT new client needs/problems/goals (in {language}, lowercase, short phrase).
STRICT RULES for needs:
- ONLY add if the CLIENT actually stated/confirmed the need themselves
- If sales rep asks "do you have X problem?" — that is NOT a need until client confirms
- DO NOT GUESS or infer needs from context — must be EXPLICITLY mentioned
- Return empty list if no clear new need{qual_ctx}{val_ctx}{profile_ctx}{needs_ctx}

Reply ONLY in JSON: {{"qualificationStatus":[{{"id":"q-xxx","status":"asked|answered|null"}}],"valueStatus":[{{"id":"v-xxx","status":"asked|answered|null"}}],"clientProfile":{{"name":null,"role":null,"company":null,"industry":null,"experience":null,"painPoints":null,"goal":null,"course":null}},"newNeeds":["need1","need2"]}}"""

            response = await asyncio.to_thread(self._client.models.generate_content,
                model=FAST_ANALYSIS_MODEL,
                contents=[prompt, f"Recent:\n{full_context}"],
                config={"response_mime_type": "application/json", "temperature": 0.0},
            )

            analysis = _parse_json(response.text)
            if not analysis:
                return

            # Merge qualification status (only upgrade)
            STATUS_RANK = {None: 0, "null": 0, "asked": 1, "answered": 2}
            qs = analysis.get("qualificationStatus")
            changed = False
            if isinstance(qs, list):
                for item in qs:
                    if not isinstance(item, dict):
                        continue
                    qid = item.get("id", "")
                    new_status = item.get("status")
                    if not qid:
                        continue
                    current = self.session.qualification_status.get(qid)
                    if STATUS_RANK.get(new_status, 0) > STATUS_RANK.get(current, 0):
                        self.session.qualification_status[qid] = new_status
                        changed = True

            # Merge value status (only upgrade)
            vs = analysis.get("valueStatus")
            if isinstance(vs, list):
                for item in vs:
                    if not isinstance(item, dict):
                        continue
                    vid = item.get("id", "")
                    new_status = item.get("status")
                    if not vid:
                        continue
                    current = self.session.value_status.get(vid)
                    if STATUS_RANK.get(new_status, 0) > STATUS_RANK.get(current, 0):
                        self.session.value_status[vid] = new_status
                        changed = True

            # System 1: commit everything immediately (may be wrong, validator will fix)
            # Merge profile (only add new fields)
            cp = analysis.get("clientProfile")
            if isinstance(cp, dict):
                new_fields = {k: v for k, v in cp.items() if v and not self.session.client_profile.get(k)}
                if new_fields:
                    self.session.update_profile(new_fields)
                    changed = True

            # Add new needs (with dedup)
            new_needs = analysis.get("newNeeds", [])
            added_needs = []
            if isinstance(new_needs, list):
                for need in new_needs:
                    if not need or not isinstance(need, str):
                        continue
                    need = need.strip().lstrip("•-– ")
                    if not need or len(self.session.locked_summary) >= 20:
                        continue
                    need_lower = need.lower()
                    is_dup = any(
                        need_lower in ex.lower() or ex.lower() in need_lower
                        for ex in self.session.locked_summary
                    )
                    if not is_dup:
                        self.session.locked_summary.append(need)
                        added_needs.append(need)
                        changed = True
            if added_needs:
                logger.info(f"[{sid}][AI] Reflex new needs: {added_needs}")

            # On finals, schedule System 2 validation (rational pass)
            if is_final:
                asyncio.create_task(self._validator_pass())

            # Send update to frontend if anything changed
            if changed:
                payload = {
                    "qualificationStatus": [
                        {"id": qid, "status": status}
                        for qid, status in self.session.qualification_status.items()
                    ],
                    "valueStatus": [
                        {"id": vid, "status": status}
                        for vid, status in self.session.value_status.items()
                    ],
                    "clientProfile": dict(self.session.client_profile),
                    "summary": list(self.session.locked_summary),
                }
                try:
                    await self.ws.send_json({"type": "analysis", "data": payload})
                except Exception:
                    pass

                # Trigger value question generation when we have enough demographic + pain context
                if (self.session.is_ready_for_value_questions()
                        and self.session.value_batch_generated == 0
                        and not self._is_generating_batch):
                    self._is_generating_batch = True
                    transcript = self.session.get_transcript_text(max_lines=25)
                    asyncio.create_task(self._generate_value_questions(transcript, 1))

        except Exception as e:
            logger.error(f"[{sid}][AI] Reflex error: {e}")

    # ── VALIDATOR (System 2): rational pass that corrects reflex mistakes ──

    async def _validator_pass(self):
        """Slower, more rational pass that reviews recent commits and corrects mistakes.
        Uses broader context (10 lines) to validate profile + needs added by reflex."""
        if getattr(self, "_validator_running", False):
            return
        self._validator_running = True
        sid = self.session.session_id

        try:
            # Small delay to let related transcripts arrive
            await asyncio.sleep(0.4)

            # Use 10 lines of context for accurate validation
            recent = self.session.conversation[-10:]
            if not recent:
                return
            context_lines = "\n".join(f"[{e['speaker']}]: {e['text']}" for e in recent)

            # Current state to validate
            current_needs = list(self.session.locked_summary[-10:]) if self.session.locked_summary else []
            current_profile = {k: v for k, v in self.session.client_profile.items() if v is not None}

            language = detect_conversation_language(self.session.conversation, self.session.forced_language)

            prompt = f"""You are a careful sales call analyzer. Validate that the recently extracted info is CORRECT based on the actual conversation. Be strict — fix any mistakes.

Current client needs (extracted by fast reflex):
{chr(10).join(f"- {n}" for n in current_needs) if current_needs else "(none)"}

Current client profile (extracted by fast reflex):
{chr(10).join(f"{k}: {v}" for k, v in current_profile.items()) if current_profile else "(none)"}

Your job: review the actual conversation and return the CORRECTED state.

Rules:
- Only keep needs that the CLIENT actually stated explicitly. Remove any inferred or made-up needs.
- Only keep profile fields that are clearly stated in the conversation. Remove guesses.
- Write needs in {language}, lowercase, short phrases.
- DO NOT remove needs/profile that came from CRM (we have no way to know this from the transcript, so be conservative — only remove things that are clearly wrong).

Reply ONLY in JSON: {{
  "validatedNeeds": ["clean list of correct needs"],
  "validatedProfile": {{"name": "...", "role": "...", "company": "...", "industry": "...", "experience": "...", "painPoints": "...", "goal": "...", "course": "..."}},
  "removedNeeds": ["needs that were wrong and should be removed"],
  "corrections": ["short reason for each correction"]
}}"""

            response = await asyncio.to_thread(self._client.models.generate_content,
                model=FULL_MODEL,  # Use the smarter model for validation
                contents=[prompt, f"Conversation:\n{context_lines}"],
                config={"response_mime_type": "application/json", "temperature": 0.0},
            )

            result = _parse_json(response.text)
            if not result:
                return

            changed = False

            # Apply removed needs
            removed = result.get("removedNeeds", [])
            if isinstance(removed, list) and removed:
                removed_lowers = {n.lower().strip() for n in removed if isinstance(n, str)}
                before_count = len(self.session.locked_summary)
                self.session.locked_summary = [
                    n for n in self.session.locked_summary
                    if n.lower().strip() not in removed_lowers
                ]
                if len(self.session.locked_summary) < before_count:
                    changed = True
                    logger.info(f"[{sid}][Validator] Removed {before_count - len(self.session.locked_summary)} wrong needs: {removed}")

            corrections = result.get("corrections", [])
            if corrections:
                logger.info(f"[{sid}][Validator] Corrections: {corrections}")

            if changed:
                try:
                    await self.ws.send_json({
                        "type": "analysis",
                        "data": {
                            "summary": list(self.session.locked_summary),
                            "clientProfile": dict(self.session.client_profile),
                        },
                    })
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"[{sid}][Validator] error: {e}")
        finally:
            self._validator_running = False

    # ── Fast analysis ─────────────────────────────────────────────────────

    async def _run_fast_analysis(self):
        if self._is_fast_running or not self.session.conversation:
            return
        self._is_fast_running = True
        sid = self.session.session_id

        try:
            # Limit context for fast analysis — only last 25 lines to keep prompt small + fast
            transcript = self.session.get_transcript_text(max_lines=25)

            # Build prefill context from existing profile
            prefilled = {k: v for k, v in self.session.client_profile.items() if v is not None}
            prefill_ctx = ""
            if prefilled:
                fields_str = "\n".join(f"{k}: {v}" for k, v in prefilled.items())
                prefill_ctx = (
                    f"\n\nPre-known client info (from CRM, NOT from the conversation):\n{fields_str}\n"
                    "IMPORTANT RULES for this CRM data:\n"
                    "1. Keep these values in the clientProfile response. Only override if the conversation clearly contradicts them.\n"
                    "2. DO NOT mark qualification questions as 'answered' just because this CRM data exists. "
                    "The 'answered' status is ONLY for info that was ACTUALLY MENTIONED IN THE TRANSCRIPT by the client or rep. "
                    "If the client hasn't yet confirmed this info verbally during the call, the question is still null or 'asked' — NOT 'answered'.\n"
                    "3. The q-confirm question is only 'answered' when the client EXPLICITLY confirms (says 'так', 'вірно', 'yes', 'correct', etc.) AFTER the rep asks the confirmation question. Pre-existing CRM data does NOT count as confirmation."
                )

            # Existing qual + value status (so AI doesn't need to re-detect what's already tracked)
            existing_status_ctx = ""
            if self.session.qualification_status:
                existing_status_ctx += "\n\nAlready tracked qual status (only return UPGRADES, never downgrades):\n"
                existing_status_ctx += "\n".join(f"{k}: {v}" for k, v in self.session.qualification_status.items())
            if self.session.value_status:
                existing_status_ctx += "\n\nAlready tracked value status:\n"
                existing_status_ctx += "\n".join(f"{k}: {v}" for k, v in self.session.value_status.items())

            # Build compact value question tracking context (only first 60 chars per question)
            value_ctx = ""
            if self.session.value_questions:
                q_lines = "\n".join(
                    f'{q["id"]}: {q["text"][:60]}'
                    for q in self.session.value_questions
                )
                value_ctx = (
                    f"\n\n4. **Value Q Tracking** — return status for each: "
                    f'"asked"/"answered"/null. Add "valueStatus": [{{id, status}}]:\n{q_lines}'
                )

            # Pass existing needs (last 10 only) so model doesn't duplicate
            needs_ctx = ""
            if self.session.locked_summary:
                recent_needs = self.session.locked_summary[-10:]
                needs_lines = "\n".join(f"- {n[:80]}" for n in recent_needs)
                needs_ctx = f"\n\nExisting needs (DO NOT repeat):\n{needs_lines}"

            prompt = self._fast_prompt + prefill_ctx + existing_status_ctx + value_ctx + needs_ctx
            logger.info(f"[{sid}][AI] Fast analysis triggered (debounce {FAST_DEBOUNCE_S}s)")

            response = await asyncio.to_thread(self._client.models.generate_content,
                model=FAST_ANALYSIS_MODEL,
                contents=[prompt, f"Transcript:\n{transcript}"],
                config={"response_mime_type": "application/json", "temperature": 0.1},
            )

            analysis = _parse_json(response.text)
            if not analysis:
                logger.warning(f"[{sid}][AI] Fast analysis: failed to parse JSON")
                return

            # Normalize qualificationStatus → list of {id, status}
            # MERGE with persistent state — once asked/answered, never go back to null
            STATUS_RANK = {None: 0, "null": 0, "asked": 1, "answered": 2}
            qs = analysis.get("qualificationStatus")
            if isinstance(qs, list):
                for item in qs:
                    if not isinstance(item, dict):
                        continue
                    qid = item.get("id", "")
                    new_status = item.get("status")
                    if not qid:
                        continue
                    current = self.session.qualification_status.get(qid)
                    # Only upgrade — never downgrade
                    if STATUS_RANK.get(new_status, 0) > STATUS_RANK.get(current, 0):
                        self.session.qualification_status[qid] = new_status
                # Send merged state to frontend
                analysis["qualificationStatus"] = [
                    {"id": qid, "status": status}
                    for qid, status in self.session.qualification_status.items()
                ]

            # Normalize clientProfile → ensure all PROFILE_FIELDS present
            cp = analysis.get("clientProfile")
            if isinstance(cp, dict):
                analysis["clientProfile"] = {f: cp.get(f) for f in PROFILE_FIELDS}

            # Normalize valueStatus → list of {id, status}
            # Same merge logic — once asked/answered, never go back
            vs = analysis.get("valueStatus")
            if isinstance(vs, list):
                for item in vs:
                    if not isinstance(item, dict):
                        continue
                    vid = item.get("id", "")
                    new_status = item.get("status")
                    if not vid:
                        continue
                    current = self.session.value_status.get(vid)
                    if STATUS_RANK.get(new_status, 0) > STATUS_RANK.get(current, 0):
                        self.session.value_status[vid] = new_status
                analysis["valueStatus"] = [
                    {"id": vid, "status": status}
                    for vid, status in self.session.value_status.items()
                ]

            logger.info(
                f"[{sid}][AI] Fast result: "
                f"qualificationStatus={json.dumps(analysis.get('qualificationStatus', []), ensure_ascii=False)[:200]}, "
                f"profile fields: {len([v for v in (analysis.get('clientProfile') or {}).values() if v])}"
            )

            # Handle new client needs — add to locked list and send immediately
            new_needs = analysis.pop("newNeeds", [])
            if isinstance(new_needs, list) and new_needs and len(self.session.locked_summary) < 20:
                added = []
                for need in new_needs:
                    if not need or not isinstance(need, str):
                        continue
                    need = need.strip().lstrip("•-– ")
                    if not need:
                        continue
                    # Skip if duplicate or too similar to existing
                    need_lower = need.lower()
                    is_dup = any(
                        need_lower in existing.lower() or existing.lower() in need_lower
                        for existing in self.session.locked_summary
                    )
                    if not is_dup and len(self.session.locked_summary) < 20:
                        self.session.locked_summary.append(need)
                        added.append(need)
                if added:
                    logger.info(f"[{sid}][AI] New needs added: {added}")
                    analysis["summary"] = list(self.session.locked_summary)

            # Send to frontend
            try:
                await self.ws.send_json({"type": "analysis", "data": analysis})
            except Exception:
                return

            # Update session value status
            if isinstance(analysis.get("valueStatus"), list):
                for item in analysis["valueStatus"]:
                    vid = item.get("id")
                    status = item.get("status")
                    if vid and status:
                        self.session.value_status[vid] = status
                logger.info(f"[{sid}][AI] Value status: {json.dumps(self.session.value_status, ensure_ascii=False)}")

                # Trigger batch 2 when ≥2 questions from batch 1 have been asked
                if self.session.value_batch_generated == 1 and not self._is_generating_batch:
                    batch1_ids = [q["id"] for q in self.session.value_questions if q.get("batch") == 1]
                    asked_count = sum(1 for vid in batch1_ids if self.session.value_status.get(vid) == "asked")
                    if asked_count >= 2:
                        self._is_generating_batch = True
                        asyncio.create_task(self._generate_value_questions(transcript, 2))

            # Update client profile
            if analysis.get("clientProfile"):
                self.session.update_profile(analysis["clientProfile"])

                # Trigger batch 1 when we have enough demographic + pain context
                if self.session.is_ready_for_value_questions() and self.session.value_batch_generated == 0 and not self._is_generating_batch:
                    self._is_generating_batch = True
                    asyncio.create_task(self._generate_value_questions(transcript, 1))

        except Exception as e:
            logger.error(f"[{sid}][AI] Fast analysis error: {e}")
        finally:
            self._is_fast_running = False
            # If new transcript came in while we were running, fire another run
            if self._fast_pending:
                self._fast_pending = False
                self._fast_task = asyncio.create_task(self._debounced_fast())

    # ── Full analysis ─────────────────────────────────────────────────────

    async def _run_full_analysis(self):
        if self._is_full_running or not self.session.conversation:
            return
        self._is_full_running = True
        sid = self.session.session_id

        try:
            # Limit context — full analysis only needs recent context for offer recommendation
            transcript = self.session.get_transcript_text(max_lines=40)
            language = detect_conversation_language(self.session.conversation, self.session.forced_language)

            lang_ctx = (
                f"\n\nIMPORTANT: The conversation is in {language}. "
                f"Write ALL output in {language}. "
                "Do not switch languages."
            )

            logger.info(f"[{sid}][AI] Full analysis triggered (debounce {FULL_DEBOUNCE_S}s)")

            response = await asyncio.to_thread(self._client.models.generate_content,
                model=FULL_MODEL,
                contents=[FULL_PROMPT + lang_ctx, f"Current transcript:\n{transcript}"],
                config={"response_mime_type": "application/json", "temperature": 0.3},
            )

            analysis = _parse_json(response.text)
            if analysis:
                logger.info(
                    f"[{sid}][AI] Full result: "
                    f"offer={str(analysis.get('recommendedOffer', ''))[:100]}"
                )
                try:
                    await self.ws.send_json({"type": "analysis", "data": analysis})
                except Exception:
                    pass
            else:
                logger.warning(f"[{sid}][AI] Full analysis: failed to parse JSON")

        except Exception as e:
            logger.error(f"[{sid}][AI] Full analysis error: {e}")
        finally:
            self._is_full_running = False
            if self._full_pending:
                self._full_pending = False
                self._full_task = asyncio.create_task(self._debounced_full())

    # ── Value question generation ─────────────────────────────────────────

    async def _generate_value_questions(self, transcript: str, batch: int):
        sid = self.session.session_id
        logger.info(f"[{sid}][AI] Generating value questions (batch {batch})...")

        try:
            profile_summary = "\n".join(
                f"{k}: {v}" for k, v in self.session.client_profile.items() if v is not None
            )
            language = detect_conversation_language(self.session.conversation, self.session.forced_language)
            logger.info(f"[{sid}][AI] Detected conversation language: {language}")

            prompt_fn = _value_prompt_batch2 if batch == 2 else _value_prompt
            prompt_text = prompt_fn(language)

            previous_ctx = ""
            if batch == 2 and self.session.value_questions:
                prev_lines = "\n".join(
                    f"{i + 1}. {q['text']}" for i, q in enumerate(self.session.value_questions)
                )
                previous_ctx = f"\n\nPrevious questions (DO NOT repeat):\n{prev_lines}"

            response = await asyncio.to_thread(self._client.models.generate_content,
                model=VALUE_GEN_MODEL,
                contents=[
                    prompt_text + previous_ctx,
                    f"Client profile:\n{profile_summary}\n\nRecent conversation:\n{transcript}",
                ],
                config={"response_mime_type": "application/json", "temperature": 0.4},
            )

            parsed = _parse_json(response.text)
            if parsed and isinstance(parsed.get("valueQuestions"), list):
                start_idx = len(self.session.value_questions)
                new_questions = [
                    {"id": f"v-{start_idx + i + 1}", "text": text, "batch": batch}
                    for i, text in enumerate(parsed["valueQuestions"])
                ]
                self.session.value_questions.extend(new_questions)
                self.session.value_batch_generated = batch
                logger.info(f"[{sid}][AI] Generated {len(new_questions)} value questions (batch {batch})")

                try:
                    await self.ws.send_json({
                        "type": "valueQuestions",
                        "questions": new_questions,
                        "batch": batch,
                    })
                except Exception:
                    pass
            else:
                logger.warning(f"[{sid}][AI] Value questions: failed to parse JSON")

        except Exception as e:
            logger.error(f"[{sid}][AI] Value questions generation error: {e}")
        finally:
            self._is_generating_batch = False
