def read_view(db):
    return [dict(order_id=r[0],customer_id=r[1],customer_name=r[2],amount=r[3])
            for r in db.execute("""SELECT o.order_id,o.customer_id,c.name,o.amount
                                   FROM orders o JOIN customers c USING(customer_id)
                                   WHERE o.status='open' ORDER BY o.order_id""")]
