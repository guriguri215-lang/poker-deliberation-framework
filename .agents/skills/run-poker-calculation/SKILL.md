---
name: run-poker-calculation
description: Choose, validate, and run the framework's deterministic poker calculators. Use for pot odds, break-even folds, combo and range expansion, heads-up Hold'em equity, EV trees, ICM, zero-sum matrix games, fixed-strategy best response, hand validation, sensitivity grids, or external-solver capability checks.
---

# Run Poker Calculation

## Workflow

1. Read [references/tool-selection.md](references/tool-selection.md) and `tools/manifest.yaml`.
2. Confirm the game, units, objective, required inputs, and tool applicability.
3. Write a JSON input file. Do not interpolate untrusted text into shell commands.
4. Run `poker-deliberate calculate <tool> --analysis-scope retrospective --input <path>`.
5. Check status, exactness, assumptions, warnings, seed, samples, confidence interval, version,
   duration, and error before using the output.
6. On failure, return the failure and safe alternative; never substitute a plausible number.
7. Store the input and ToolResult in the run directory and cite the reproduction command.

## Limits

- Use exact Hold'em enumeration only below the configured evaluation bound; otherwise use a fixed
  seed and report Monte Carlo uncertainty.
- Call fixed-strategy output a best response, not a two-player equilibrium.
- Treat support-enumeration fallback output as approximate when `exact_algorithm` is false.
- Treat `solver_status=unavailable` as no result; never reuse sample or fixed values.
- Request approval before package installation, external solver execution, or long computation.
