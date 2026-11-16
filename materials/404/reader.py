"""Original adapter understands one complete GraphQL JSON response."""
def step(source,state):
    if state.get('read'):return None
    state['read']=True
    frame=source.read()
    if frame is None:raise ValueError('empty response')
    return {'data':frame.get('data'), 'complete':True,'errors':frame.get('errors',[])}
