class Catalog:
    def __init__(self):
        self.foreign_keys=set()
        self.unique_keys=set()

    def declare_fk(self, child, column, parent, parent_column):
        self.foreign_keys.add((child,column,parent,parent_column))

    def declare_unique(self, table, column):
        self.unique_keys.add((table,column))

    def declared_fk(self,join):
        return (join['left']['table'],join['left_key'],
                join['right']['table'],join['right_key']) in self.foreign_keys
