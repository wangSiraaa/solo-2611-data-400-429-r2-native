"""Original append-only relation writer for one complete orders response."""
class Relations:
    def __init__(self):self.orders={};self.lines={}
    def apply(self,response):
        data=response.get('data') or {}
        for order in data.get('orders') or []:
            if order is None:continue
            self.orders[order['id']]={'id':order['id'],'tax':order.get('tax')}
            for line in order.get('lines') or []:
                if line is None:continue
                self.lines[(order['id'],line['id'])]={'order_id':order['id'],**line}
    def query(self):
        return {'orders':sorted(self.orders.values(),key=lambda r:r['id']),
                'lines':sorted(self.lines.values(),key=lambda r:(r['order_id'],r['id']))}
