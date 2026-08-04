# Problem Context — How the Network Works

Read this before you design anything. Most of the difficulty of the assignment
comes from the physical shape of the network, and you cannot design the
algorithm without understanding it.

> Everything below is the domain model for this exercise. It is a simplified
> but faithful picture of how low-voltage distribution works in Indian cities.
> Where we have simplified, we say so.

---

## 1. The network is a tree, not a mesh

Power reaches a domestic customer through a chain that looks like this:

```
  66/11 kV Substation
        │
        ├── 11 kV Feeder ──────────────┬──────────────┬─────────────
        │                              │              │
   Distribution              Distribution       Distribution
   Transformer (DT)          Transformer        Transformer
   11kV → 400/230V
        │
        ├─ LT line, on poles ─── P-1 ─── P-2 ─── P-3 ─── P-4
        │                                 │
        │                                 └─── P-5 ─── P-6   (branch/spur)
        │
        └─ another LT line ───── P-20 ── P-21 ── P-22

   Houses tap off individual poles via service drops.
```

The important property: **the low-tension side is radial.** There are no loops.
Every pole has exactly one path back to its distribution transformer, and every
DT has exactly one path back to the substation.

That single fact is what makes this problem solvable. It also tells you what
shape your core data structure should be.

## 2. What happens physically when a fault occurs

A fault is almost always on a **span** — the stretch of wire between two
adjacent poles — or at a **piece of equipment** (the DT itself, a fuse, a
jumper connection at a pole).

When a span fails, everything electrically downstream of it goes dark.
Everything upstream stays live.

```
   DT ── P-1 ── P-2 ──╳── P-3 ── P-4
                       │
                    fault here
                       │
   P-1: live      P-3: dark
   P-2: live      P-4: dark
```

So the observable signature of a fault is a **boundary**: the last live pole,
and the first dark pole beyond it. The fault is on the span between them.

Notice what this means for your algorithm. The fault is on an **edge**. Your
sensors report on **nodes**. You are inferring edge state from node state, and
the answer is the frontier between the live region and the dark region.

Notice also what it means for alerting. One snapped wire produces dozens of
dark poles. All of them are symptoms of a single cause. If your system reports
each dark pole as its own incident, you have built a system that makes the
control room's night worse.

### Multiple simultaneous faults

During a storm, three spans in the same division can fail within minutes. Each
one produces its own live/dark boundary. Your localization has to find *all*
the boundaries, not just one — and it must not merge two genuinely separate
faults into one ticket, or split one fault into two.

### Faults that are not on a span

Some things you should expect to see and be able to distinguish:

| What broke | What you observe |
|-----------|------------------|
| Span between two poles | Live/dark boundary mid-line |
| Distribution transformer / its HT fuse | Every pole under that DT goes dark at once, with no live pole beneath it |
| 11 kV feeder | Every pole under every DT on that feeder goes dark |
| A single pole's own lamp circuit | One pole dark, everything downstream of it still live — **not an outage**, a broken sensor point |

That last row is worth staring at. A single isolated dark pole with live
children is physically impossible as a line fault. It is the network telling
you your sensor is lying.

## 3. Why the control room currently takes two hours

The existing process:

1. Householders notice the power is out. Some fraction of them call the
   complaint number over the next 20–40 minutes.
2. The operator plots the complaints roughly on a paper map or a spreadsheet
   of ward names and guesses which DT is involved.
3. A lineman is sent to the area on a two-wheeler.
4. He starts at the DT and walks or rides the line, checking poles, until he
   finds the break. On a long LT line with spurs, this is slow.
5. Only now does the control room know what is actually broken, so only now can
   they dispatch the right vehicle, ladder, wire, and crew size.

Steps 1–4 are what you are compressing. Step 5 onwards is the department's own
operation and is explicitly out of scope.

## 4. The messy reality you must design around

These are real properties of the deployment, not hypotheticals.

**A device that loses power cannot talk for long.** Each pole device has a
small capacitor-backed reserve. When it loses supply it can transmit for a few
seconds — enough for one message. After that it is silent until power returns.
Design implications: you may get exactly one "I am dying" packet, or you may
get none if the radio was busy. Silence is ambiguous — a pole that stops
reporting might be dark, or its modem might be broken.

**Some poles have no device at all.** Coverage is incomplete. Your topology has
gaps, and the fault may be on a span between two poles neither of which reports.

**Devices fail on their own.** A meaningful fraction of the fleet is offline at
any given moment for reasons unrelated to power.

**Load shedding is scheduled and routine.** Whole feeders are taken down on
purpose. These are not faults and must not generate tickets.

**Clocks disagree and messages arrive out of order.** Two poles that lost power
in the same instant may report timestamps a minute apart, and the downstream
one may arrive first.

**The wiring diagram is incomplete.** This is the big one, and it is deliberate.
See `02-data-and-systems.md` §3. You know where every pole *is*. You do not
reliably know which pole feeds which. For a substantial share of distribution
transformers, nobody ever digitized the order of poles along the line.

That last constraint is the heart of the assignment. You cannot walk a tree you
do not have. What you do about it — infer it from geography, ask the department
for a survey, degrade to a coarser answer, or something we haven't thought of —
is the most interesting design decision you will make, and we will read it
first.

## 5. What "good" looks like to the customer

The department will consider this a success if:

- Time from fault occurring to control room knowing the location is **under two
  minutes**, versus two hours today.
- The location is precise enough to drive to and hand to a crew — a span, not a
  ward.
- Operators trust the alerts. A system that fires on load shedding and dead
  modems gets ignored, and an ignored system has zero value.
- Ticket closure reflects reality. "Fixed" means the poles are live again, as
  measured, not as claimed.

Now go read [`02-data-and-systems.md`](02-data-and-systems.md) for the actual
data contracts.
