# Decision: the bench screen is a peer approval channel

**Date:** 2026-09-05
**Status:** decided; binds the ArcticBase interaction spec (D1)
**Supersedes:** the "ArcticBase renders, the CLI decides" suggestion in
[`specs/2026-09-05-networked-workers-design.md`](specs/2026-09-05-networked-workers-design.md) §6

## The decision

Approving on the Raspberry Pi's screen carries **the same authority** as approving in
`pare-cli`. Not a display surface with the decision made elsewhere — a peer channel.
Either can answer a pending approval; first response wins.

## Why, in the operator's words

> "If I poke the screen at the bench, that should be the same approval as a CLI tool
> risk auth."

## Why that is right on safety grounds, not just ergonomics

The person best placed to approve a flash write is the one **looking at the wired-up
target**. A design that makes them walk to another terminal, or SSH in from the bench,
adds friction at precisely the moment full attention is wanted.

And friction on approvals does not make operators careful. It makes them reach for
"approve this tool for the whole session" so they stop being interrupted — trading a
series of deliberate decisions for one blanket one. A channel that is convenient at the
bench is therefore *safer* than one that is not, even though it widens who can answer.

The earlier "render there, answer here" split optimised for a threat that D2 has
already accepted (anything on the tailnet is trusted) at the cost of the behaviour that
actually protects hardware.

## What this does NOT excuse

Two problems survive this decision, because neither is about *who may answer*:

1. **What you see must be what gets dispatched.** If a screen renders "read 16 bytes at
   0x0" over a pending chip erase, approving on a trusted channel does not help — you
   approved a lie. PARE must compose the decision text itself, hand it over as bytes to
   be displayed verbatim, and carry a short digest that appears on the screen, in the
   CLI, and in the audit row. This is an integrity link and it is independent of trust.

2. **Session-scope is a different kind of act from a one-time yes.** A single tap that
   disarms a gate for every later call, producing audit rows indistinguishable from
   individual approvals, is not made safe by arriving on a trusted channel. It is
   already unavailable for `critical` tools; the open question for D1 is whether it
   should exist at all for hardware writes, where individual approval is the entire
   point of wiring a probe to something brickable.

## Consequences for the D1 spec

- ArcticBase is an **approval terminal**, not a viewer. D1 is correspondingly bigger:
  PARE must learn about a response and resolve the pending approval future.
- `Daemon._route_approval_response` never checks which channel answered. Harmless when
  the only channel was a local Unix socket; load-bearing once there are two.
- The decision text and its digest become PARE-authored artifacts, not ArcticBase
  templates.
