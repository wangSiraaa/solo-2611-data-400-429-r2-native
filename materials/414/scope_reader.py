from view import read_view


def read_scope(db, scope):
    return [row for row in read_view(db)
            if scope['amount_lo'] <= row['amount'] < scope['amount_hi']]
