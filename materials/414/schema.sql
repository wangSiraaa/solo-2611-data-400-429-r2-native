PRAGMA foreign_keys=ON;
CREATE TABLE customers(customer_id TEXT PRIMARY KEY,name TEXT NOT NULL,private_note TEXT NOT NULL DEFAULT '');
CREATE TABLE orders(order_id TEXT PRIMARY KEY,customer_id TEXT NOT NULL REFERENCES customers(customer_id),
                    amount INTEGER NOT NULL,status TEXT NOT NULL CHECK(status IN ('open','closed')),
                    internal_note TEXT NOT NULL DEFAULT '');
