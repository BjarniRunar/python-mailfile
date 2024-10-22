# This file is part of Mailfile
#
# Mailfile is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version.
#
# Mailfile is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with Mailfile. If not, see <https://www.gnu.org/licenses/>.
#
__author__ = 'Bjarni R. Einarsson <bre@mailpile.is>'
__version__ = '0.0.1'
__doc__ = """Encrypted IMAP File Storage

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
basic synchronization, and Mailfile's sister project, Mailpile
<https://www.mailpile.is/>, needs exactly such features...

Other storage solutions that present the same API as Python's imaplib should
work as well. Included is one such solution, `backends.FilesystemIMAP`, which
reads/writes from files on disk using a variant of the Maildir format.

See the doc-strings for `Mailfile.synchronize` for a description of the
protocol itself and `Mailfile.encode_object` to read about the message format
in IMAP.
"""

import base64
import copy
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


def _clean_metadata(metadata):
    for k in ('_', 'fn'):
        if k in metadata:
            del metadata[k]
    return metadata


class Mailfile_File(StringIO):
    """
    This class presents a file-like interface (based on StringIO) to a file
    stored in Mailfile.

    All operations are in RAM until the file is closed, at which point (if
    the file was opened in a writable mode), the contents will be written
    back to Mailfile. Note that whether that triggers a write to the IMAP server
    or just a write to cache, depends on the Mailfile configuration. Please use
    `Mailfile.flush` if you need guarantees.

    Each Mailfile_File object has two extra attributes, file_path and metadata:
    the file_path is read-only, but the metadata object is a free-form dict
    of JSON-serializable data that gets stored along with the file. For
    performance reasons, only small amounts of information should be stored
    in metadata.

    In particular, the `versions` metadata attribute, if set, should be an
    integer informing Mailfile how many backups to keep of this file before
    garbage collection.
    """
    def __init__(self, mailfile, file_path, mode, metadata, *args, **kwargs):
        StringIO.__init__(self, *args, **kwargs)
        self._file_path = file_path
        self._open_mode = mode
        self._mailfile = mailfile
        self._lock = mailfile._lock
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
            self._mailfile._set_file(self)
            self._mailfile = None  # Break reference cycle
        else:
            StringIO.close(self, *args, **kwargs)


