Removed the Total Recall and Token Optimizer integrations: sidebar session
search no longer shells out to a third-party CLI (it already had its own
in-process scanner in `ccc_server/recent_search.py`, now the only path), the
"TR" history badge and Token Optimizer quality-score badges/pills are gone,
the Kimi-to-Total-Recall bridge script and module are removed, and the
Total Recall / Token Optimizer dashboard launchers are gone from Settings.
`/api/session/<sid>/token-sitter-checkpoint` keeps working but now always
reports no checkpoint instead of reading that tool's files. Use `ccc recall`
for cross-session search instead.
