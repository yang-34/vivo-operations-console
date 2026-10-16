def item(key,name,cost,currency,balance,payload,**kwargs):return dict(key=key,name=name,cost=cost,currency=currency,balance=balance,payload=payload,**kwargs)
def collect_catalog(provider,families):
    result=[]
    for name,read in families:result.extend(read())
    return result
