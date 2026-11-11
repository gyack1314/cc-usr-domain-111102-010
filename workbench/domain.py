import zlib

def uvarint(value):
    if type(value) is not int or not 0<=value<(1<<64):raise ValueError('无符号整数越界')
    out=bytearray()
    while value>=128:out.append((value&127)|128);value>>=7
    out.append(value);return bytes(out)
def read_uvarint(data,offset=0):
    start=offset;value=0
    for shift in range(0,70,7):
        if offset>=len(data):raise ValueError('整数编码截断')
        byte=data[offset];offset+=1;value|=(byte&127)<<shift
        if not byte&128:
            if value>=(1<<64) or bytes(data[start:offset])!=uvarint(value):raise ValueError('整数编码非规范或越界')
            return value,offset
    raise ValueError('整数编码过长')
def zigzag(value):
    if type(value) is not int or not -(1<<63)<=value<(1<<63):raise ValueError('有符号整数越界')
    return (value<<1)^(value>>63)
def unzigzag(value):
    if type(value) is not int or not 0<=value<(1<<64):raise ValueError('整数越界')
    return (value>>1)^-(value&1)
def crc(data):return zlib.crc32(data)&0xffffffff

class BytesSource:
    def __init__(self,data):self.data=data;self.reads=[]
    def read_at(self,offset,length):
        if type(offset) is not int or type(length) is not int or offset<0 or length<0 or offset+length>len(self.data):raise ValueError('读取越界')
        self.reads.append((offset,length));return self.data[offset:offset+length]
