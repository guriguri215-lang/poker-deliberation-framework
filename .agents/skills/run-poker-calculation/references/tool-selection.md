# Tool selection

- `pot_odds`: exact call threshold after a bet and optional expected rake.
- `break_even_fold`: zero-equity bluff threshold; use `ev_tree` when called branches have equity.
- `mdf`: one-bet minimum-defense indifference threshold, not a complete strategy.
- `effective_stack` and `spr`: supplied decision-point stack and pot geometry.
- `rake_amount` and `raked_call_ev`: declared percentage/cap and no-future-betting call EV.
- `bluff_ev`: single-street call-or-fold bluff or semi-bluff EV.
- `polar_river_bluff_fraction`: polarized river toy-model indifference fraction.
- `bayes_update`: posterior conditional on supplied prior and likelihood assumptions.
- `pot_reconstruction`: exact pot sequence from a starting pot and incremental contributions.
- `combos`: pair/suited/offsuit expansion, blockers, weights, normalization.
- `holdem_equity`: heads-up NLHE exact enumeration or seeded Monte Carlo.
- `ev_tree`: fully supplied finite action probabilities and terminal payoffs.
- `icm`: independent-chip-model payouts, not future-game simulation.
- `matrix_game`: small two-player zero-sum matrix equilibrium and duality gap.
- `fixed_strategy_best_response`: small finite game with chance, information sets, and a fully fixed
  opponent strategy.
- `hand_validator`: canonical card, action, pot, and stack checks.
- `sensitivity`: bounds and influence ranking over a supplied parameter grid.
- `solver_status`: capability discovery only; unavailable means no equilibrium output exists.
