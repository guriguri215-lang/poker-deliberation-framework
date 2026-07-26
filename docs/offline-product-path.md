# Offline Python product path

P2-029A completes one local-only path from retrospective input admission through verified terminal
storage and a concise user projection. It does not enable an external provider, external solver,
Codex/Python bridge, package installation, network transmission, or GTO/equilibrium claim.

## Input safety and diagnostics

`analysis_scope="unspecified"` remains the fail-closed default. A refused run records the exact
`SecurityEvent.rule_id` and presents deterministic guidance for that rule. In particular,
`scope-field-unspecified` tells the caller to declare
`analysis_scope="retrospective"`; provider and requested-tool effects remain after this check and
therefore execute zero times on refusal.

Japanese lexical screening removes only a bounded, grammatically negative live-context span such as
`今プレイ中ではありません`、`今プレイ中じゃありません`、or `今プレイ中ではなく`.
Question forms, double negation, an independent live clause, and independently recognized
real-time-instruction language remain visible to the blocking matcher. This is a deterministic
defense-in-depth rule, not a semantic Japanese-language proof.

## Redaction mapping integrity

Redaction recursively produces JSON-compatible values. A mapping key is redacted before insertion.
If that projected key is already occupied, the next key is the first unused
`<redacted key> [collision N]` for monotonically increasing `N` starting at 2.

- No entry is overwritten or discarded.
- The suffix contains only a collision ordinal; it contains no source secret, hash, or digest.
- Literal keys that already use a collision suffix are skipped rather than overwritten.
- The same rule is applied independently at every nested mapping.
- Existing non-colliding projections retain their original keys and values.

Redaction remains defense in depth. It does not authorize restricted input or claim to recognize
every secret encoding.

## ICM cached-subset floating-point bound

The calculator has `n` positive-stack active players, `L` listed output players including zero
stacks, and `p` effective payout places. `p` is at least 1 and no greater than `n`.

Because `recurse(remaining, place)` is cached, removal depth `k` has exactly
`C(n,k)` distinct non-base subset states for `k=0..p-1`. A state at depth `k` has `n-k` possible
winners. Its binary64 work is bounded as follows:

- the remaining-stack sum: `n-k` additions;
- per winner: one division, one current-prize multiplication/addition, and `L` continuation
  multiplication/additions.

Therefore the non-base state contributes at most `(n-k)*(2*L+4)` binary64 operations. The final
equity sum, payable-prize sum, and subtraction contribute `L+p+1`. The executable bound is

```text
M = (2*L+4) * sum((n-k)*C(n,k), k=0..p-1) + L + p + 1
```

This is derived from the cache states and executed loops; it is not a substitution of an
unqualified `2^n` expression.

For binary64 unit roundoff `u=2^-53`, the implementation uses
`gamma_M=(M*u)/(1-M*u)`. The DP combines non-negative terms. A four-envelope safety factor covers
the non-cancelling DP evaluation, the output and payable sums, and the final subtraction:

```text
raw_bound = 4 * gamma_M * max(1, abs(payable_prize_sum))
ulps = max(1, ceil(raw_bound / ulp(max(1, abs(payable_prize_sum)))))
tolerance = ulps * ulp(max(1, abs(payable_prize_sum)))
```

The ULP conversion rounds the analytical bound upward. Inputs and aggregate active stacks/payable
prizes must remain finite. The calculator, executable verifier, typed result metadata, generated
manifest, and tool-contract documentation use this same formula. Verification observations record
the materialized `M` and ULP count. Tests cover the 12-active/100-listed boundary, zero-stack
outputs, binary prize scaling, independent small-player `Fraction` oracles, and a schema-valid
injected error larger than the derived bound.

This verifies binary64 conservation for the implemented Independent Chip Model. It does not add
future-game simulation, risk preference, bounty modeling, a poker solver, GTO, or equilibrium.

## Concise user projection

`render_summary(FinalReport)` and CLI `--format summary` are additive display projections.

- The adjudicated conclusion and CALCULATED/A-confidence correction records appear first.
- Important successful tool fields retain their exact numeric-exactness label. A
  `floating-verified` result is included only with passed verification; an approximation is labeled
  ESTIMATE and requires its declared interval/error and stopping condition.
- Major input/tool assumptions and limitations are shown explicitly.
- Failed or unavailable tools remain limitations. `solver_status=unavailable` never becomes a
  strategy result.
- Agent prose is not copied into the summary or promoted to a verified conclusion. The summary only
  reports that unverified sections remain in the complete JSON.

Complete input, claim records, verification observations, execution audit, and complete tool output
remain in the existing verified `final_report.json`, `final_report.md`, and
`tool_results/*` payloads. No summary artifact is added to terminal inventory.

## Storage and compatibility

The ordinary `Orchestrator.run` path continues to publish the P2-012B marker-last terminal revision.
`load_report`, `report_path`, verified status mapping, resume, and legacy migration keep their
existing signatures and meanings. `final_report.json` and `final_report.md` retain their artifact
schema, canonicalization, and reader checks. The summary is rendered after a verified report read.

The separate approval, revision-storage, terminal, cleanup, phase, budget, and local-data canonical
JSON families are not consolidated by P2-029A. Their domains, versions, supported types, datetime
rules, NFC rules, parsers, and consumers are inventoried only in the ignored goal evidence. Any
non-byte-equivalent consolidation requires a separate proposal.

## Dogfood contract

The local-only dogfood runs use the ordinary Orchestrator and product revision roots for:

1. correction of a pot-odds user claim using the verified `pot_odds` result;
2. structured `CanonicalHand` validation through `hand_validator`;
3. an unsupported full-game strategy request returning honest solver unavailability and
   no GTO/equilibrium claim.

For each run the test and goal evidence verify terminal status, current revision reader,
`final_report.json` canonical bytes, report loading, concise summary, and recorded calculator input
paths. LocalProvider remains non-generative.
