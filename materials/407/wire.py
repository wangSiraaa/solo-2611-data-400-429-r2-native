"""Bounded PostgreSQL-style type/length framing; no user request identifiers."""
import struct

def frame(kind,payload=b''):return kind.encode('ascii')+struct.pack('!I',len(payload)+4)+payload

def exact(sock,n):
    parts=[]
    while n:
        b=sock.recv(n)
        if not b:raise EOFError('socket ended inside protocol frame')
        parts.append(b);n-=len(b)
    return b''.join(parts)

def receive(sock):
    kind=exact(sock,1).decode('ascii');length=struct.unpack('!I',exact(sock,4))[0]
    if length<4 or length>65536:raise ValueError('invalid frame length')
    return kind,exact(sock,length-4)
