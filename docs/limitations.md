# Current limitations

- OpenAI Agents SDK and API key are absent in the inspected environment. The Provider boundary exists,
  but outbound model execution is intentionally not implemented.
- Developer Docs MCP could not be registered because the installed Codex executable returned access
  denied. The official Codex Manual and official OpenAI web documentation were used instead.
- The workspace contains an empty `.git` directory but no initialized Git repository. Choose either
  `git init` (and an intentional first commit) or removal of the empty directory before relying on
  Git-aware tooling; this repository does not make that ownership decision automatically.
- No external poker solver, external card library, paid range, or private dataset is bundled.
- No full NLHE game-tree equilibrium, CFR/CFR+, node locking, or solver exploitability computation.
- Equity is heads-up NLHE only; multiway and PLO equity are unsupported.
- Range syntax supports explicit combos, pairs, suited/offsuit classes, and weights; `+`, intervals,
  exclusions, and solver-native range formats are not implemented.
- Hand validation does not fully model straddles, returned uncalled bets, site-specific rake timing,
  side pots, or every jurisdictional minimum-raise rule.
- Free-text normalization supports the documented key-value/player/action format. It is not a
  natural-language or site-specific parser; unrecognized lines are preserved as warnings, not facts.
- Best response uses pure-policy enumeration and is limited to small, finite, acyclic games.
- Matrix support enumeration may use approximate fictitious play for degenerate/large unsupported
  cases; such output is never labeled exact.
- ICM has no future-game simulation, skill edge, risk preference, bounty equity model, or deal model.
- Human approval decisions persist and resume; approved external actions are not auto-executed.
- Evidence records are validated, claim-linked, stored in `evidence.jsonl`, and included in reports.
  Case-specific web retrieval itself still requires a connected agent and explicit recording.
- Local calculators run in-process. Hard size/work/depth caps prevent callers from requesting
  unbounded work, convergent best-response DAGs are memoized, and over-budget results fail closed,
  but the MVP has no OS-level preemptive CPU or memory sandbox. Providers must honor the cooperative
  cancellation contract. Any future external-code executor must use process isolation and true
  time/memory limits.
- Redaction covers common structured keys and token forms, not arbitrary personal information or
  every possible secret encoding.
- `audit-claim` without structured calculation inputs preserves a USER_CLAIM as unverified rather
  than guessing its truth.
