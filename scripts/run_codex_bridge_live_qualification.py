"""Prepare and sanitize the repository-owned P2-025B subscription qualification."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
source_root_text = str(SOURCE_ROOT)
sys.path[:] = [source_root_text, *(item for item in sys.path if item != source_root_text)]

from poker_deliberation.bounded_river_call_ev_evaluation import (  # noqa: E402
    build_repository_owned_bounded_river_evaluation_admission,
)
from poker_deliberation.codex_bridge.canonical import (  # noqa: E402
    canonical_json_bytes,
    parse_canonical_model,
)
from poker_deliberation.codex_bridge.evaluation import (  # noqa: E402
    BoundedCodexBridgeEvaluationResultV1,
)
from poker_deliberation.codex_bridge.identity import (  # noqa: E402
    verify_bridge_checkout,
    verify_bridge_module_origins,
)
from poker_deliberation.codex_bridge.models import (  # noqa: E402
    BridgeRole,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.product import (  # noqa: E402
    bridge_read_summary,
    prepare_product_bridge,
    read_product_request,
    role_request_preview,
)
from poker_deliberation.codex_bridge.qualification import (  # noqa: E402
    PUBLIC_SYNTHETIC_FIXTURE_ID,
    build_sanitized_live_qualification_manifest,
    load_public_synthetic_fixture,
    write_sanitized_live_qualification_manifest,
)
from poker_deliberation.codex_bridge.storage import BoundedCodexBridgeStore  # noqa: E402
from poker_deliberation.config import AppConfig  # noqa: E402
from poker_deliberation.orchestrator import Orchestrator  # noqa: E402
from poker_deliberation.providers import LocalProvider  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/codex_bridge/v1/public-synthetic-qualification.json"
PUBLIC_MANIFEST = ROOT / "qualifications/p2-025b-codex-subscription-v1.json"
PUBLIC_EVALUATION = ROOT / "qualifications/p2-025b-deterministic-evaluation-v1.json"


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(ROOT), *arguments),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=15,
    )
    if completed.returncode != 0:
        raise ValueError("qualification Git identity probe failed")
    return completed.stdout.decode("utf-8", errors="strict").strip()


def _confined_ignored_root(value: Path) -> Path:
    root = value.resolve(strict=False)
    if root == ROOT or ROOT not in root.parents:
        raise ValueError("qualification work root must remain inside the repository")
    relative = root.relative_to(ROOT).as_posix()
    completed = subprocess.run(
        ("git", "-C", str(ROOT), "check-ignore", "-q", f"{relative}/__probe__"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=15,
    )
    if completed.returncode != 0:
        raise ValueError("qualification work root is not ignored")
    return root


def _config(work_root: Path) -> AppConfig:
    return AppConfig(
        runs_dir=work_root / "source" / "legacy",
        revision_runs_dir=work_root / "source" / "product",
        durable_budget_runs_dir=work_root / "source" / "budget",
    )


def _prepare(args: argparse.Namespace) -> int:
    fixture = load_public_synthetic_fixture(FIXTURE)
    if fixture.fixture_id != PUBLIC_SYNTHETIC_FIXTURE_ID:
        raise ValueError("qualification fixture identity mismatch")
    work_root = _confined_ignored_root(args.work_root)
    if work_root.exists():
        raise ValueError("qualification work root must be new")
    verify_bridge_checkout(
        ROOT,
        repository_commit_id=args.source_commit,
        repository_tree_id=args.source_tree,
    )
    verify_bridge_module_origins(ROOT)
    work_root.mkdir(parents=True)
    config = _config(work_root)
    admission = build_repository_owned_bounded_river_evaluation_admission(
        fixture.range_notation,
        fixture.source_terminal_run_id,
    )
    report = Orchestrator(config=config, provider=LocalProvider()).run_bounded_river_call_ev_review(
        admission
    )
    if report.run_id != fixture.source_terminal_run_id:
        raise ValueError("qualification source terminal run identity mismatch")
    bridge_root = work_root / "bridge"
    read = prepare_product_bridge(
        config=config,
        repository_root=ROOT,
        bridge_root=bridge_root,
        source_run_id=report.run_id,
        bridge_run_id=args.bridge_run_id,
        repository_commit_id=args.source_commit,
        repository_tree_id=args.source_tree,
        auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
    )
    request = read_product_request(
        repository_root=ROOT,
        bridge_root=bridge_root,
        bridge_run_id=args.bridge_run_id,
        role=BridgeRole.STRATEGY_ANALYST,
        auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
    )
    output = {
        "schema_version": "1.0.0",
        "operation": "prepared_without_model_execution",
        "fixture_id": fixture.fixture_id,
        "source_commit_id": args.source_commit,
        "source_tree_id": args.source_tree,
        "source_storage_environment": {
            "POKER_DELIBERATION_RUNS_DIR": config.runs_dir.relative_to(ROOT).as_posix(),
            "POKER_DELIBERATION_REVISION_RUNS_DIR": (
                config.revision_runs_dir.relative_to(ROOT).as_posix()
            ),
            "POKER_DELIBERATION_DURABLE_BUDGET_RUNS_DIR": (
                config.durable_budget_runs_dir.relative_to(ROOT).as_posix()
            ),
        },
        "bridge_summary": bridge_read_summary(read),
        "first_role_request": role_request_preview(request),
    }
    sys.stdout.buffer.write(canonical_json_bytes(output) + b"\n")
    return 0


def _manifest(args: argparse.Namespace) -> int:
    work_root = _confined_ignored_root(args.work_root)
    bridge_root = work_root / "bridge"
    store = BoundedCodexBridgeStore(bridge_root)
    read = store.read_current(args.bridge_run_id)
    artifacts = {item.logical_name: item.model for item in read.decoded_artifacts()}
    plan = artifacts.get("run_plan.json")
    if plan is None:
        raise ValueError("qualification run plan is missing")
    verify_bridge_checkout(
        ROOT,
        repository_commit_id=plan.repository_commit_id,
        repository_tree_id=plan.repository_tree_id,
    )
    verify_bridge_module_origins(ROOT)
    evaluation_bytes = args.deterministic_evaluation.read_bytes()
    evaluation = parse_canonical_model(
        evaluation_bytes,
        BoundedCodexBridgeEvaluationResultV1,
    )
    if (
        not evaluation.passed
        or evaluation.source_commit_id != plan.repository_commit_id
        or evaluation.source_tree_id != plan.repository_tree_id
    ):
        raise ValueError("deterministic evaluation is not bound to the qualification source")
    if args.output.resolve(strict=False) != PUBLIC_MANIFEST.resolve(strict=False):
        raise ValueError("sanitized qualification output path is not the public manifest path")
    if args.evaluation_output.resolve(strict=False) != PUBLIC_EVALUATION.resolve(strict=False):
        raise ValueError("deterministic evaluation output path is not the public artifact path")
    if args.output.exists() or args.evaluation_output.exists():
        raise ValueError("public qualification artifacts must not already exist")
    manifest = build_sanitized_live_qualification_manifest(
        read,
        repository_root=ROOT,
        qualification_id=args.qualification_id,
        deterministic_evaluation_sha256=evaluation.result_sha256,
    )
    with args.evaluation_output.open("xb") as stream:
        stream.write(evaluation_bytes)
    write_sanitized_live_qualification_manifest(args.output, manifest)
    sys.stdout.buffer.write(
        canonical_json_bytes(
            {
                "schema_version": manifest.schema_version,
                "qualification_status": manifest.qualification_status,
                "qualified_scope": manifest.qualified_scope,
                "repository_commit_id": manifest.repository_commit_id,
                "repository_tree_id": manifest.repository_tree_id,
                "runtime_source_inventory_sha256": (manifest.runtime_source_inventory_sha256),
                "manifest_sha256": manifest.manifest_sha256,
            }
        )
        + b"\n"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--source-commit", required=True)
    prepare.add_argument("--source-tree", required=True)
    prepare.add_argument("--work-root", type=Path, required=True)
    prepare.add_argument("--bridge-run-id", required=True)
    prepare.set_defaults(handler=_prepare)

    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--work-root", type=Path, required=True)
    manifest.add_argument("--bridge-run-id", required=True)
    manifest.add_argument("--deterministic-evaluation", type=Path, required=True)
    manifest.add_argument("--qualification-id", required=True)
    manifest.add_argument("--output", type=Path, default=PUBLIC_MANIFEST)
    manifest.add_argument("--evaluation-output", type=Path, default=PUBLIC_EVALUATION)
    manifest.set_defaults(handler=_manifest)
    args = parser.parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
