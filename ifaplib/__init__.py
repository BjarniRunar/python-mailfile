__author__ = 'Bjarni R. Einarsson <bre@mailpile.is>'
__version__ = '0.0.1'
__doc__ = """IMAP File Access Protocol

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
"""

import base64
import email.parser
import hashlib
import json
import os
import re
import stat
import threading
import time
import zlib

from StringIO import StringIO
from base64 import urlsafe_b64encode
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.backends import default_backend


def _clean_path(path):
    while path[:1] == '/':
        path = path[1:]
    while path[-1:] == '/':
        path = path[:-1]
    return path.replace('//', '/')


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
            self.metadata['ts'] = int(time.time())
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
        self._seen = set([])

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

    def synchronize(self, cleanup=False):
        """
        This method implements the IFAP synchronization protocol, bringing
        our in-memory metadata index up to date with what is on the server.

        If cleanup is requested, delete from IMAP any IFAP data that is no
        longer needed.

        The synchronization protocol is as follows; it depends on messages
        in an IMAP folder receiving ascending, never-repeated integer IDs.

        1. Messages in IFAP are read and parsed in reverse order:
           1. If we cannot parse or decrypt the message, ignore it.
           2. If we have seen and processed this message before, stop.
           3. File objects: If a message represents a new file or a NEWER
              version of one we've already seen, update our file index.
              If a file object is a snapshot, load and parse it.
           4. All other messages are ignored.
        """
        with self._lock:
            self.flush()
            if 'OK' != self.imap.select(self._base_folder)[0]:
                if ('OK' != self.imap.create(self._base_folder)[0] or
                        'OK' != self.imap.select(self._base_folder)[0]):
                    raise IOError('Could not select: %s' % self._base_folder)

            (rv, (seqs,)) = self.imap.uid('SEARCH', 'ALL')
            if rv != 'OK':
                raise IOError(
                    'Could not search: %s (%s, [%s])'
                    % (self._base_folder, rv, seqs))

            distance = 0
            seqs = sorted([int(i) for i in seqs.split(' ') if i])
            broken = set([])
            to_delete = set([])
            for seq in reversed(seqs):
                if seq in self._seen:
                    break
                try:
                    (rv, data) = self.imap.uid(
                        'FETCH', str(seq), '(BODY.PEEK[]<0.1024>)')
                    if rv != 'OK':
                        broken.add(seq)
                        continue
                    metadata = self._parse_message(
                        None, data[0][1], headersonly=True, clean=False)
                    file_path = metadata['fn']
                    self._seen.add(seq)
                    distance += 1
                except (ValueError, NameError, AttributeError, KeyError,
                        IndexError, InvalidToken):
                    broken.add(seq)
                    continue

                if self._tree.get(file_path, (-1,))[0] < seq:
                    self._clean_metadata(metadata)
                    versions = set([seq])
                    if file_path in self._tree:
                        versions |= self._tree[file_path][2]
                    self._tree[file_path] = (seq, metadata, versions)
                    if file_path == self._SNAPSHOT_FILE_PATH:
                        try:
                            self._parse_snapshot(seq)
                        except ValueError:
                            print('FIXME: Corrupt snapshot, what to do?')

            if cleanup:
                # Go through the tree, decide what to keep...
                keeping = set([])
                for fp in self._tree:
                    seq, metadata, versions = self._tree[fp]
                    wanted = metadata.get('versions', 1)
                    versions.add(seq)
                    keeping_versions = set(sorted(versions)[-wanted:]) 
                    keeping |= keeping_versions
                    self._tree[fp] = (seq, metadata, keeping_versions)

                to_delete = sorted(list(self._seen - keeping))
                if to_delete:
                    (rs, data) = self.imap.uid('STORE',
                        ','.join([str(s) for s in to_delete]),
                        '+FLAGS.SILENT',
                        '(\Deleted)')
                    (re, data) = self.imap.expunge()
                    if rs == re == 'OK':
                        self._seen -= set(to_delete)

            if distance > 20:
                self.save_snapshot()

    def save_snapshot(self):
        """
        Save a snapshot of the current metadata index back to IMAP.
        """
        def _j(smv):
            seq, metadata, versions = smv
            return [seq, metadata, list(versions)]
        with self.open(self._SNAPSHOT_FILE_PATH, 'w') as fd:
            fd.write(zlib.compress(json.dumps({
                'tree': dict((fp, _j(self._tree[fp])) for fp in self._tree),
                'seen': list(self._seen)})))

    def _parse_snapshot(self, seq):
        metadata, contents = self._get_file(self._SNAPSHOT_FILE_PATH, seq) 
        snapshot = json.loads(zlib.decompress(contents))
        for file_path in snapshot['tree']:
            seq, metadata, versions = snapshot['tree'][file_path]
            versions = set(versions)
            if file_path in self._tree:
                versions |= self._tree[file_path][2]
            if file_path not in self._tree or seq > self._tree[file_path][0]:
                self._tree[file_path] = (seq, metadata, versions)
        self._seen |= set(snapshot['seen'])

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
                fobj = self._unwritten[file_path]
                eml = self.encode_object(
                    file_path, fobj.getvalue(), metadata=fobj.metadata)
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

    def _clean_metadata(self, metadata):
        for k in ('_', 'fn'):
            if k in metadata:
                del metadata[k]

    def _parse_message(self, file_path, data, headersonly=False, clean=True):
        if headersonly:
            parser = email.parser.HeaderParser()
        else:
            parser = email.parser.Parser()
        message = parser.parsestr(data, headersonly=headersonly)

        xifap = message['X-IFAP'].strip()
        if xifap[:1] == '!':
            xifap = self.config.fernet.decrypt(xifap[1:])
        else:
            xifap = base64.b64decode(xifap)
        metadata = json.loads(xifap)

        if file_path and metadata['fn'] != file_path:
            raise IOError('File path mismatch: %s' % metadata['fn'])

        if clean:
            self._clean_metadata(metadata)

        if headersonly:
            return metadata

        for part in message.walk():
            if part.get_content_type() == 'application/x-ifap':
                contents = part.get_payload()
                if contents[:1] == '!':
                    contents = self.config.fernet.decrypt(contents[1:])
                else:
                    contents = base64.b64decode(contents)
                return metadata, contents[:metadata['bytes']]

        raise OSError(
            'No data in message, %s is corrupt?' % (file_path or 'file'))

    def _get_file(self, file_path, version):
        with self._lock:
            seq, metadata, versions = self._tree[file_path]
            if version is not None:
                if version not in versions:
                    raise KeyError('Unknown version: %s' % version)
                    seq = version

            (rv, data) = self.imap.uid('FETCH', str(seq), '(BODY[])')
            if rv != 'OK':
                raise OSError(
                    'Could not fetch: %s=%s (%s)' % (file_path, seq, data[0]))

            return self._parse_message(file_path, data[0][1])

    def listdir(self, file_path):
        """Emulate os.listdir() for a given path."""
        dirents = []
        with self._lock:
            clean_path = _clean_path(file_path)
            if clean_path:
                clean_path += '/'
            potentials = [k for k in self._tree if k.startswith(clean_path)]
            if potentials:
                dirents = ['.', '..']
            for p in potentials:
                dirents.append(p[len(clean_path):].split('/')[0])
        if not dirents:
            raise OSError('No such file or directory: `%s`' % file_path) 
        return sorted(list(set(dirents)))

    def lstat(self, file_path, fh=None):
        """Emulate os.lstat() for a given path."""
        try:
            info = self._tree[_clean_path(file_path)]
            mode = stat.S_IFREG | 0o600
            size = info[1].get('bytes', 0)
            ts = info[1].get('t', 0)
        except KeyError:
            subdirs = self.listdir(file_path)
            size = len(subdirs)
            mode = stat.S_IFDIR | 0o700
            ts = 0
        return {
            'st_atime': ts,
            'st_ctime': ts,
            'st_nlink': 1,
            'st_mode': mode,
            'st_size': size,
            'st_gid': os.getgid(),
            'st_uid': os.getuid()}

    def open(self, file_path, mode='r', version=None):
        """Open an IFAP file for reading, writing or appending."""
        with self._lock:
            file_path = _clean_path(file_path)
            contents = ''
            metadata = {}
            if 'r' in mode or 'a' in mode:
                mode = mode.replace('+', 'w')
                if file_path in self._unwritten:
                    file_obj = self._unwritten[file_path]
                    contents = file_obj.getvalue()
                    metadata = file_obj.metadata
                else:
                    try:
                        metadata, contents = self._get_file(file_path, version)
                    except (OSError, IOError, KeyError, ValueError) as e:
                        if 'w' not in mode and 'a' not in mode:
                            raise OSError('Error open(%s): %s' % (file_path, e))
                        metadata = {}
                        contents = ''
            return IFAP_File(self, file_path, mode, metadata, contents)


if __name__ == "__main__":
    import sys, doctest
    results = doctest.testmod(optionflags=doctest.ELLIPSIS)
    print('%s' % (results, ))
    if results.failed:
        sys.exit(1)
