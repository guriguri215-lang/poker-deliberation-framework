from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from poker_deliberation.codex_bridge.identity import (
    BridgeIdentityError,
    bridge_runtime_source_inventory,
    bridge_runtime_source_inventory_sha256,
    verify_bridge_checkout,
    verify_bridge_module_origins,
)
from tests.codex_bridge_support import REPOSITORY_ROOT


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *arguments),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=15,
    )
    return completed.stdout.decode("utf-8").strip()


def _commit(root: Path, message: str) -> tuple[str, str]:
    _git(root, "add", "tracked.txt")
    _git(
        root,
        "-c",
        "user.name=Codex Bridge Test",
        "-c",
        "user.email=codex-bridge@example.invalid",
        "commit",
        "-m",
        message,
    )
    return _git(root, "rev-parse", "HEAD"), _git(root, "rev-parse", "HEAD^{tree}")


def test_checkout_gate_requires_exact_clean_identity_and_normal_index(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("one\n", encoding="utf-8", newline="\n")
    commit_id, tree_id = _commit(tmp_path, "initial")

    verify_bridge_checkout(
        tmp_path,
        repository_commit_id=commit_id,
        repository_tree_id=tree_id,
    )
    tracked.write_text("dirty\n", encoding="utf-8", newline="\n")
    with pytest.raises(BridgeIdentityError, match="checkout binding mismatch"):
        verify_bridge_checkout(
            tmp_path,
            repository_commit_id=commit_id,
            repository_tree_id=tree_id,
        )
    tracked.write_text("one\n", encoding="utf-8", newline="\n")
    _git(tmp_path, "update-index", "--assume-unchanged", "tracked.txt")
    with pytest.raises(BridgeIdentityError, match="checkout binding mismatch"):
        verify_bridge_checkout(
            tmp_path,
            repository_commit_id=commit_id,
            repository_tree_id=tree_id,
        )


def test_checkout_gate_rejects_replace_refs(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("one\n", encoding="utf-8", newline="\n")
    first_commit, _first_tree = _commit(tmp_path, "first")
    tracked.write_text("two\n", encoding="utf-8", newline="\n")
    second_commit, second_tree = _commit(tmp_path, "second")
    _git(tmp_path, "replace", second_commit, first_commit)

    with pytest.raises(BridgeIdentityError, match="checkout binding mismatch"):
        verify_bridge_checkout(
            tmp_path,
            repository_commit_id=second_commit,
            repository_tree_id=second_tree,
        )


def test_bridge_modules_originate_in_repository_checkout() -> None:
    verify_bridge_module_origins(REPOSITORY_ROOT)


def test_runtime_source_inventory_is_sorted_and_covers_role_and_request_authority() -> None:
    inventory = bridge_runtime_source_inventory(REPOSITORY_ROOT)
    by_path = {item.path: item for item in inventory}
    agent_paths = {
        ".codex/agents/adjudicator.toml",
        ".codex/agents/calculator-builder.toml",
        ".codex/agents/evidence-researcher.toml",
        ".codex/agents/intake-reconstructor.toml",
        ".codex/agents/math-tool-auditor.toml",
        ".codex/agents/poker-orchestrator.toml",
        ".codex/agents/report-writer.toml",
        ".codex/agents/skeptic-falsifier.toml",
        ".codex/agents/strategy-analyst.toml",
    }
    request_authority_paths = {
        ".agents/skills/audit-poker-claim/SKILL.md",
        ".agents/skills/review-poker-hand/SKILL.md",
        ".agents/skills/run-poker-calculation/SKILL.md",
        "src/poker_deliberation/capabilities.py",
        "tests/fixtures/codex_bridge/v1/public-synthetic-qualification.json",
    }
    deterministic_workflow_paths = {
        "scripts/run_bounded_river_review_workflow_evaluation.py",
        "tests/fixtures/bounded_river_review_workflow/v1/range.json",
        "tests/fixtures/bounded_river_review_workflow/v1/source-ja.txt",
        "tests/fixtures/bounded_river_review_workflow/v2/scenarios.json",
    }

    assert tuple(item.path for item in inventory) == tuple(sorted(item.path for item in inventory))
    assert agent_paths | request_authority_paths | deterministic_workflow_paths <= by_path.keys()
    assert "src/poker_deliberation/codex_bridge/qualification.py" in by_path
    assert "src/poker_deliberation/public_preflight.py" in by_path
    for relative in agent_paths | request_authority_paths | deterministic_workflow_paths:
        raw = REPOSITORY_ROOT.joinpath(*relative.split("/")).read_bytes()
        assert by_path[relative].size == len(raw)
        assert by_path[relative].sha256 == hashlib.sha256(raw).hexdigest()
    assert len(bridge_runtime_source_inventory_sha256(REPOSITORY_ROOT)) == 64


def _minimal_runtime_inventory_root(root: Path) -> Path:
    files = {
        ".agents/skills/audit-poker-claim/SKILL.md": b"---\nname: audit-poker-claim\n---\n",
        ".agents/skills/review-poker-hand/SKILL.md": b"---\nname: review-poker-hand\n---\n",
        ".agents/skills/run-poker-calculation/SKILL.md": b"---\nname: run-poker-calculation\n---\n",
        ".codex/agents/strategy-analyst.toml": b'name = "strategy-analyst"\n',
        "pyproject.toml": b"[project]\nname = 'inventory-fixture'\n",
        "requirements.lock": b"fixture==1.0\n",
        "scripts/run_codex_bridge_live_qualification.py": b"# fixture runner\n",
        "scripts/run_bounded_river_review_workflow_evaluation.py": b"# workflow fixture runner\n",
        "src/poker_deliberation/capabilities.py": b"CAPABILITIES = ()\n",
        "tests/fixtures/codex_bridge/v1/public-synthetic-qualification.json": b"{}",
        "tests/fixtures/bounded_river_review_workflow/v1/range.json": b"{}",
        "tests/fixtures/bounded_river_review_workflow/v1/source-ja.txt": b"fixture\n",
        "tests/fixtures/bounded_river_review_workflow/v2/scenarios.json": b"{}",
    }
    for relative, raw in files.items():
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return root


@pytest.mark.parametrize(
    "relative",
    [
        ".codex/agents/strategy-analyst.toml",
        ".agents/skills/review-poker-hand/SKILL.md",
        "src/poker_deliberation/capabilities.py",
        "tests/fixtures/codex_bridge/v1/public-synthetic-qualification.json",
        "scripts/run_bounded_river_review_workflow_evaluation.py",
        "tests/fixtures/bounded_river_review_workflow/v2/scenarios.json",
    ],
)
def test_runtime_source_inventory_hash_changes_when_new_authority_bytes_change(
    tmp_path: Path,
    relative: str,
) -> None:
    root = _minimal_runtime_inventory_root(tmp_path)
    before = bridge_runtime_source_inventory_sha256(root)
    target = root.joinpath(*relative.split("/"))

    target.write_bytes(target.read_bytes() + b"mutation")

    assert bridge_runtime_source_inventory_sha256(root) != before
