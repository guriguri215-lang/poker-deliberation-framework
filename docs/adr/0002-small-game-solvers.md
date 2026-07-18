# ADR 0002: 小規模ゲームだけをローカル厳密計算する

- Status: Accepted
- Date: 2026-07-17

## Decision

- 二人ゼロ和行列ゲームは小さなsupportを列挙し、候補を線形方程式で解いて鞍点条件を検証する。
- 固定相手戦略への展開形best responseは、best-response側の各information setに同一行動を
  割り当てる純粋方策を列挙し、chanceと固定相手戦略の期待値を厳密評価する。
- 組合せ爆発を防ぐ上限を持ち、超過時は失敗理由と外部ソルバー要件を返す。
- full NLHE equilibrium、CFR、近似GTOはMVPで生成しない。

## Rationale

純粋方策列挙は小規模ゲームでは遅いが明快で、information-set制約を破る局所貪欲法より安全で
ある。大規模ゲームへの一般化可能性を主張しないことで、偽の均衡結果を防ぐ。
