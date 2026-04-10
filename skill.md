# Scriptius AI Sales Assistant — Skill Guide

How each component works during a live sales call.

---

## 1. Client Card

**What it shows**: Real-time profile of the person you're talking to.

**Fields**:
- **HubSpot Deal** — paste a deal URL or ID to auto-fill client data from HubSpot
- **Course** — manually selected by the sales rep
- **Position** / **Experience** — job title and years in the field
- **Company** / **Industry** — where they work and their sector
- **Pain Points** / **Goal** — problems and objectives mentioned

**How it updates**:
- **From HubSpot**: paste a deal URL → name, role, company, industry fill instantly from the associated contact
- **From conversation**: powered by `gemini-2.5-flash-lite` (fast analysis, 0.1s debounce after each transcript). Extracts info from both client and sales rep lines
- Fields fill as the conversation progresses — once set, they update only if the conversation contradicts them

**Tips**:
- Paste the HubSpot deal URL before starting the call to pre-fill the card
- Select the course manually before the call starts
- Even if only the sales rep's mic is active, the system extracts info from rep's lines

---

## 2. Client Needs & Problems

**What it shows**: Up to 20 bullet points of confirmed client needs, problems, pain points, and goals.

**How it works**:
- **Immediate extraction** — fires on every client transcript (zero debounce). A focused `gemini-2.5-flash-lite` call analyzes the last 10 conversation lines and extracts new needs
- **Backup extraction** — the fast analysis (0.1s debounce) also catches needs as part of its broader analysis
- **Language**: always in the same language as the conversation (Ukrainian, etc.)

**Rules**:
- A need is ONLY added when the **client confirms or states** it themselves
- If the sales rep asks "Do you have problems with X?" — that is NOT added. The system waits for the client's answer
- Once added, a point is **never removed** (unless the client explicitly says it was wrong)
- Duplicates and similar points are filtered out (substring matching)
- Maximum 20 points

**Tips**:
- Ask open-ended questions to surface needs: "What are the main challenges you face?"
- When the client confirms a problem, it appears in the list within ~1 second
- Use these bullet points as your pitch summary — read them back to the client before making the offer: "So, you mentioned that you struggle with X, Y, and Z..."

---

## 3. Recommended Offer

**What it shows**: The best-fit course with what the client gets and the price.

**What the client gets** (shown as checkmarks):
- Сертифікат про закінчення курсу
- Доступ до LMS платформи та спільноти
- Фінальний проєкт з менторською підтримкою

**Price**: $500

**Available courses**:
- **Управління командою** — $500
- **Excel для бізнесу** — $500

**How it works**:
- Powered by `gemini-2.5-flash` (full analysis, 1.5s debounce)
- Analyzes the full conversation transcript to pick the best-fit course
- Updates as more context becomes available

### Client Readiness Bar

Shows how ready the client is to hear the offer. Each segment has its own color:

| Level | Color | Label | When it triggers |
|---|---|---|---|
| 1 | Dark red | Low | Default (start of call) |
| 2 | Light red | Early | 2+ qualification questions checked |
| 3 | Yellow | Neutral | All 4 qualification questions + 1 client need |
| 4 | Light green | Warm | All 4 qualification + 3 needs |
| 5 | Dark green | Ready | All 4 qualification + 3 needs + 2 value questions asked |

**Tips**:
- Wait until readiness is at least "Warm" (level 4) before presenting the offer
- Read back the client's needs from the list, then present the course + price

---

## 4. Qualification Questions (Actualisation & Qualification)

**What it shows**: 4 checkboxes tracking whether key qualifying questions have been asked/answered.

**Questions**:
1. "Чи зручно вам зараз розмовляти?" — Is the client available to talk?
2. "Яка ваша посада та в якій індустрії ви працюєте?" — Role and industry
3. "Скільки років ви уже працюєте у цій сфері?" — Experience
4. "З якими основними проблемами ви зараз стикаєтесь і яких цілей хочете досягти?" — Pain points and goals

**Statuses**:
- **Unchecked** — not yet asked or mentioned
- **Checked (asked)** — the sales rep asked this question (or any question covering the same topic, in any language)
- **Crossed out ("Not relevant")** — the client already provided the info (in any context). The rep doesn't need to ask it again — the question is resolved.

**How it updates**:
- Powered by `gemini-2.5-flash-lite` (fast analysis, 0.1s debounce)
- Matches by **meaning**, not exact wording — "What do you do?" counts as asking about role
- **One answer can resolve multiple questions**: if you ask "What's your position?" and the client says "I'm a senior marketer at TechCorp in IT for 5 years", that single answer crosses out role, experience, company, AND industry questions at once. No need to ask them separately.
- Updates within ~1-2 seconds of the question being answered

**Tips**:
- Go through these in order at the start of the call
- You don't need to use the exact wording — any natural question covering the topic will be recognized
- Once all 4 are checked, move on to value justification

---

## 5. Value Justification Questions

**What it shows**: AI-generated personalized sales questions in two rounds, displayed side by side.

**Layout**: Two columns — Round 1 (left) and Round 2 (right).

**How it works**:
- **Round 1** (5 questions) — generated when 2+ profile tag fields are filled (industry, experience, company, painPoints, goal)
- **Round 2** (5 deeper follow-ups) — generated when 2+ questions from round 1 have been asked
- Powered by `gemini-2.5-flash-lite`
- Questions are personalized based on the client's industry, role, and pain points

**Question style**:
- Short and clear — under 15 words each
- Simple everyday words, no corporate jargon
- Expert-level thinking — references specific realities of the client's industry/role
- Each question is designed to make the client think about the hidden cost of their problems

**Strategic purpose** (internal — don't say this to the client):
- Surface the hidden cost of current problems (time, money, team morale)
- Create urgency by connecting problems to real consequences
- Make the client feel the gap between where they are and where they could be

**Tips**:
- Don't read the questions word-for-word — use them as conversation guides
- The questions are ordered strategically — try to follow the order
- After asking round 1 questions, round 2 appears automatically with deeper follow-ups
- The checkboxes next to each question track whether you've asked it (recognized by meaning, not exact wording)

---

## 6. HubSpot Integration

**What it does**: Auto-fills the client card from a HubSpot deal.

**How to use**:
1. Open the deal in HubSpot → copy the URL from the browser bar
2. Paste it into the **HubSpot Deal** field in Scriptius (top of the client card)
3. Press Enter or click the arrow button
4. Name, role, company, industry fill in immediately

**What gets pulled**:

| HubSpot Contact Field | Scriptius Client Card |
|---|---|
| First name + Last name | Name |
| Job title | Position |
| Company name | Company |
| Industry | Industry |

**Setup**: See [docs/hubspot-setup.md](docs/hubspot-setup.md) for full setup instructions.

---

## Call Flow Summary

1. **Before the call** → Paste HubSpot deal URL → Client card fills → Select course
2. **Qualification** → Ask the 4 qualifying questions → Client card fills in more
3. **Value Justification** → Once 2+ profile fields filled, round 1 questions appear → Ask them → Round 2 appears
4. **Client Needs** → As the client confirms problems, they appear in the needs list (up to 20)
5. **Check readiness** → Wait for "Warm" or "Ready" (green bar)
6. **Offer** → Read back the client's needs from the list → Present the course, what they get, and the price
