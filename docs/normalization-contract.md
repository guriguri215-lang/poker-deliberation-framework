# Versioned hand normalization contract

## Scope and identity

P3-014A implements one repository-owned grammar:

- contract version: `1.0.0`
- parser ID: `poker-deliberation.generic-key-value-hand`
- parser version: `1.0.0`
- source kind: `documented-key-value-hand`
- encoding: strict UTF-8
- supported site: `none`

This parser is not a natural-language or site-specific hand-history parser. It does not infer
missing cards, stacks, actions, rules, tournament context, rake timing, or site semantics.
Structured JSON remains the input form for fields outside this grammar.

## Byte and Unicode rules

The parser receives exact bytes. UTF-8 BOM, invalid UTF-8, non-NFC text, prohibited control/format
characters, recognized credential shapes, bare CR, and mixed LF/CRLF are errors. A document may use
LF or CRLF consistently and may omit its final newline. The exact accepted source bytes are never
newline-normalized before hashing.

The resource limits are:

| resource | limit |
|---|---:|
| source bytes | 1,048,576 |
| logical lines | 10,000 |
| bytes per line | 16,384 |
| `player` records | 10 |
| `action` records | 2,000 |
| returned diagnostics | 256 |
| identifier code points | 256 |

An input beyond a limit fails closed. The last diagnostic is `NRM_E_DIAGNOSTIC_LIMIT` when further
line diagnostics are omitted.

## Grammar version 1

Blank lines and lines whose first non-horizontal-whitespace character is `#` are ignored. Inline
comments are not recognized. Other lines have this shape:

```text
key: value
```

Keys contain ASCII letters and underscore and are ASCII-case-insensitive. Horizontal ASCII space or
tab is allowed around the key, colon, and value. Scalar keys occur at most once:

```text
game_type
format
table_size
small_blind
big_blind
ante
rake
hero_player_id
hero_cards
board
analysis_objective
```

The repeatable records are:

```text
player: id, position, starting_stack
action: street, actor, action, amount[, to_amount]
```

`player` and `action` use a single-record RFC 4180 subset: comma separators, double-quote quoting,
and doubled double-quote escaping. Embedded newlines and backslash escaping are unsupported.
`hero_cards` and `board` use ASCII whitespace or comma separators.

Numbers use invariant ASCII syntax `[0-9]+(?:\.[0-9]+)?`; `table_size` uses `[0-9]+`. Signs,
exponents, underscores, localized separators, `NaN`, and infinities are errors. Canonical schema
validation still enforces positive blinds, finite values, table size, player identity, card count,
and the declared enum values. `format: tournament` requires structured JSON because grammar v1
cannot represent the required tournament context.

Malformed lines, unknown keys, duplicate scalar keys, invalid CSV, invalid numbers, unsupported
format, and canonical schema violations are errors. No partial `CanonicalHand` is returned.

## Typed result and provenance

`NormalizationRequestV1`, `NormalizationDiagnosticV1`, `NormalizationProvenanceV1`, and
`NormalizationResultV1` are strict, frozen, unknown-field-rejecting contracts. A result is exactly
one of:

- `success`: one complete `CanonicalHand`, no error diagnostics, and normalized-hand provenance;
- `failed`: no hand, at least one error diagnostic, and no normalized-hand hash.

Provenance records exact source byte length and plain SHA-256. Successful results also record the
byte length and SHA-256 of compact, sorted-key UTF-8 JSON for the complete `CanonicalHand`.
These unkeyed hashes detect corruption and correlation mismatch; they are not signatures and do not
authenticate the writer.

Diagnostics contain only severity, stable code, optional line, and a closed field name. Fixed
messages are resolved from the code by repository code. Raw lines, values, secrets, CSV exceptions,
and Pydantic input/error text are not stored or displayed.

## CLI, phases, persistence, and readers

For a non-JSON `review-hand` file, CLI performs a bounded binary read and calls the byte parser.
The typed result is temporarily projected through the reserved
`_poker_normalization_result_v1` metadata key because the public `Orchestrator.run` signature and
`CaseInput` schema remain compatible. `Orchestrator.run` immediately performs strict typed
revalidation and source/hand correlation, removes the transport key, and passes the typed value to
the pure Normalization phase. Metadata is not provenance authority.

The phase revalidates the result and returns it with the normalized case. The orchestrator persists
it as canonical `normalization.json` using
`poker-normalization-result-artifact-v1`. Structured JSON hand input does not invent this artifact.
Existing runs without it remain readable, and copy-only legacy migration never manufactures
provenance.

When `normalization.json` exists, the terminal reader requires `input.json` and
`normalized_case.json`, re-encodes the persisted source text as UTF-8, recomputes the exact source
hash, recomputes normalized-hand provenance, and checks both cases against the typed result.
Unknown versions and any hash, hand, or artifact correlation mismatch fail closed.

The original role allowlists remain unchanged: only the intake role receives `raw_text`. Other roles
receive their existing structured, blind, claim, evidence, or tool-specific context projections.
Successful hand normalization continues through the registered `hand_validator`; it does not imply
site rules, GTO, equilibrium, or solver output.

## Fixtures and reproduction

The repository-owned fixture contains exact source bytes encoded as Base64 and the complete expected
typed result:

```powershell
.\.venv\Scripts\python.exe scripts\generate_normalization_fixtures.py --check
```

It covers LF, CRLF, BOM, mixed newline, non-NFC, duplicate scalar, unknown key, and exponent-number
cases. Unit, property, integration, adversarial, characterization, product reader, and unchanged
P3-017A evaluation tests cover the remaining contract and compatibility boundaries.
