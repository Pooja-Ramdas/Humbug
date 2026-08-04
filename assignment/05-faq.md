# FAQ and Ground Rules

---

## Ground rules

**Can I use AI to build this?**
Yes, and we expect you to. Claude Code, Cursor, Copilot, ChatGPT, whatever you
like. You must document how, in `AI-WORKFLOW.md`. The only real rule is that you
understand what shipped, because we will ask you to explain specific code on the
follow-up call.

**Can I use libraries, templates, boilerplate?**
Yes. Use a graph library, a map library, an admin template, a starter kit. Say
what you used in `DECISIONS.md`. Nobody is impressed by a hand-rolled quadtree.

**Can I discuss this with other people?**
Talk to whoever you like about the problem. Do not submit someone else's work as
yours, and do not share your solution repo with other candidates — we do compare
submissions, and matching submissions get both candidates dropped.

**Can I reuse something I built before?**
Yes, if you say so and it is genuinely yours.

**What if I can't finish?**
Submit anyway. A partial system with clear documentation of what is missing beats
silence, and beats a system that pretends to be complete. Write in the submission
note what you cut and why.

---

## Scope questions

**How real does the data need to be?**
Synthetic, but shaped like the real thing. Generate a network matching the
schemas and proportions in `02-data-and-systems.md`. A few thousand poles across
a few dozen transformers is plenty; you do not need all 38,400. What matters is
that the shape is right — radial lines, branches, varying line lengths, ~9% of
poles without devices, ~60% of transformers missing pole ordering.

**Do I need real map tiles?**
Any map that renders for a reviewer with no API key of theirs is fine. Free
OpenStreetMap tiles are fine. A schematic or graph view instead of a geographic
map is a legitimate choice if you can defend it — argue it in `ARCHITECTURE.md`.

**Do I need to handle the 11 kV / HT side?**
Only to the extent that a feeder-level outage is one of the fault types you must
distinguish. No modelling of transmission.

**Do I need authentication?**
No. A hardcoded operator identity is fine. Do not spend hours on auth.

**Real-time updates, or is polling fine?**
Your call. Polling is fine if you justify it. WebSockets are fine if you get
them working through your host's proxy — note that this is a classic deployment
failure, so if you use them, test them on the deployed URL and not just locally.

**Should the system handle historical analysis or predict future faults?**
No. Out of scope, and building it instead of the core will cost you.

**Can I add features not in the brief?**
Yes, once the core works, and only if you can justify them as product decisions.
Ranking incidents by households affected, or flagging a span that has failed
three times this month, are the kinds of additions that read as good judgment.
Extra features on top of a broken core read as poor prioritisation.

---

## The hard parts, answered as far as we will answer them

**The topology is missing for most transformers. Is that intentional?**
Yes. It is the central design problem. See `02-data-and-systems.md` §3.

**Then what's the right approach to it?**
We will not tell you, because how you approach an underspecified problem is what
we are measuring. Several different answers score full marks. What does not score
is silently assuming the data is complete.

**Am I allowed to say "the department needs to do a survey" and stop there?**
Not as your whole answer. In the real engagement you would be told the survey
takes eight months and asked what you can deliver in the meantime. Specify the
survey if you think it is needed, and also ship something that works today.

**How do I tell a dead sensor from a dark pole?**
That is a core part of the assignment. `01-problem-context.md` §2 and
`02-data-and-systems.md` §2 contain everything you need to reason about it. Read
the firmware notes and the physical reasoning about what is and isn't possible
when a pole's children are still live.

**A pole with no device is on the fault boundary. Now what?**
Also part of the assignment. Your answer will probably involve reporting a range
rather than a point, and being honest about it in the UI.

**What counts as "one fault" versus two?**
Your judgement, defended in writing. Two spans failing on the same line ten
minutes apart is arguably one incident for the crew and two for the algorithm.
There is no single right answer; there are answers with reasons and answers
without.

---

## Logistics

**Deadline?**
Stated in the email that carried this brief. Seven days from receipt.

**Can I get an extension?**
Ask before the deadline, with a reason, and we will usually say yes. Asking
afterwards, we usually won't.

**Who do I contact?**
**[HIRING CONTACT EMAIL]** — for factual questions: broken links,
contradictions between documents, logistics. We will not review your approach or
tell you whether a design is right; that is the assignment.

**Is this paid work? Will you use it?**
No, and no. It is a hiring exercise. The scenario is fictional, we have no
contract with any utility, and we will not use your code. Your repo is yours —
keep it public and put it on your CV if you like.

**What happens after I submit?**
We review in batch after the deadline and reply either way within two weeks.
Shortlisted candidates get a 30-minute call — see `04-evaluation.md`.
