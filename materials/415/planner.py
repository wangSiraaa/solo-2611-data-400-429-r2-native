def optimize(plan,catalog):
    if plan['op']=='join' and catalog.declared_fk(plan):
        return dict(op='scan',table=plan['left']['table'],projection=plan['projection'],
                    reason='declared foreign key')
    return plan.copy()
