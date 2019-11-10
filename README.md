# python-ifaplib: IMAP File Access Protocol Library

The IMAP File Access Protocol defines a way to maintain a "filesystem" inside
an IMAP folder. The filesystem can be symmetrically encrypted (using the
cryptography library's AES-128 Fernet construct), it supports concurrent
readers/writers and file versioning.

Due to the fact that file data must live entirely in RAM and be transmitted in
its entirety over the network after every change, IFAP is not well suited for
very large files. Please also be considerate towards the IMAP server admin!

The motivation for this tool is that an IMAP account is the most commonly
available form of standards compliant "cloud storage" available to the general
public. This makes an IMAP account a compelling location for app backups or
basic synchronization.

Other storage solutions that present the same API as Python's imaplib should
work as well. Included is one such solution, `backends.FilesystemIMAP`, which
reads/writes from files on disk using a variant of the Maildir format.

See the doc-strings for `IFAP.synchronize` for a description of the protocol
itself and `IFAP.encode_object` to read about the message format in IMAP.


## Dependencies

You will need:

   * Python 2.7
   * cryptography

If you want to mount your IFAP using FUSE, you also need:

   * fusepy


## Shell examples

These all do roughly what you would expect:

    python -m ifaplib help

    python -m ifaplib login

    python -m ifaplib put README.md /project/

    python -m ifaplib ls /

    python -m ifaplib mount /path/to/mountpoint

    python -m ifaplib logout


## Code example

    from imaplib import IMAP4
    from ifaplib import IFAP

    ifap = IFAP(IMAP4('imap.domain.com', 143), 'IFAP_DATA')

    # This is the key used to encrypt/decrypt our data
    ifap.set_encryption_key(your_secret_here)

    # Do whatever IMAP authentication dance you need, see imaplib.
    ifap.imap.login(...)

    with ifap:
        with ifap.open('/magic/path/name.txt', 'w') as fd:
            fd.write('hello world')

        # Trying to read the file here would fail, unless we sync state:
        ifap.synchronize()

    with ifap.open('/magic/path/name.txt', 'r') as fd:
        print(fd.read())


## TODO

   * Python 3
   * Add an info command to the CLI, to get your encryption key etc.
   * Should we hard-code in a max file size and error out?
   * Recovery: synchronize mode that ignores snapshots
   * Does append mode work or make sense?
   * Data injection attack: prefer mode that requires encryption?
   * More testing: does it work with the popular IMAP servers?
   * File locking would be nice


## Copyright, license, credits

This code is: (C) Copyright 2019, Bjarni R. Einarsson <bre@mailpile.is>

Ifaplib is free software: you can redistribute it and/or modify
it under the terms of the GNU Lesser General Public License as
published by the Free Software Foundation, either version 3 of
the License, or (at your option) any later version.

Ifaplib is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU Lesser General Public
License along with ifaplib. If not, see <https://www.gnu.org/licenses/>.
