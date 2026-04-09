# Scriptius AI Sales Assistant — Skill Guide

How each component works during a live sales call.

---

## 1. Client Card

**What it shows**: Real-time profile of the person you're talking to.

**Fields**:
- **Name** — client's name
- **Position** — job title/role
- **Experience** — years in the field
- **Company** — where they work
- **Industry** — their sector
- **Pain Points** — problems they mentioned
- **Goal** — what they want to achieve
- **Course** — manually selected by the sales rep

**How it updates**:
- Powered by `gemini-2.5-flash-lite` (fast analysis, 0.1s debounce after each transcript)
- Extracts info from BOTH client and sales rep lines (the rep often repeats/confirms client info)
- Fields fill in as the conversation progresses — once set, they update only if the conversation contradicts them
- Pre-known CRM data is preserved unless the conversation overrides it

**Tips**:
- Select the course manually before the call starts
- The card fills fastest when you ask direct questions: "What's your role?", "How long have you been doing this?"
- Even if only the sales rep's mic is active, the system extracts info from rep's lines (useful in single-mic test mode)

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

**What it shows**: The best-fit course/product to pitch, with price and a short reason why it fits.

**How it works**:
- Powered by `gemini-2.5-flash` (full analysis, 1.5s debounce)
- Analyzes the full conversation transcript
- Matches client needs to available courses:
  - **Управління командою** — $500
  - **Excel для бізнесу** — $500
- Updates as more context becomes available

**Tips**:
- The recommendation improves as the conversation progresses — more needs = better match
- Use it as a guide, not a script — adapt the recommendation to the flow of the conversation
- The "Client Readiness" bar shows how ready the client is to hear the offer

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
- **Checked (answered)** — the client volunteered this info without being asked

**How it updates**:
- Powered by `gemini-2.5-flash-lite` (fast analysis, 0.1s debounce)
- Matches by **meaning**, not exact wording — "What do you do?" counts as asking about role
- Updates within ~1-2 seconds of the question being asked

**Tips**:
- Go through these in order at the start of the call
- You don't need to use the exact wording — any natural question covering the topic will be recognized
- Once all 4 are checked, move on to value justification

---

## 5. Value Justification Questions

**What it shows**: AI-generated personalized sales questions tailored to the client's profile.

**How it works**:
- **Batch 1** (5 questions) — generated when ≥2 profile tag fields are filled (industry, experience, company, painPoints, goal)
- **Batch 2** (5 deeper follow-ups) — generated when ≥2 questions from batch 1 have been asked
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
- When ≥2 questions from batch 1 are asked, batch 2 appears automatically with deeper follow-ups
- The checkboxes next to each question track whether you've asked it (recognized by meaning, not exact wording)

---

## Call Flow Summary

1. **Start call** → Select course → Begin qualification questions
2. **Qualification** → Ask the 4 qualifying questions → Client card fills in
3. **Value Justification** → Once ≥2 profile fields filled, personalized questions appear → Ask them
4. **Client Needs** → As the client confirms problems, they appear in the needs list
5. **Offer** → When enough needs are identified, read back the needs list → Present the recommended offer
