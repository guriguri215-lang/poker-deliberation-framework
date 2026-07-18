import math

from poker_deliberation.tools.best_response import best_response_to_fixed_strategy


def _kuhn_game() -> tuple[dict[str, object], dict[str, dict[str, float]]]:
    ranks = {"J": 0, "Q": 1, "K": 2}
    nodes: dict[str, object] = {}
    deals: dict[str, object] = {}
    for p0 in ranks:
        for p1 in ranks:
            if p0 == p1:
                continue
            prefix = f"{p0}{p1}"
            start = f"{prefix}-p0-start"
            deals[prefix] = {"probability": 1 / 6, "child": start}
            p1_after_check = f"{prefix}-p1-after-check"
            p1_after_bet = f"{prefix}-p1-after-bet"
            p0_after_check_bet = f"{prefix}-p0-after-check-bet"
            nodes[start] = {
                "type": "player",
                "player": 0,
                "information_set": f"p0-start-{p0}",
                "actions": {"check": p1_after_check, "bet": p1_after_bet},
            }
            showdown_one = 1 if ranks[p0] > ranks[p1] else -1
            showdown_two = 2 if ranks[p0] > ranks[p1] else -2
            nodes[p1_after_check] = {
                "type": "player",
                "player": 1,
                "information_set": f"p1-after-check-{p1}",
                "actions": {"check": f"{prefix}-cc", "bet": p0_after_check_bet},
            }
            nodes[f"{prefix}-cc"] = {"type": "terminal", "payoff": showdown_one}
            nodes[p0_after_check_bet] = {
                "type": "player",
                "player": 0,
                "information_set": f"p0-after-check-bet-{p0}",
                "actions": {"fold": f"{prefix}-cbf", "call": f"{prefix}-cbc"},
            }
            nodes[f"{prefix}-cbf"] = {"type": "terminal", "payoff": -1}
            nodes[f"{prefix}-cbc"] = {"type": "terminal", "payoff": showdown_two}
            nodes[p1_after_bet] = {
                "type": "player",
                "player": 1,
                "information_set": f"p1-after-bet-{p1}",
                "actions": {"fold": f"{prefix}-bf", "call": f"{prefix}-bc"},
            }
            nodes[f"{prefix}-bf"] = {"type": "terminal", "payoff": 1}
            nodes[f"{prefix}-bc"] = {"type": "terminal", "payoff": showdown_two}
    game: dict[str, object] = {
        "root": "deal",
        "nodes": {"deal": {"type": "chance", "actions": deals}, **nodes},
    }
    fixed: dict[str, dict[str, float]] = {}
    for card in ranks:
        fixed[f"p1-after-check-{card}"] = {"check": 0.0, "bet": 1.0}
        fixed[f"p1-after-bet-{card}"] = {"fold": 0.0, "call": 1.0}
    return game, fixed


def test_best_response_keeps_same_action_per_information_set() -> None:
    game = {
        "root": "deal",
        "nodes": {
            "deal": {
                "type": "chance",
                "actions": {
                    "high": {"probability": 0.5, "child": "high"},
                    "low": {"probability": 0.5, "child": "low"},
                },
            },
            "high": {
                "type": "player",
                "player": 0,
                "information_set": "hidden",
                "actions": {"a": "ha", "b": "hb"},
            },
            "low": {
                "type": "player",
                "player": 0,
                "information_set": "hidden",
                "actions": {"a": "la", "b": "lb"},
            },
            "ha": {"type": "terminal", "payoff": 1},
            "hb": {"type": "terminal", "payoff": 0},
            "la": {"type": "terminal", "payoff": 0},
            "lb": {"type": "terminal", "payoff": 1},
        },
    }
    result = best_response_to_fixed_strategy(game, {})
    assert result["value"] == 0.5
    assert len(result["pure_policy"]) == 1
    assert result["equilibrium_claim"] is False


def test_kuhn_best_response_to_fixed_always_bet_call_strategy() -> None:
    game, fixed = _kuhn_game()
    result = best_response_to_fixed_strategy(game, fixed)
    assert result["information_set_constraint_enforced"] is True
    assert result["evaluated_policies"] == 64
    assert math.isclose(result["value"], 1 / 3, abs_tol=1e-9)


def test_player_one_minimizes_player_zero_payoff() -> None:
    game = {
        "root": "decision",
        "nodes": {
            "decision": {
                "type": "player",
                "player": 1,
                "information_set": "p1",
                "actions": {"help-p0": "win", "hurt-p0": "lose"},
            },
            "win": {"type": "terminal", "payoff": 1},
            "lose": {"type": "terminal", "payoff": -1},
        },
    }
    result = best_response_to_fixed_strategy(game, {}, best_responder=1)
    assert result["pure_policy"] == {"p1": "hurt-p0"}
    assert result["player0_value"] == -1
    assert result["best_responder_value"] == 1


def test_convergent_dag_is_memoized_per_policy() -> None:
    nodes: dict[str, object] = {"end": {"type": "terminal", "payoff": 1}}
    child = "end"
    for index in range(200):
        node_id = f"chance-{index}"
        nodes[node_id] = {
            "type": "chance",
            "actions": {
                "left": {"probability": 0.5, "child": child},
                "right": {"probability": 0.5, "child": child},
            },
        }
        child = node_id
    result = best_response_to_fixed_strategy({"root": child, "nodes": nodes}, {}, best_responder=0)
    assert result["value"] == 1
    assert result["policy_node_work_upper_bound"] == 201
