import os
import threading


class FilesystemIMAP(object):
    """
    This is a filesystem-backed "mock IMAP server" for use with IFAP.
    It works with a tree that looks surprisingly similar to a Maildir.
    """
    def __init__(self, base_dir, port=None, sep=':', create=False):
        self.sep = sep
        self.base_dir = base_dir
        self.selected = []
        self.ls_cache = {}
        self.response_data = {}
        self.lock = threading.RLock()
        if create and not os.path.exists(base_dir):
            os.mkdir(base_dir, 0o700 if (create is True) else create)

    def _path(self, path):
        if path == '/':
            return self.base_dir
        else:
            return os.path.join(self.base_dir, path)

    def _list(self, path):
        if path not in self.ls_cache:
            self.ls_cache[path] = results = {}
            for sub in ('cur', 'new'):
                sub = os.path.join(path, sub)
                if os.path.isdir(sub):
                    results.update(dict(
                        (self._fn_parse(fn)[0], fn)
                        for fn in os.listdir(sub) if fn[:4] == 'eml-'))
        return self.ls_cache[path]

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
                return ('OK', ['APPEND completed: %8.8x' % seq])
        except (IOError, OSError, ValueError, KeyError, IndexError) as e:
            return ('NO', ['APPEND failed: %s' % e])

    def response(self, code):
        rv = self.response_data.get(code)
        if code in self.response_data:
            del self.response_data[code]
        return rv

    def select(self, mailbox='INBOX', readonly=False):
        try:
            self.response_data = {}
            message_count = len(self._list(self._path(mailbox)))
            self.selected = mailbox
            return ('OK', [message_count])
        except (IOError, OSError, ValueError, KeyError, IndexError) as e:
            return ('NO', ['No such mailbox'])

    def search(self, charset, *criteria):
        try:
            for c in criteria:
                if c and c != 'ALL':
                    raise ValueError('I am not very good at searching')
            with self.lock:
                seqs = self._list(self._path(self.selected)).keys()
                return ('OK', [' '.join('%d' % s for s in seqs)])
        except (IOError, OSError, ValueError, KeyError, IndexError) as e:
            return ('NO', ['Search failed: %s' % e])

    def fetch(self, message_set, message_parts):
        try:
            seq = int(message_set)
            mpath = self._path(self.selected)
            files = self._list(mpath)
            for sub in ('cur', 'new'):
                fn = os.path.join(mpath, sub, files[seq])
                if os.path.exists(fn):
                    data = open(fn, 'rb').read().replace('\n', '\r\n')
                    return ('OK', [['', data]])
        except (IOError, OSError, ValueError, KeyError, IndexError) as e:
            pass
        return ('NO', ['Search failed: %s' % e])

    def close(self): return ('OK', ['This is a noop'])
    def logout(self): return ('OK', ['This is a noop'])
