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

Contract version 2 adds the authoritative `numeric_exactness` field:

- `exact`: no rounding is present in the declared result fields.
- `exact-under-model`: exact only under the named model qualifier.
- `floating-verified`: binary floating-point output accepted only after the typed output schema and
  the tool-specific invariants/tolerance policy pass.
- `approximate`: a non-exact method with complete method, stochastic/seed, samples or iterations,
  interval or error, and stopping-condition metadata.
- `unavailable`: no valid numeric result exists because a precondition, capability, or required
  input is absent.

The original three-value `exactness` field is retained unchanged as a version-1 compatibility
projection. It is insufficient for new decisions: consumers must migrate to `numeric_exactness`.
The mapping is explicit in the generated manifest and never promotes `failed` or `unavailable` to a
successful numerical claim. Old version-1 artifacts remain loadable; new registry executions emit
contract version `2.0.0`.

The canonical 20-tool input/output definitions live in
`poker_deliberation.tools.contracts.tool_contracts`. The complete JSON Schemas are generated into
`tools/manifest.yaml`; [Tool contracts](tool-contracts.md) is generated from the same definitions.
Strict models reject missing required fields and unknown extra fields before calculator execution,
and reject malformed output after execution.

## Numerical tolerance

There is no repository-wide epsilon. Each `floating-verified` contract declares its fields, unit,
absolute/relative/ULP/caller-supplied method, formula, rationale, and verification checks.

- Straight-line formulas use operation-count-bounded ULP policies at the output magnitude.
- Matrix games retain a caller-visible payoff-unit tolerance because conditioning depends on the
  supplied matrix; sensitivity tests vary this tolerance rather than silently loosening it.
- ICM records its model qualifier and checks prize conservation with a recursion-aware floating
  policy. It is not labelled mathematically exact.
- Monte Carlo sampling error is represented by the reported Hoeffding interval, not by a floating
  comparison tolerance. The fixed seed and fixed sample stopping condition are mandatory.
- Hand validation derives its default chip comparison tolerance from the observed magnitude and
  bounded action count. An explicit override is recorded in the input.
- Exact combinatorial combo expansion does not acquire a floating tolerance. Weighted normalization
  is separately `floating-verified`.

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
