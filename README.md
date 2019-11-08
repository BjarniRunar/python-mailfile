# python-ifaplib: IMAP File Access Protocol Library

The IMAP File Access Protocol defines a way to maintain a "filesystem" inside
an IMAP folder. The filesystem can be symmetrically encrypted (using the
cryptography library's AES-128 Fernet construct), it supports concurrent
readers/writers, file versioning and basic file locking for synchronization.

Due to the fact that file data must live entirely in RAM and be transmitted in
its entirety over the network after every change, IFAP is not well suited for
very large files. Please also be considerate towards the IMAP server admin!

The motivation for this tool, is that an IMAP account is the most commonly
available form of standards compliant "cloud storage", which makes it a
compelling location for app backups or basic synchronization.

See the doc-strings for `IFAP.synchronize` for a description of the protocol
itself and `IFAP.encode_object` for a description of the message format in
IMAP.


## Dependencies

You will need:

   * Python 2.7 or 3.x (not sure which 3.x)
   * cryptography


## Shell examples:

    ...
 
## Code example:

    from imaplib import IMAP4
    from ifaplib import IFAP
  
    ifap = IFAP(IMAP4('imap.domain.com', 143), 'IFAP_DATA')

    # This is the key used to encrypt/decrypt our data
    ifap.set_encryption_key(your_secret_here)

    # Do whatever IMAP authentication dance you need, see imaplib.
    ifap.imap.authenticate(...)

    with ifap:
        with ifap.open('/magic/path/name.txt', 'w') as fd:
            fd.write('hello world')

        with ifap.open('/matic/path/name.txt', 'r') as fd:
            print(fd.read())

