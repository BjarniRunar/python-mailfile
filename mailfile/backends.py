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
import os
import sys
import threading


DEBUGGING = True


def _l(cmd, rv):
    if DEBUGGING:
        sys.stderr.write('%-40.40s %s\n' % (cmd, ('%s' % (rv,))[:45]))
    return rv


class FilesystemIMAP(object):
    """
    This is a filesystem-backed "mock IMAP server" for use with Mailfile
    It works with a tree that looks surprisingly similar to a Maildir.
    """
    def __init__(self, base_dir, port=None, sep=':', create=False):
        self.sep = sep
        self.base_dir = base_dir
        self.selected = []
        self.response_data = {}
        self.lock = threading.RLock()
        self.create_mode = create if isinstance(create, int) else 0o700
        if create and not os.path.exists(base_dir):
            os.mkdir(base_dir, create_mode)

    def _path(self, path):
        if path == '/':
            return self.base_dir
        else:
            return os.path.join(self.base_dir, path)

    def _list(self, path):
        results = {}
        for sub in ('cur', 'new'):
            sub = os.path.join(path, sub)
            if os.path.isdir(sub):
                results.update(dict(
                    (self._fn_parse(fn)[0], fn)
                    for fn in os.listdir(sub) if fn[:4] == 'eml-'))
        return results

    def _fn_parse(self, fn):
        seq, flags = fn[4:].split(self.sep)
        return (int(seq, 16), flags[2:])

    def _fn_fmt(self, seq, flags=None):
        return 'eml-%8.8x%s2,%s' % (seq, self.sep, flags or '')

    def append(self, mailbox, flags, date_time, message):
        try:
            with self.lock:
                mpath = self._path(mailbox)
                files = self._list(mpath)
                if files:
                    seq = max(files.keys()) + 1
                else:
                    seq = 1
                newfn = self._fn_fmt(seq, flags)
                files[seq] = newfn
                with open(os.path.join(mpath, 'cur', newfn), 'w') as fd:
                    fd.write(message.replace('\r\n', '\n'))
                return _l('APPEND', ('OK', ['APPEND completed: %8.8x' % seq]))
        except (IOError, OSError, ValueError, KeyError, IndexError) as e:
            return _l('APPEND', ('NO', ['APPEND failed: %s' % e]))

    def response(self, code):
        rv = self.response_data.get(code)
        if code in self.response_data:
            del self.response_data[code]
        return rv

    def uid(self, command, *args):
        if command == 'SEARCH':
            return self.search(None, *args)
        if command == 'FETCH':
            return self.fetch(*args)
        if command == 'STORE':
            return self.store(*args)
        raise ValueError('Unknown command: %s' % command)

    def select(self, mailbox='INBOX', readonly=False):
        try:
            mpath = self._path(mailbox)
            if not os.path.exists(mpath) or not os.path.isdir(mpath):
                raise OSError('Not a directory: %s' % mpath)
            self.response_data = {}
            message_count = len(self._list(mpath))
            self.selected = mailbox
            return _l('SELECT %s' % mailbox, ('OK', [message_count]))
        except (IOError, OSError, ValueError, KeyError, IndexError) as e:
            return _l('SELECT', ('NO', ['No such mailbox: %s' % e]))

    def create(self, mailbox):
        try:
            mpath = self._path(mailbox)
            if not os.path.exists(mpath) or not os.path.isdir(mpath):
                os.mkdir(mpath)
                os.mkdir(os.path.join(mpath, 'cur'))
                os.mkdir(os.path.join(mpath, 'new'))
                os.mkdir(os.path.join(mpath, 'tmp'))
            return _l('CREATE', ('OK', ['Created %s' % mailbox]))
        except (IOError, OSError, ValueError, KeyError, IndexError) as e:
            return _l('CREATE', ('NO', ['Failed: %s' % e]))

    def search(self, charset, *criteria):
        try:
            for c in criteria:
                if c and c != 'ALL':
                    raise ValueError('I am not very good at searching')
            with self.lock:
                seqs = self._list(self._path(self.selected)).keys()
                return _l('SEARCH', ('OK', [' '.join('%d' % s for s in seqs)]))
        except (IOError, OSError, ValueError, KeyError, IndexError) as e:
            return _l('SEARCH', ('NO', ['Search failed: %s' % e]))

    def store(self, message_set, command, flags):
        if command not in ('+FLAGS', '+FLAGS.SILENT'):
            raise ValueError('I do not know how to %s' % command)
        if flags not in ('(\Deleted)', ):
            raise ValueError('I do not know how to set %s' % flags)

        mpath = self._path(self.selected)
        files = self._list(mpath)
        for seq in (int(s) for s in message_set.split(',')):
            if seq not in files:
                continue
            for sub in ('cur', 'new'):
                fn = os.path.join(mpath, sub, files[seq])
                if os.path.exists(fn):
                    os.remove(fn)
        return _l(
            'STORE %s %s %s' % (message_set, command, flags),
            ('OK', [message_set]))

    def fetch(self, message_set, message_parts):
        try:
            seq = int(message_set)
            mpath = self._path(self.selected)
            files = self._list(mpath)
            for sub in ('cur', 'new'):
                fn = os.path.join(mpath, sub, files[seq])
                data = open(fn, 'rb').read().replace('\n', '\r\n')
                return _l(
                    'FETCH %s %s' % (message_set, message_parts),
                    ('OK', [['', data]]))
        except (IOError, OSError, ValueError, KeyError, IndexError) as e:
            pass
        return _l('FETCH', ('NO', ['Fetch failed: %s' % e]))

    def close(self): return _l('CLOSE', ('OK', ['This is a noop']))
    def logout(self): return _l('LOGOUT', ('OK', ['This is a noop']))
    def expunge(self): return _l('EXPUNGE', ('OK', ['This is a noop']))
