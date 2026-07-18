# Calculation policy

## Priority

1. Validated local exact tool.
2. Small, reviewable exact Python algorithm.
3. Approved external solver or library.
4. Seeded simulation with uncertainty.
5. Mathematical bounds and sensitivity.
6. Explicit heuristic estimate.
7. UNKNOWN.

## Result requirements

Every ToolResult records input, output, status, exactness, assumptions, version, seed, sample count,
confidence interval, duration, warnings, error, and reproduction command. A failure never becomes a
numeric result.

## Scope

- Hold'em equity is heads-up. Exact enumeration is bounded; Monte Carlo reports a conservative
  two-sided 95% Hoeffding interval for independent scores in `[0,1]`, including small samples.
- ICM is the Independent Chip Model, not a future-game simulation.
- Matrix support enumeration is exact up to recorded floating tolerance; fallback fictitious play is
  approximate and reports its duality gap.
- Extensive-form best response enumerates one action per responder information set and requires a
  fixed strategy at every opponent information set. Terminal payoff is player 0 utility in a
  two-player zero-sum game; player 1 therefore minimizes that value.
- The zero-equity bluff threshold is `risk / (risk + reward)`. Rake in a called branch is not
  silently subtracted from reward; nonzero-equity/rake branches belong in an EV tree.
- MDF uses `pot / (pot + bet)` only for the documented single-bet indifference model. It is not a
  complete defense strategy. Polar river bluff fraction uses `bet / (pot + 2*bet)` only for a
  polarized river range against a bluff-catcher with no rake.
- Raked call EV uses `equity * (pot_after_bet + call_cost - rake) - call_cost` with a declared
  percentage/cap and no future betting. Bluff EV likewise uses a single-street call-or-fold model.
- Bayes output is exact conditional on the supplied prior and likelihoods; it does not validate or
  infer those empirical inputs.
- Caller parameters cannot lift hard caps on samples, exact evaluations, ICM field size, matrix
  dimensions/iterations, game-tree nodes/depth, or pure policies.
- No local tool claims full NLHE GTO or equilibrium.
