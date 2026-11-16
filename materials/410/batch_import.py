"""Existing loader publishes operations separately; later errors leave a partial batch."""


def apply(engine, operations):
    for item in operations:
        if item['op'] == 'put':
            engine.put(item['row_id'], item['category'], item['amount'])
        elif item['op'] == 'delete':
            engine.delete(item['row_id'])
        else:
            raise ValueError('UNKNOWN_OPERATION')
    return engine.snapshot()
