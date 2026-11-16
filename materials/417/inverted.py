from bounds import build_bound
from scorer import statistics


class Index:
    def __init__(self,docs,block_size=1):
        self.docs={k:list(v) for k,v in docs.items()}
        ids=sorted(self.docs)
        self.blocks=[ids[i:i+block_size] for i in range(0,len(ids),block_size)]
        stats=statistics(self.docs)
        self.bounds=[build_bound(block,self.docs,stats) for block in self.blocks]

    def remove(self,rid):
        del self.docs[rid]

    def put(self,rid,tokens):
        fresh=rid not in self.docs
        self.docs[rid]=list(tokens)
        if fresh:
            self.blocks.append([rid])
            self.bounds.append(build_bound([rid],self.docs,statistics(self.docs)))
