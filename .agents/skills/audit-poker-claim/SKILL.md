---
name: audit-poker-claim
description: Audit mathematical, probabilistic, game-theoretic, solver, range, and poker-strategy claims. Use when Codex must verify or correct pot odds, equity, EV, combos, ICM, best-response, equilibrium, GTO, exploitability, or software/API assertions with explicit assumptions, calculations, counterexamples, and claim-level evidence.
---

# Audit Poker Claim

## Workflow

1. State the USER_CLAIM exactly and identify whether it is mathematical, empirical, strategic,
   software-related, or mixed.
2. Read [references/epistemic-policy.md](references/epistemic-policy.md).
3. List material assumptions and the objective function.
4. Select a deterministic calculator where possible. Record exact input, output, version, and
   reproduction command.
5. For current software, rules, or external facts, prefer official primary sources and map each
   source to claim IDs.
6. Ask the skeptic for a concrete counterexample or changed premise, not generic disagreement.
7. Correct errors explicitly: wrong claim, error type, formula or counterexample, corrected result,
   validity range, and remaining uncertainty.
8. If the available evidence cannot decide the claim, return UNKNOWN rather than a consensus guess.

## Prohibitions

- Never use agent vote count as truth evidence.
- Never label a solver-free large poker-tree answer GTO, equilibrium, or exact range.
- Never fabricate precision, citations, ranges, solver output, or tool success.
- Never save or request private chain-of-thought.