class Mailfile_Config(object):
    """
    This class represents the current configuration of your Mailfile storage.
    Access and manipulate it using the `Mailfile.config` property. Take special
    note of the fact that settings get reverted when exiting a `with Mailfile`.

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
    please use `Mailfile.set_encryption_key()` instead.
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
            subject='[Mailfile] File Storage',
            email_to='.. <to@mailfile.example>',
            email_from='.. <from@mailfile.example>',
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


class Mailfile(object):
    _SNAPSHOT_FILE_PATH = 'Mailfile/metadata'

    def __init__(self, imap_obj, base_folder='FILE_STORAGE', **kwargs):
        self.config = Mailfile_Config(**kwargs)
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
        When used in a `with mailfile ...` statement, the Mailfile object is locked
        and changes are buffered in RAM until a threshold is reached, the
        user calls `<instance>.flush()` or the block is exited.

        Note that changes to `<instance>.config` made within a `with` block
        are reverted when the block is exited; this allows an application to
        turn encryption on or off temporarily.
        """
        self._lock.acquire()
        self._sstack.append(Mailfile_Config._Copy(self.config))
        self.config.buffering = True
        self.synchronize()
        return self

    def __exit__(self, *args, **kwargs):
        self.synchronize()
        self.config = self._sstack.pop(-1)
        self._lock.release()

    def synchronize(self, cleanup=False, snapshot=None, ignore_snapshot=False):
        """
        This method implements the Mailfile synchronization protocol, bringing
        our in-memory metadata index up to date with what is on the server.

        If cleanup is requested, delete from IMAP any Mailfile data that is no
        longer needed.

        The synchronization protocol is as follows; it depends on messages
        in an IMAP folder receiving ascending, never-repeated integer IDs.

        1. Messages in Mailfile are read and parsed in reverse order:
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
            existing = set(seqs)
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
                    _clean_metadata(metadata)
                    versions = set([seq])
                    if file_path in self._tree:
                        versions |= self._tree[file_path][2]
                    self._tree[file_path] = (seq, metadata, versions)
                    if file_path == self._SNAPSHOT_FILE_PATH and not ignore_snapshot:
                        try:
                            self._parse_snapshot(seq, existing)
                        except ValueError:
                            print('FIXME: Corrupt snapshot, what to do?')

            if cleanup:
                # Go through the tree, decide what to keep...
                keeping = set([])
                for fp in self._tree.keys():
                    seq, metadata, versions = self._tree[fp]
                    wanted = metadata.get('versions', 1)
                    versions.add(seq)
                    keeping_versions = existing & set(sorted(versions)[-wanted:])
                    keeping |= keeping_versions
                    if keeping_versions:
                        self._tree[fp] = (
                            max(keeping_versions), metadata, keeping_versions)
                    else:
                        del self._tree[fp]

                to_delete = sorted(list(self._seen - keeping))
                if to_delete:
                    (rs, data) = self.imap.uid('STORE',
                        ','.join([str(s) for s in to_delete]),
                        '+FLAGS.SILENT',
                        '(\Deleted)')
                    (re, data) = self.imap.expunge()
                    if rs == re == 'OK':
                        self._seen -= set(to_delete)

            self._seen &= existing
            if (snapshot is not False) and (distance > 20 or snapshot is True):
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

    def _parse_snapshot(self, seq, existing):
        metadata, contents = self._get_file(self._SNAPSHOT_FILE_PATH, seq)
        snapshot = json.loads(zlib.decompress(contents))
        for file_path in snapshot['tree']:
            seq, metadata, versions = snapshot['tree'][file_path]
            if seq not in existing:
                continue
            versions = set(versions) & existing
            if file_path in self._tree:
                versions.update(self._tree[file_path][2] & existing)
                self._tree[file_path][2].update(versions)
            if file_path not in self._tree or seq > self._tree[file_path][0]:
                self._tree[file_path] = (seq, metadata, versions)
        self._seen |= (set(snapshot['seen']) & existing)

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
        Encode (and optionally encrypt) an Mailfile object for storage in IMAP.
        Returns a RFC2822 formatted string suitable for storage in IMAP.

        An Mailfile encoded object is an RFC2822 message, with an X-Mailfile header
        that contains the message metadata, and exactly one MIME part of type
        `application/x-mailfile` containing any file data. Other (ornamental)
        headers or MIME parts may be present for compability and usability.

        Both the metadata and the file data may be encrypted using `Fernet`
        from the `cryptography` library (AES-128, etc). Before encrypting,
        the file data is padded by adding garbage to the end and the
        metadata may contain a `_` attribute with padding as well.

        When not encrypting, both are base64 encoded. Encrypted data is
        prefixed with a '!' character to differentiate it from clear-text.

        The metadata is a JSON-encoded dictionary, which always contains at
        least `fn` and `bytes` key/value pairs, the previous of which is the
        file's full path and name (within the Mailfile filesystem) and the latter
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
        xmailfile = json.dumps(mdata, indent=1).strip()

        if self.config.encrypt:
            # Note: The padding numbers, 148 and 2048, are chosen in part to
            #       keep small files below 3*1500 bytes: three network packets
            #       assuming a common network MTU, and <one 4KB block on disk.
            encoding = '7bit'
            subject = self.config.subject
            filename = 'mailfile.enc'
            padding = ('_' * 200)
            mdata['_'] = padding[:148 - (len(xmailfile) % 148)]
            xmailfile = json.dumps(mdata, indent=1)
            file_data += (' ' * (2048 - (len(file_data) % 2048)))
        else:
            encoding = 'base64'
            subject = '%s: %s' % (self.config.subject, file_path)
            filename = os.path.basename(file_path)

        return '\r\n'.join([
            'To: %s' % self.config.email_to,
            'From: %s' % self.config.email_from,
            'Subject: %s' % subject,
            'X-Keep-On-Server: manual-delete, not-email',
            'X-Mailfile:',
            self._reflow(
                self._maybe_encrypt(xmailfile, b64encode=True),
                indent=' ', preserve=(not self.config.encrypt)),
            'Content-Type: application/x-mailfile',
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
        automatically when exiting a `with mailfile ...` block. Returns True
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

    def _parse_message(self, file_path, data, headersonly=False, clean=True):
        if headersonly:
            parser = email.parser.HeaderParser()
        else:
            parser = email.parser.Parser()
        message = parser.parsestr(data, headersonly=headersonly)

        xmailfile = message['X-Mailfile'].strip()
        if xmailfile[:1] == '!':
            xmailfile = self.config.fernet.decrypt(xmailfile[1:])
        else:
            xmailfile = base64.b64decode(xmailfile)
        metadata = json.loads(xmailfile)

        if file_path and metadata['fn'] != file_path:
            raise IOError('File path mismatch: %s' % metadata['fn'])

        if clean:
            _clean_metadata(metadata)

        if headersonly:
            return metadata

        for part in message.walk():
            if part.get_content_type() == 'application/x-mailfile':
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
            potentials = [k for k in self._tree if k.startswith(clean_path)
                          and not self._tree[k][1].get('deleted')
                          and self._tree[k][0] in self._seen]
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

    def remove(self, file_path, versions=None):
        file_path = _clean_path(file_path)
        with self._lock:
            if file_path in self._unwritten:
                del self._unwritten[file_path]

            finfo = self._tree.get(file_path)
            if finfo is None:
                raise OSError('No such file: %s' % (file_path,))

            if finfo[1].get('versions', 1) > 1 and not versions:
                with self.open(file_path, 'w') as fd:
                    fd.metadata['deleted'] = True
                return self.synchronize(snapshot=True)

            if not versions:
                versions = copy.copy(finfo[2])
            for version in versions:
                if version not in finfo[2]:
                    raise OSError('No such version: %s[%s]' % (file_path, version))

            (rs, data) = self.imap.uid('STORE',
                ','.join([str(s) for s in versions]),
                '+FLAGS.SILENT',
                '(\Deleted)')
            if rs != 'OK':
                raise OSError('Delete failed: %s' % data[0])
            (re, data) = self.imap.expunge()

            for v in versions:
                finfo[2].remove(v)
            if len(finfo[2]):
                seq = max(finfo[2])
                with self.open(file_path, 'r', version=seq) as fd:
                    self._tree[file_path] = (
                        seq, _clean_metadata(fd.metadata), finfo[2])
            else:
                del self._tree[file_path]

            self.synchronize(cleanup=True, snapshot=True)

    def open(self, file_path, mode='r', version=None):
        """Open an Mailfile file for reading, writing or appending."""
        with self._lock:
            file_path = _clean_path(file_path)
            contents = ''
            metadata = {}
            mode = mode.replace('+', 'w')
            if file_path in self._unwritten:
                file_obj = self._unwritten[file_path]
                contents = file_obj.getvalue()
                metadata = file_obj.metadata
            else:
                try:
                    metadata, contents = self._get_file(file_path, version)
                    if metadata.get('deleted'):
                        if 'w' in mode or 'a' in mode:
                            del metadata['deleted']
                        raise OSError('File is deleted')
                except (OSError, IOError, KeyError, ValueError) as e:
                    if 'w' not in mode and 'a' not in mode:
                        raise OSError('Error open(%s): %s' % (file_path, e))
                    contents = ''
            if 'r' not in mode and 'a' not in mode:
                contents = ''
            return Mailfile_File(self, file_path, mode, metadata, contents)


if __name__ == "__main__":
    import sys, doctest
    results = doctest.testmod(optionflags=doctest.ELLIPSIS)
    print('%s' % (results, ))
    if results.failed:
        sys.exit(1)
