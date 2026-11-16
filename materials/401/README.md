# Adaptive LEFT JOIN pipeline

    python3 main.py fixtures/normal.json
    python3 main.py fixtures/fanout.json
    python3 main.py fixtures/sparse.json

Uses Python 3.11 only. Sources.left is one-shot; Sources.right may be rescanned and is immutable. Application source access uses those iterators, not private fixture arrays. Audit counts actual API row visits, not physical disk reads or file JSON decoding. A right row consumed during nested probing, hash build, discarded work or rescan counts each time. Cache/hash probes are reported separately and are not source row visits. A row returned by Executor.next is immediately visible; later plan changes cannot retract it. Source occurrence id is unique on its side; equal keys/values with different ids remain different occurrences. NULL never matches NULL. LEFT JOIN produces every matching pair or one null-extended left row if there are no matches. Source order defines left order and right order within each left. This starter's ordinary nested loop is operational; the two separate runtime mistakes are using produced rows as scan-work telemetry, and restarting earlier output after switching physical operators. This is not an external-memory join or a general cost-based SQL optimizer.
