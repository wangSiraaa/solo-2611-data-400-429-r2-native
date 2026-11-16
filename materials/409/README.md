# In-place delta pages and transaction recovery

Run `/opt/homebrew/bin/python3.11 -B probe.py`. The probe copies actual seed pages, executes scripts in child processes, exits those processes with os._exit(73), and opens the resulting files in fresh recovery processes. Work directories are disposable. Source seed is initialization material, not an input to recover(ROOT).

Normal committed/no-force work recovers correctly. A page containing both uncommitted alpha changes and a committed beta change is really flushed; the original committed-only recovery leaves alpha=101. An abort can be durable while its inverse changes have not yet reached the page. A writer may also flush page_lsn=2 while the durable WAL is empty: this cannot be repaired after the fact and must be prevented by WAL-before-page ordering or rejected if received as an invalid old directory.

The fixed diagnostic is intentionally restricted to L/alpha in the two-ADD fixture. It proves actual logged inverse deltas can survive a process exit after log force/before page write, or after a persisted first inverse. It does not implement generic transaction analysis, READY publication, writer coordination, arbitrary pages, or later business reuse. The target must handle those.

The legal input family has arbitrary valid existing initial row values. The supplied seed only prepares these example directories; no original seed/baseline copy is supplied to a running recover operation.
