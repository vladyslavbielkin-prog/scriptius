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

FAST_DEBOUNCE_S = 0.1
FULL_DEBOUNCE_S = 1.5

# ── Qualification questions ──────────────────────────────────────────────────

QUALIFICATION_QUESTIONS = [
    {"id": "q-available", "text": "Чи зручно вам зараз розмовляти?"},
    {"id": "q-role", "text": "Яка ваша посада та в якій індустрії ви працюєте?"},
    {"id": "q-experience", "text": "Скільки років ви уже працюєте у цій сфері?"},
    {"id": "q-pain", "text": "З якими основними проблемами ви зараз стикаєтесь і яких цілей хочете досягти?"},
]

# ── Prompts (verbatim from production server.js) ─────────────────────────────

_q_list = "\n".join(
    f'   {i}. [id="{q["id"]}"] "{q["text"]}"'
    for i, q in enumerate(QUALIFICATION_QUESTIONS)
)

FAST_PROMPT = f"""You are analyzing a live sales call transcript. Be FAST and concise.

Transcript lines are prefixed with [Sales Rep] or [Client].

Do THREE things:

1. **Qualification Tracking** — check these questions:
{_q_list}

   For each, return status:
   - "asked" — rep asked this OR any question covering the same info (match by MEANING, any language). Example: "What do you do?" covers q-role. "Tell me about your challenges" covers q-pain.
   - "answered" — client mentioned this info without being asked. Example: "I'm a marketing manager" → q-role answered.
   - null — not yet asked or mentioned.

2. **Client Profile** — extract any info about the CLIENT mentioned ANYWHERE in the transcript (from client statements OR from rep paraphrasing/confirming client info). Return null if truly unknown.
   Fields: name, role, company, industry, experience, painPoints, goal, course

   IMPORTANT: Extract even if the info comes from [Sales Rep] lines — the rep often repeats/confirms what the client said, or speaks on behalf of client in single-mic test mode. Example: "[Sales Rep]: I work in marketing" → industry: "marketing". "[Sales Rep]: So you manage a team of 5" → experience/role info about team management.

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
    return f"""You are a world-class sales consultant who deeply understands business operations. Based on the client profile and conversation context below, generate exactly 5 value justification questions in {language}.

Your goal: ask questions that make the client THINK. Questions that show you understand their world better than most people they talk to. The client should feel "wow, that's a great question" — not "that's a generic sales pitch."

Strategic intent (internal — never say these to the client):
- Surface the hidden cost of their current problems (time, money, team morale, missed opportunities)
- Make them feel the GAP between where they are and where they could be
- Create urgency by connecting their problem to real consequences they haven't fully considered

STYLE:
- SHORT and CLEAR — one thought per question, under 15 words
- Use simple everyday words, NOT corporate jargon (no "ROI", "optimization", "synergy", "KPI", "ефективність", "оптимізація", "стратегічний")
- But the THINKING behind the question must be expert-level — reference specific realities of their industry, role, or situation
- Ask about concrete things: numbers, time, people, specific situations — not abstract concepts

SPECIALIZATION — this is what makes the questions expert:
- Use details from their industry, role, and pain points to ask questions only a knowledgeable person would ask
- Reference real scenarios that happen in their type of work (e.g., for a marketing manager: "How many hours does your team spend on reports that nobody reads?")
- Ask about second-order effects they might not have thought about (e.g., how their problem affects their team, their clients, their career growth)
- Each question should feel like it was written specifically for THIS person, not a template

Language:
- Write in {language}
- Use conversational, everyday vocabulary in that language

Reply ONLY in JSON: {{ "valueQuestions": ["question1", "question2", "question3", "question4", "question5"] }}"""


def _value_prompt_batch2(language: str) -> str:
    return f"""You are a world-class sales consultant. The rep already asked the first 5 value questions. Now generate 5 DEEPER follow-up questions in {language} that build on what the client has revealed.

These questions should go to the NEXT level — now that you know more about the client, ask things that:
- Quantify the problem: "How many hours/people/dollars does this cost you per month?"
- Expose what they've already tried and why it failed
- Connect their problem to people around them (team, boss, clients)
- Make them imagine life AFTER the problem is solved — in specific, concrete terms
- Reveal the real reason they haven't fixed this yet (budget? time? don't know how?)

STYLE:
- SHORT and CLEAR — under 15 words per question
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


