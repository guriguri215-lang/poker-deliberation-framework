# Calculation policy

## Priority

1. Validated local tool with the strongest applicable numeric contract.
2. Small, reviewable integer/rational algorithm whose declared outputs require no rounding.
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

The canonical 22-tool input/output definitions live in
`poker_deliberation.tools.contracts.tool_contracts`. The complete JSON Schemas are generated into
`tools/manifest.yaml`; [Tool contracts](tool-contracts.md) is generated from the same definitions.
Strict models reject missing required fields and unknown extra fields before calculator execution,
and reject malformed output after execution.

`hand_pot_ledger` is `exact-under-model`: it converts the explicitly selected profile's canonical
decimal chip unit to bounded integers, requires conservation, and compares its output with a
separate `Fraction`/integer oracle. This label does not imply site conformance, winner evaluation,
GTO, equilibrium, or support for an unselected rule profile.

## Numerical tolerance

There is no repository-wide epsilon. Each `floating-verified` contract declares its fields, unit,
absolute/relative/ULP/caller-supplied method, formula, rationale, and verification checks.

The registry executes those checks after typed output validation. A result is rejected if the
executable verifier is absent, its check inventory differs from the canonical contract, or any
observed invariant exceeds the applied policy. Successful metadata records the executed checks,
observed values, and the per-result policy; declaration text alone is not verification evidence.

- `absolute`: `abs(actual - expected) <= absolute` in the declared field unit.
- `relative`: `abs(actual - expected) <= relative * max(abs(actual), abs(expected))`.
- `absolute-or-relative`: the larger of the declared absolute and relative bounds applies.
- `ulp`: `ulps * ulp(max(abs(actual), abs(expected), 1))` applies to binary64 fields.
- `caller-supplied`: the contract formula resolves the input value and any documented
  magnitude-scaled floating floor; the resolved absolute bound is recorded in the result.

No comparison inherits Python's implicit `math.isclose` relative tolerance. Input probability
normalization for EV trees and fixed-strategy games uses the same tool-specific ULP policy recorded
by the contract.

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
- Matrix support enumeration examines the bounded support set and accepts a candidate only when its
  residuals and duality gap pass the recorded floating tolerance; fallback fictitious play is
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
- Bayes output is a deterministic binary64 evaluation conditional on the supplied prior and
  likelihoods; it does not validate or infer those empirical inputs.
- Caller parameters cannot lift hard caps on samples, complete-enumeration evaluations
  (`max_exact_evaluations`), ICM field size, matrix
  dimensions/iterations, game-tree nodes/depth, or pure policies.
- No local tool claims full NLHE GTO or equilibrium.
