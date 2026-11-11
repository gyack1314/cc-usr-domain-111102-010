from workbench.domain import uvarint,read_uvarint,zigzag,unzigzag,crc,BytesSource
def check(c,root,read):
    for n in [0,1,127,128,(1<<64)-1]:c.assertEqual(read_uvarint(uvarint(n)),(n,len(uvarint(n))))
    for n in [-(1<<63),-1,0,1,(1<<63)-1]:c.assertEqual(unzigzag(zigzag(n)),n)
    for b in [b'\x80',b'\x80\x00',b'\xff'*10]:
        with c.assertRaises(ValueError):read_uvarint(b)
    s=BytesSource(b'abc');c.assertEqual(s.read_at(1,2),b'bc');c.assertEqual(s.reads,[(1,2)])
    with c.assertRaises(ValueError):s.read_at(2,2)
    c.assertNotEqual(crc(b'a'),crc(b'b'))