def detect_conversation_language(conversation: list[dict]) -> str:
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

    # ── Public API ────────────────────────────────────────────────────────

    def on_new_transcript(self, speaker: str, text: str) -> None:
        if self._fast_task and not self._fast_task.done():
            self._fast_task.cancel()
        self._fast_task = asyncio.create_task(self._debounced_fast())

        if self._full_task and not self._full_task.done():
            self._full_task.cancel()
        self._full_task = asyncio.create_task(self._debounced_full())

        # Fire needs extraction only when CLIENT speaks (not sales rep questions)
        if speaker == "client":
            asyncio.create_task(self._extract_needs_immediate(text))

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

            language = detect_conversation_language(self.session.conversation)

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

    # ── Fast analysis ─────────────────────────────────────────────────────

    async def _run_fast_analysis(self):
        if self._is_fast_running or not self.session.conversation:
            return
        self._is_fast_running = True
        sid = self.session.session_id

        try:
            transcript = self.session.get_transcript_text()

            # Build prefill context from existing profile
            prefilled = {k: v for k, v in self.session.client_profile.items() if v is not None}
            prefill_ctx = ""
            if prefilled:
                fields_str = "\n".join(f"{k}: {v}" for k, v in prefilled.items())
                prefill_ctx = (
                    f"\n\nPre-known client info (from CRM):\n{fields_str}\n"
                    "Keep these values in the clientProfile response. "
                    "Only override if the conversation clearly contradicts them."
                )

            # Build value question tracking context
            value_ctx = ""
            if self.session.value_questions:
                q_lines = "\n".join(
                    f'   {i}. [id="{q["id"]}"] "{q["text"]}"'
                    for i, q in enumerate(self.session.value_questions)
                )
                value_ctx = (
                    f"\n\n4. **Value Question Tracking** — check these value justification questions:\n"
                    f"{q_lines}\n\n"
                    "   For each, return status: \"asked\" (rep asked this or similar by meaning), "
                    "\"answered\" (client provided this info without being asked), or null.\n\n"
                    "   Add to JSON response: \"valueStatus\": [{id, status}]"
                )

            # Pass existing needs so model doesn't duplicate
            needs_ctx = ""
            if self.session.locked_summary:
                needs_lines = "\n".join(f"- {n}" for n in self.session.locked_summary)
                needs_ctx = f"\n\nExisting client needs (DO NOT repeat these):\n{needs_lines}"

            prompt = FAST_PROMPT + prefill_ctx + value_ctx + needs_ctx
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
            qs = analysis.get("qualificationStatus")
            if isinstance(qs, list):
                analysis["qualificationStatus"] = [
                    {"id": item.get("id", ""), "status": item.get("status")}
                    for item in qs if isinstance(item, dict)
                ]

            # Normalize clientProfile → ensure all PROFILE_FIELDS present
            cp = analysis.get("clientProfile")
            if isinstance(cp, dict):
                analysis["clientProfile"] = {f: cp.get(f) for f in PROFILE_FIELDS}

            # Normalize valueStatus → list of {id, status}
            vs = analysis.get("valueStatus")
            if isinstance(vs, list):
                analysis["valueStatus"] = [
                    {"id": item.get("id", ""), "status": item.get("status")}
                    for item in vs if isinstance(item, dict)
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

                # Trigger batch 1 when ≥2 tag fields filled
                if self.session.get_filled_tag_fields() >= 2 and self.session.value_batch_generated == 0 and not self._is_generating_batch:
                    self._is_generating_batch = True
                    asyncio.create_task(self._generate_value_questions(transcript, 1))

        except Exception as e:
            logger.error(f"[{sid}][AI] Fast analysis error: {e}")
        finally:
            self._is_fast_running = False

    # ── Full analysis ─────────────────────────────────────────────────────

    async def _run_full_analysis(self):
        if self._is_full_running or not self.session.conversation:
            return
        self._is_full_running = True
        sid = self.session.session_id

        try:
            transcript = self.session.get_transcript_text()
            language = detect_conversation_language(self.session.conversation)

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

    # ── Value question generation ─────────────────────────────────────────

    async def _generate_value_questions(self, transcript: str, batch: int):
        sid = self.session.session_id
        logger.info(f"[{sid}][AI] Generating value questions (batch {batch})...")

        try:
            profile_summary = "\n".join(
                f"{k}: {v}" for k, v in self.session.client_profile.items() if v is not None
            )
            language = detect_conversation_language(self.session.conversation)
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
