Added `ccc spawn --continue-from <sid|prefix>` and `ccc send <sid> --new-if-large-and-stale`:
resuming an old session reloads its whole transcript into context (a real
case re-loaded 430k tokens to do a few merges), so these spawn a fresh
session that continues from a short brief instead. `--continue-from`
resolves to the session's latest continuation successor, reuses its
cwd/engine/model/effort, and rebinds any children or WatchTower tickets
still reporting to the old chain onto the new session.
`--new-if-large-and-stale` sends normally unless the target is both large
(context tokens, `--large-threshold`/`$CCC_LARGE_CONTEXT_TOKENS`, default
150k) and stale (idle time, `--stale-seconds`/`$CCC_STALE_SECONDS`, default
1h — the prompt-cache TTL); `--dry-run` previews the decision either way.
Any message later addressed to an old session with a recorded continuation
now automatically forwards to its latest successor at delivery time (child
reports, WatchTower ticket notices, peer messages alike), following chains
and skipping sessions still actively working their own turn.
