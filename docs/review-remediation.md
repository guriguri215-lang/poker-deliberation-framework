# Correctness and security hardening

This document records externally useful behavior and boundary changes. The corresponding regression
tests are part of the repository.

## Mathematics and game theory

- Blind postings now establish the outstanding bet; calls, checks, raises, short all-ins, street
  resets, and repeat action by all-in players share one betting-state model.
- Fixed-strategy best response declares terminal payoff as player 0 zero-sum utility and correctly
  minimizes it for player 1.
- Monte Carlo equity uses a conservative Hoeffding interval, including one-sample `[0,1]` behavior;
  impossible dead-card boards fail before enumeration.
- Called-branch rake was removed from the zero-equity bluff threshold. ICM rejects silently truncated
  nonzero prizes and fields above its hard limit. Sensitivity settings use canonical JSON keys and
  are described as association, not causation.

## Orchestration and misinformation

- Input claims are normalized to `USER_CLAIM/C`; typed claim checks require finite values and
  nonnegative tolerance.
- Provider contexts are role-specific allowlists. Provider prose/claims remain `UNKNOWN`, are
  excluded from the adjudicated conclusion, and generate disputes until verified.
- Input approval metadata is parsed as an `ApprovalProposal`; decision fields are ignored and the
  application creates a new PENDING request.
- Evidence records now flow through validation, `evidence.jsonl`, claim-ID checks, and FinalReport.
- Confidence cannot be raised by an unrelated exact tool when material claims, invalid hand data, or
  unresolved disputes remain. Approval-required/limited/completed status is explicit.

## Security and dependencies

- Caller-controlled work parameters have hard ceilings; the registry enforces serialized input and
  output limits, aggregate matrix/policy-node estimates reject combinatorial work, and convergent
  DAGs are memoized. Runtime overruns end in auditable `FAILED_WITH_LIMITATIONS` artifacts.
- Common structured secrets and token patterns are redacted across stored inputs, tool results,
  approvals, reports, and direct calculate output.
- Unknown/unsafe tool names cannot create copy-executable reproduction strings. Known reproduction
  instructions use JSON argv with the actual configured run root.
- New runs reserve their ID exclusively. Environment-configured run roots cannot escape the current
  workspace.
- Provider contexts are deep-isolated and cancellable; RunStore enforces per-artifact and whole-run
  byte budgets. Final-report redaction covers auto hand results, claim/evidence text, and dynamic keys.
- Build requirements are exact-pinned and the documented setup installs `requirements.lock` before
  a no-dependency/no-build-isolation editable install.

## Test and reproducibility coverage

Regression tests cover every item above, manifest/registry parity, lock/build parity, evidence
persistence, nondefault run roots with spaces, free-text normalization, and duplicate-run protection.
Default canonical calculators have a direct-child wall-time boundary, but process-tree, CPU, and
memory isolation are intentionally not claimed. Noncanonical `phase_isolated=False` diagnostic
tools remain in-process, and any future external executor needs its own resource isolation.

## Comparative framework hardening

A comparison with `pokerframe` informed the following behavior. The implementation is native to this
codebase and does not copy that project's dataclass architecture:

- `FocalDecision`, pre-action `DecisionSnapshot`, and `BlindDecisionContext`. The hand validator now
  records `pot_before`, requested and actual call, uncalled-excess-adjusted contestable pot,
  pre-action stack, prior history, and side-pot risk. The hand strategy analyst receives only this
  blind context; serialization is checked for forbidden keys and result/claim fragments. Ranges
  without decision-time provenance are excluded from the blind payload.
- Retrospective-only screening fails closed for every unspecified orchestrated case and refuses
  recognized live assistance, private-card acquisition, collusion, automated play, and detection
  evasion before providers or requested tools run. Direct calculate CLI calls also require explicit
  retrospective scope. Strings matched by deterministic prompt-injection rules remain inert in
  provider payloads; lexical detection is documented as best effort. Typed `SecurityEvent` records
  preserve the matched rule and input hash without copying the instruction.
- Result-oriented rationales are detected deterministically. Only the outcome-as-proof rationale is
  rejected; the underlying action remains undecided until decision-time inputs support it.
- Every provider call now writes an `AgentExecutionRecord` with provider/version, allowed tools,
  context SHA-256, timestamps, status, and error. `DeterministicMockProvider` exercises this path
  without a live API.
- Exact tools were added for effective stack, SPR, MDF, declared rake, raked call EV, bluff EV,
  polarized river bluff fraction, and Bayes updates.

The compared ICM shove/fold helper was not adopted as a generally exact tool: it is a single-caller
toy model driven by supplied call frequency and called equity. Japanese grammar expansion, range
provenance compatibility, and live-solver adapter qualification remain separate design projects.
