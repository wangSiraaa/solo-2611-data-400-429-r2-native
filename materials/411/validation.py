def conflict(store, tx):
    return [key for key in tx.writes
            if store.last_write.get(key, -1) > tx.start]
