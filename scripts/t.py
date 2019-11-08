#!/usr/bin/python
import random

from ifaplib import IFAP
from ifaplib.backends import FilesystemIMAP

ifap = IFAP(FilesystemIMAP('/tmp/foo', create=0o775), 'IFAP')

if random.randint(0, 2) == 0:
    ifap.set_encryption_key('hello world')

with ifap:
    ifap.config.subject = 'IFAP Test Script'

    print('This is awesome: %s' % (ifap.config.key or '(no crypto)'))
    with ifap.open('/hello/world', 'rw') as fd:
        fd.write('one\n') 
        fd.write('two\n') 
        fd.write('three\n') 
        print(fd.getvalue())

    with ifap.open('/hello/world', 'r') as fd:
        print(fd.read())

    with ifap.open('/hello/world', 'r') as fd:
        print(fd.read())

print('%s' % (ifap.imap.select('IFAP'),))
print('%s' % (ifap.imap.search(None, 'ALL'),))
print('%s' % (ifap.imap.fetch('1', '(BODY.TEXT)'),))
