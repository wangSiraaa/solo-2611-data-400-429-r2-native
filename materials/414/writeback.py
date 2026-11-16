def put_view(db,desired):
    with db:
        db.execute('DELETE FROM orders')
        db.execute('DELETE FROM customers')
        customers={}
        for row in desired:
            customers[row['customer_id']]=row['customer_name']
        db.executemany('INSERT INTO customers(customer_id,name) VALUES(?,?)',customers.items())
        db.executemany("INSERT INTO orders(order_id,customer_id,amount,status) VALUES(?,?,?,'open')",
                       [(r['order_id'],r['customer_id'],r['amount']) for r in desired])
