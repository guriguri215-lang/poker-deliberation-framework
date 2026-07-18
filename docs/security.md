# Security

- Workspace-write is the default maximum; analysis agents are read-only.
- Run IDs and artifact paths are validated and resolved under the configured run root.
- `.env` is ignored and only `.env.example` exists. With `record_sensitive_data=false`, structured
  secret keys plus common API-key/Bearer/token shapes are redacted from artifacts and CLI reports.
  Redaction is defense in depth, so users must still avoid placing arbitrary secrets in poker input.
- Shell execution is outside the model-facing runtime. Inputs are JSON, not interpolated commands.
- Web, GitHub, README, issue, and hand-history instructions are treated as untrusted data.
- Tools have non-overridable aggregate work estimates (including support combinations and
  policy-node work), memoized DAG evaluation, plus serialized input/output limits; failures remain
  failures. RunStore enforces per-artifact and whole-run byte budgets.
- Providers receive a cooperative deadline/cancellation control and role-specific deep-copy context.
  Strings matched by deterministic prompt-injection rules are replaced by hash-tagged removal
  markers before any provider call. This is best-effort lexical detection, not a semantic guarantee.
  The only outbound provider is disabled; no external/untrusted executable is run.
- Hand strategy analysis uses a mechanically verified decision-time payload. The focal action size,
  later streets, realized result, showdown-only cards, and user claims are excluded. Each payload is
  represented in the execution audit by its SHA-256 hash. Unprovenanced `known_ranges` are excluded
  until the schema can establish that they were available at the focal decision.
- The application is retrospective-only. Every orchestrated case with an unspecified scope fails
  closed, regardless of kind or input representation. Review commands declare retrospective scope;
  direct `calculate` CLI calls require `--analysis-scope retrospective`. Explicit live scope and
  recognized live-decision language, private-card acquisition, collusion, automated play, and
  detection-evasion requests are refused before provider or requested calculator execution.
  Free-text language detection is best-effort defense in depth, so callers must not mislabel live
  input as retrospective. Direct `ToolRegistry.execute` is a trusted internal primitive, not a
  policy boundary. Typed `SecurityEvent` artifacts record the rule and input hash, not a copied
  harmful excerpt.
- External code, packages, services, long compute, destructive changes, secret access, paid data, and
  objective changes require an ApprovalRequest.
- Rejected actions use a no-action path. Approved external actions are recorded but not automatically
  executed by the MVP.
- Input approval proposals cannot set decision status/timestamps. They are recreated as PENDING and
  only `resume` may decide them. Environment-configured run roots must remain inside the workspace.
- Reproduction instructions are stored as JSON argv, and unknown tool names never produce a shell
  command.

Adversarial tests also cover blind-context invariance, prohibited-use refusal, prompt-injection event
recording, pre-approved injection, fake provider claims, secret canaries, command injection names,
hard compute limits, runtime overruns, duplicate run IDs, and outside-root config.
