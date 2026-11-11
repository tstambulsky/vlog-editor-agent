# House rules: how this journal writes (tweet + spoken narration)

TEMPLATE: this is YOUR voice. Edit it freely; it is the one writing file meant to be
customized. It sits ON TOP of `humanizer.md` and `stop-slop.md` (vendored, MIT, see
README): where they disagree, this file wins. The drafting, humanize and review passes
all read it, so a rule you add here is enforced everywhere.

Tip: if you like an all-lowercase tweet style, say so in the Tweet section and set
`TWEET_LOWERCASE=true` so the mechanical lint enforces it too.

## Shared rules (narration AND tweet)

- First person singular. "I", not "we".
- Say the thing first. No throat-clearing ("Here's the thing", "Let me tell you", "So today").
- No "not X, it's Y" / "it's not about X, it's about Y" contrasts. State Y.
- No rule-of-three lists or triads of adjectives. Use 2 or 4 items, or one.
- No em dashes or en dashes anywhere. Commas, periods, or a new line.
- No false agency ("the city taught me", "the energy pulls you in"). A person does the verb.
- No quotable golden sentences, no punchy moral after every paragraph.
- No closing question and no prompt-to-reply ("what do you think?", "would you?").
  End on a fact, a short directive, or just stop after the last real moment.
- Exact and concrete beats abstract: a time, a price, a count, a name from the notes.
  Never invent a number, a name, a quote or an event that is not in the notes or the footage.
- Vary sentence length. Have an opinion. Owning a miss ("I got lost twice") is part of the voice.
- No travel-brochure words: vibrant, bustling, hidden gem, paradise, breathtaking, nestled,
  magical, unforgettable, journey, tapestry, testament, delve, embark, realm, elevate.

## Narration (spoken by the cloned voice)

- Casual spoken English with contractions. Short sentences that are easy to say in one breath.
- Normal capitalization: the narration text becomes the burned subtitles.
- Each beat narrates what is on screen in that clip. No generic reflection floating over
  unrelated footage.
- The last beat ends on the last real moment of the day or one concrete takeaway.

## Tweet (X caption)

1. Line 1, always: `day N/M in <place>` (N, M and place come from the task).
2. Blank line, then 1-4 short lines about the day: one concrete observation beats a summary.
3. @-mention only people/brands named in the day's notes, verbatim.

Never in the tweet: hashtags, links, emoji spam, a question at the end, engagement bait,
"day N" twice. Hard limit 600 characters. The system appends shoutouts and an optional
footer later; do not write a footer yourself.

## Scoring rubric (used by the reviewer pass)

Score each piece 1-10 on five dimensions; total out of 50. Below REVIEW_MIN_SCORE gets rewritten.

| Dimension | 10 means |
|---|---|
| Directness | states things; no announcing, hedging, throat-clearing |
| Rhythm | sentence lengths vary; no metronome, no stacked one-liners |
| Trust | respects the reader; no explaining the obvious, no moral at the end |
| Authenticity | sounds like this author talking/typing, not a brand or a chatbot |
| Density | nothing cuttable; concrete details carry it |

Automatic caps (a piece cannot score above 40 if it has any): an em/en dash, a closing
question, a "not X, it's Y" contrast, a triad, a travel-brochure word, or an invented fact.
