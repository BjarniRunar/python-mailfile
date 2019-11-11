# python-mailfile: Encrypted IMAP File Storage

This library implements a simple "filesystem" inside an IMAP folder. The
filesystem can be symmetrically encrypted (using the cryptography library's
AES-128 Fernet construct), it supports concurrent readers/writers and file
versioning.

Due to the fact that file data must live entirely in RAM and be transmitted in
its entirety over the network after every change, Mailfile is not well suited
for very large files. Please also be considerate towards the IMAP server admin!

The motivation for this tool is that an IMAP account is the most commonly
available form of standards compliant "cloud storage" available to the general
public. This makes an IMAP account a compelling location for app backups or
basic synchronization, and Mailfile's sister project,
[Mailpile](https://www.mailpile.is/), needs exactly such features...

Other storage solutions that present the same API as Python's imaplib should
work as well. Included is one such solution, `backends.FilesystemIMAP`, which
reads/writes from files on disk using a variant of the Maildir format.

See the doc-strings for `Mailfile.synchronize` for a description of the
protocol itself and `Mailfile.encode_object` to read about the message format
in IMAP.


## Dependencies

You will need:

   * Python 2.7
   * cryptography

If you want to mount your Mailfile using FUSE, you also need:

   * fusepy


## Shell examples

These all do roughly what you would expect:

    python -m mailfile help

    python -m mailfile login

    python -m mailfile put README.md /project/

    python -m mailfile ls /

    python -m mailfile mount /path/to/mountpoint

    python -m mailfile logout


## Code example

    from imaplib import IMAP4
    from mailfile import Mailfile

    mailfile = Mailfile(IMAP4('imap.domain.com', 143), 'MAILFILE_DATA')

    # This is the key used to encrypt/decrypt our data
    mailfile.set_encryption_key(your_secret_here)

    # Do whatever IMAP authentication dance you need, see imaplib.
    mailfile.imap.login(...)

    with mailfile:
        with mailfile.open('/magic/path/name.txt', 'w') as fd:
            fd.write('hello world')

        # Trying to read the file here would fail, unless we sync state:
        mailfile.synchronize()

    with mailfile.open('/magic/path/name.txt', 'r') as fd:
        print(fd.read())


## TODO

   * Python 3
   * Add an info command to the CLI, to get your encryption key etc.
   * Should we hard-code in a max file size and error out?
   * Recovery: synchronize mode that ignores snapshots
   * Does append mode work or make sense?
   * Data injection attack: prefer mode that requires encryption?
   * More testing: does it work with the popular IMAP servers?
   * Implement CLI put --recursive command
   * File locking would be nice


## Copyright, license, credits

This code is: (C) Copyright 2019, Bjarni R. Einarsson <bre@mailpile.is>

Mailfile is free software: you can redistribute it and/or modify
it under the terms of the GNU Lesser General Public License as
published by the Free Software Foundation, either version 3 of
the License, or (at your option) any later version.

Mailfile is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU Lesser General Public
License along with mailfile. If not, see <https://www.gnu.org/licenses/>.
