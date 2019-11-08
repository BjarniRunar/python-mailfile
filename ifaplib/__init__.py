__author__ = 'Bjarni R. Einarsson <bre@mailpile.is>'
__version__ = '0.0.1'
__doc__ = """IMAP File Access Protocol

The IMAP File Access Protocol defines a way to maintain a "filesystem" inside
an IMAP folder. The filesystem can be symmetrically encrypted (using the
cryptography library's AES-128 Fernet construct), it supports concurrent
readers/writers, file versioning and basic file locking for synchronization.

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
"""

import base64
import email.parser
import hashlib
import json
import os
import re
import threading

from StringIO import StringIO
from base64 import urlsafe_b64encode
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.backends import default_backend


class IFAP_File(StringIO):
    """
    This class presents a file-like interface (based on StringIO) to a file
    stored in IFAP.

    All operations are in RAM until the file is closed, at which point (if
    the file was opened in a writable mode), the contents will be written
    back to IFAP. Note that whether that triggers a write to the IMAP server
    or just a write to cache, depends on the IFAP configuration. Please use
    `IFAP.flush` if you need guarantees.

    Each IFAP_File object has two extra attributes, file_path and metadata:
    the file_path is read-only, but the metadata object is a free-form dict
    of JSON-serializable data that gets stored along with the file. For
    performance reasons, only small amounts of information should be stored
    in metadata.

    In particular, the `versions` metadata attribute, if set, should be an
    integer informing IFAP how many backups to keep of this file before
    garbage collection.
    """
    def __init__(self, ifap, file_path, mode, metadata, *args, **kwargs):
        StringIO.__init__(self, *args, **kwargs)
        self._file_path = file_path
        self._open_mode = mode
        self._ifap = ifap
        self._lock = ifap._lock
        self._metadata = metadata

    file_path = property(lambda self: self._file_path)
    metadata = property(lambda self: self._metadata)

    def __enter__(self, *args, **kwargs):
        self._lock.acquire()
        return self

    def __exit__(self, *args, **kwargs):
        self.close()
        self._lock.release()

    def __len__(self):
        p0 = self.tell()
        self.seek(0, mode=2)
        p2 = self.tell()
        self.seek(p0)
        return p2 

    def close(self, *args, **kwargs):
        if 'w' in self._open_mode or 'a' in self._open_mode:
            self._ifap._set_file(self)
            self._ifap = None  # Break reference cycle
        else:
            StringIO.close(self, *args, **kwargs)


class IFAP_Config(object):
    """
    This class represents the current configuration of your IFAP storage.
    Access and manipulate it using the `IFAP.config` property. Take special
    note of the fact that settings get reverted when exiting a `with IFAP`.

    Available settings:

       <config>.subject           Subject used in IMAP messages
       <config>.email_to          Address for To-header in IMAP messages
       <config>.email_from        Address for From-header in IMAP messages

       <config>.buffering         Boolean: whether or not to buffer changes
       <config>.buffer_max_bytes  Force a flush if we buffer more than this

       <config>.key               Current encryption key
       <config>.fernet            Current encryption engine
       <config>.encrypt           Boolean: whether to encrypt or not

    Important: The key and fernet settings should not be modified directly,
    please use `IFAP.set_encryption_key()` instead.
    """
    @classmethod
    def _Copy(cls, obj):
        return cls(
            obj.buffering_max_bytes, obj.buffering,
            obj.subject, obj.email_to, obj.email_from,
            obj.encrypt, obj.fernet, obj.key)

    def __init__(self,
            buffering_max_bytes=102400,
            buffering=False,
            subject='[IFAP] File Storage',
            email_to='.. <to@ifap.example>',
            email_from='.. <from@ifap.example>',
            encrypt=False,
            fernet=None,
            key=None):
        self.buffering_max_bytes = buffering_max_bytes
        self.buffering = buffering
        self.subject = subject
        self.email_to = email_to
        self.email_from = email_from
        self.encrypt = encrypt
        self.fernet = fernet
        self.key = key


