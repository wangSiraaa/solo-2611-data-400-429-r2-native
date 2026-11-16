# Extended-query pipeline subset

    python3 main.py fixtures/normal.json
    python3 main.py fixtures/skip.json

Only Python 3.11 stdlib. backend.py exchanges actual framed bytes on a socket pair and fragments writes into 3-byte pieces. P payload is statement-name NUL SQL; B payload is portal NUL statement-name; E payload is portal; S is Sync; X closes. Backend 1/2 are ParseComplete/BindComplete, T/D are column/row JSON, C command tag, E error text, Z one-byte transaction state I/T/E. This framing/lifecycle subset is explicit and not a full PostgreSQL wire implementation. SQLite executes the fixed small SQL, while the facility models PostgreSQL's error-until-Sync and failed-transaction behavior. Error skips all later P/B/E before the next S. Sync ends that skip phase but does not automatically roll back failed transactions. There is no request id in backend messages. Application may alter decoder/pipeline, not backend outcomes or wire contract. Input id is an application identity; it is never echoed by backend packets.
