# Hand normalization checklist

Capture what is known and mark the rest unknown:

- game and cash/tournament format;
- table size, blinds, ante, rake;
- player IDs, positions, starting and effective stacks;
- hero cards, board cards, street order;
- incremental action amounts, target amounts, pots before/after;
- all-ins, side pots, payouts, bounty, remaining players;
- supplied or explicitly assumed ranges;
- analysis objective: chipEV, dollar EV, ICM, or exploitative EV.

Before calculating, reject duplicated cards, stack underflow, impossible board, backwards streets,
pot mismatch, and known minimum-raise violations. Ask only for missing fields whose answers can
change the recommendation or enable an exact calculation.