class IFAP(object):
    _SNAPSHOT_FILE_PATH = 'IFAP/metadata.json'

    def __init__(self, imap_obj, base_folder='FILE_STORAGE', **kwargs):
        self.config = IFAP_Config(**kwargs)
        self.imap = imap_obj
        self._base_folder = base_folder
        self._lock = threading.RLock()
        self._sstack = []
        self._unwritten = {}
        self._unwritten_bytes = 0
        self._tree = {}

    def __enter__(self, *args, **kwargs):
        """
        When used in a `with ifap ...` statement, the IFAP object is locked
        and changes are buffered in RAM until a threshold is reached, the
        user calls `<instance>.flush()` or the block is exited.

        Note that changes to `<instance>.config` made within a `with` block
        are reverted when the block is exited; this allows an application to
        turn encryption on or off temporarily.
        """
        self._lock.acquire()
        self._sstack.append(IFAP_Config._Copy(self.config))
        self.config.buffering = True
        self.synchronize()
        return self

    def __exit__(self, *args, **kwargs):
        self.synchronize()
        self.config = self._sstack.pop(-1)
        self._lock.release()

    def synchronize(self):
        """
        This method implements the IFAP synchronization protocol, bringing
        our in-memory metadata index up to date with what is on the server.

        The synchronization protocol is as follows; it depends on messages
        in an IMAP folder receiving ascending, never-repeated integer IDs.

        1. Messages in IFAP are read and parsed in reverse order:
           1. If we cannot parse or decrypt the message, ignore it.
           2. If we have seen and processed this message before, stop.
           3. File objects: If a message represents a new file or a NEWER
              version of one we've already seen, update our file index.
              Otherwise, queue for deletion.
           4. Lock objects: If a message represents a lock deletion, or an
              OLDER but unexpired version of a lock we've already seen,
              update our lock index. Otherwise, queue for deletion.
           5. Snapshot objects: ... FIXME ...
           6. All other messages are ignored.

        ...FIXME FIXME FIXME...
        """
        with self._lock:
            self.flush()

            if 'OK' != self.imap.select(self._base_folder)[0]:
                raise IOError('Could not select: %s' % self._base_folder)
            (rv, (seqs,)) = self.imap.search(None, 'ALL')
            if rv != 'OK':
                raise IOError(
                    'Could not search: %s (%s, [%s])'
                    % (self._base_folder, rv, seqs))
            seqs = sorted([int(i) for i in seqs.split(' ') if i])
            broken = set([])
            to_delete = set([])
            for seq in reversed(seqs):
                if seq in to_delete:
                    continue
                (rv, data) = self.imap.fetch(str(seq), '(BODY.PEEK[]<0.1024>)')
                if rv != 'OK':
                    broken.add(seq)
                    continue
                try:
                    parser = email.parser.Parser()
                    message = parser.parsestr(data[0][1], headersonly=True)
                    xifap = message['X-IFAP'].strip()
                    if xifap[:1] == '!':
                        xifap = self.config.fernet.decrypt(xifap[1:])
                    else:
                        xifap = base64.b64decode(xifap)
                    metadata = json.loads(xifap)
                    file_path = metadata['fn']
                except (ValueError, NameError, AttributeError, KeyError,
                        IndexError, InvalidToken):
                    broken.add(seq)
                    continue

                if file_path == self._SNAPSHOT_FILE_PATH:
                    print('FIXME: SNAPSHOT AT %s' % seq)
                elif self._tree.get(file_path, (-1,))[0] >= seq:
                    continue
                else:
                    for k in ('_', 'fn'):
                        if k in metadata:
                            del metadata[k]
                    if file_path in self._tree:
                        to_delete.add(self._tree[file_path][0])
                    self._tree[file_path] = (seq, metadata)

        # These are the messages we consider obsolete
        to_delete |= (
            set(seqs) - broken - set(k[0] for k in self._tree.values()))
        print('FIXME: Delete these: %s' % to_delete)

    def _maybe_encrypt(self, data, b64encode=False):
        if self.config.encrypt:
            return '!' + self.config.fernet.encrypt(data)
        if b64encode:
            return base64.b64encode(data)
        return data

    def _reflow(self, data, indent='', linelen=78, preserve=False):
        if preserve:
            return indent + data.replace('\n', '\r\n' + indent).strip()
        else:
            linelen -= len(indent)
            return indent + re.sub(
                '(\S{%d,%d})' % (linelen, linelen),
                lambda m: m.group(0)+'\r\n'+indent,
                ''.join(data.split())).strip()

    def encode_object(self, file_path, file_data, metadata=None):
        """
        Encode (and optionally encrypt) an IFAP object for storage in IMAP.
        Returns a RFC2822 formatted string suitable for storage in IMAP.

        An IFAP encoded object is an RFC2822 message, with an X-IFAP header
        that contains the message metadata, and exactly one MIME part of type
        `application/x-ifap` containing any file data. Other (ornamental)
        headers or MIME parts may be present for compability and usability.

        Both the metadata and the file data may be encrypted using `Fernet`
        from the `cryptography` library (AES-128, etc). Before encrypting,
        the file data is padded by adding garbage to the end and the
        metadata may contain a `_` attribute with padding as well.

        When not encrypting, both are base64 encoded. Encrypted data is
        prefixed with a '!' character to differentiate it from clear-text.

        The metadata is a JSON-encoded dictionary, which always contains at
        least `fn` and `bytes` key/value pairs, the previous of which is the
        file's full path and name (within the IFAP filesystem) and the latter
        is the size in bytes of the data. The value of the `bytes` attribute
        is used to remove padding when decoding/decrypting.

        Any other metadata (object type, descriptions, deletion tombstones)
        is preserved, but it is up to the application to ensure that it all
        serializes safely to/from JSON and is not too large.
        """
        mdata = {}
        if metadata:
            mdata.update(metadata)
        mdata.update({'fn': file_path, 'bytes': len(file_data)})
        xifap = json.dumps(mdata, indent=1).strip()

        if self.config.encrypt:
            # Note: The padding numbers, 148 and 2048, are chosen in part to
            #       keep small files below 3*1500 bytes: three network packets
            #       assuming a common network MTU, and <one 4KB block on disk.
            encoding = '7bit'
            subject = self.config.subject
            filename = 'ifap.enc'
            padding = ('_' * 200)
            mdata['_'] = padding[:148 - (len(xifap) % 148)]
            xifap = json.dumps(mdata, indent=1)
            file_data += (' ' * (2048 - (len(file_data) % 2048)))
        else:
            encoding = 'base64'
            subject = '%s: %s' % (self.config.subject, file_path)
            filename = os.path.basename(file_path)

        return '\r\n'.join([
            'To: %s' % self.config.email_to,
            'From: %s' % self.config.email_from,
            'Subject: %s' % subject,
            'X-IFAP:',
            self._reflow(
                self._maybe_encrypt(xifap, b64encode=True),
                indent=' ', preserve=(not self.config.encrypt)),
            'Content-Type: application/x-ifap',
            'Content-Transfer-Encoding: %s' % encoding,
            'Content-Disposition: attachment; filename="%s"' % filename,
            '',
            self._reflow(self._maybe_encrypt(file_data, b64encode=True))])

    def set_encryption_key(self, key):
        """
        Set the key to use for encryption/decryption. Enables encryption (and
        decryption) of data stored in IMAP from this point onwards.

        Note: The key is NOT stretched for you, it is just hashed to a standard
        size before use. Please use `cryptography.fernet.Fernet.generate_key`
        or something of equivalent strength to generate strong keys.
        """
        self.config.key = urlsafe_b64encode(hashlib.sha256(key).digest()[:32])
        self.config.fernet = Fernet(self.config.key)
        self.config.encrypt = True

    def flush(self):
        """
        Write any buffered changes to the remote server. This gets called
        automatically when exiting a `with ifap ...` block. Returns True
        upon success, False if there was a problem writing to the server.
        """
        happy = True
        with self._lock:
            for file_path in self._unwritten.keys():
                eml = self.encode_object(
                    file_path, self._unwritten[file_path].getvalue())
                (rv, d) = self.imap.append(self._base_folder, None, None, eml)
                if rv == 'OK':
                    self._unwritten_bytes -= len(self._unwritten[file_path])
                    del self._unwritten[file_path]
                else:
                    happy = False
        return happy

    def _maybe_flush(self):
        if (not self.config.buffering
                or self.config.buffering_max_bytes < self._unwritten_bytes):
            self.flush()

    def _set_file(self, file_obj):
        with self._lock:
            self._unwritten[file_obj.file_path] = file_obj
            self._unwritten_bytes += len(file_obj)
            self._maybe_flush()

    def open(self, file_path, mode='r'):
        """
        Open an IFAP file for reading, writing or appending.
        """
        with self._lock:
            contents = ''
            metadata = {}
            if 'r' in mode or 'a' in mode:
                mode = mode.replace('+', 'w')
                if file_path in self._unwritten:
                    contents = self._unwritten[file_path].getvalue()
                else:
                    print('FIXME: Try to open the file and load the contents')
            return IFAP_File(self, file_path, mode, metadata, contents)


if __name__ == "__main__":
    import sys, doctest
    results = doctest.testmod(optionflags=doctest.ELLIPSIS)
    print('%s' % (results, ))
    if results.failed:
        sys.exit(1)
