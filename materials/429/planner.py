def candidates(left,right,k):
    return {'left_rows':left.top(k),'right_rows':right.top(k),'reason':'Take local top K before joining.'}
