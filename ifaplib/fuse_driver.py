"""FUSE driver for the IMAP File Access Protocol

This is a basic FUSE driver for accessing an IFAP file store.

Use it like so: python -m ifaplib mount /path/to/mountpoint

The only "weird magic" in this module is the stat cache that lets us pretend
things like mkdir() succeed and have meaning, even though IFAP does not actually
have a concept of directories.
"""
import errno
import fcntl
import os
import stat
import sys
import time

from fusepy import FUSE, FuseOSError, Operations


class IFAP_Fuse(Operations):
    def __init__(self, ifap, verbose):
        self.root = '/tmp'
        self.ifap = ifap
        self.verbose = verbose
        self.fhs = {}
        self.stat_cache = {}

    def _l(self, msg, stuff=None):
        if self.verbose:
            sys.stderr.write('%s\n' % msg)
        return stuff

    def _make_stat(self, mode):
        now = int(time.time())
        return {
            'st_ctime': now, 'st_atime': now,
            'st_gid': os.getgid(), 'st_uid': os.getuid(),
            'st_size': 0, 'st_nlink': 1,
            'st_mode': mode}

    def access(self, path, mode):
        return self._l('access(%s, %s)' % (path, mode), 0)

    def chmod(self, path, mode):
        return self._l('chmod(%s, %s)' % (path, mode), 0)

    def chown(self, path, uid, gid):
        return self._l('chown(%s, %s, %s)' % (path, uid, gid), 0)

    def mkdir(self, path, mode):
        self.stat_cache[path] = self._make_stat(stat.S_IFDIR | mode)
        return self._l('mkdir(%s, %s)' % (path, mode), 0)

    def getattr(self, path, fh=None):
        self._l('getattr(%s, %s)' % (path, fh))
        try:
            stat = self.ifap.lstat(path)
            if path in self.stat_cache:
                del self.stat_cache[path]
            return self._l(' -> %s' % stat, stat)
        except Exception as e:
            if path in self.stat_cache:
                return self._l(
                    ' -> %s' % self.stat_cache[path], self.stat_cache[path])
            self._l(' -> %s' % (e,))
            raise FuseOSError(errno.ENOENT)

    def readdir(self, path, fh):
        self._l('readdir(%s, %s)' % (path, fh))
        try:
            dirents = self.ifap.listdir(path)
            self._l(' -> %s' % (dirents,))
            for d in dirents:
                yield d
        except Exception as e:
            self._l(' -> %s' % (e,))
            raise FuseOSError(errno.ENOENT)

    def readlink(self, path):
        self._l('readlink(%s)' % (path,))
        raise FuseOSError(errno.EACCES)

    def mknod(self, path, mode, dev):
        self._l('mknod(%s, %s, %s)' % (path, mode, dev))
        raise FuseOSError(errno.EACCES)

    def rmdir(self, path):
        self._l('rmdir(%s)' % (path,))
        raise FuseOSError(errno.EACCES)

    def statfs(self, path):
        self._l('statfs(%s)' % (path,))
        stv = os.statvfs('/')
        return dict((key, getattr(stv, key)) for key in ('f_bavail', 'f_bfree',
            'f_blocks', 'f_bsize', 'f_favail', 'f_ffree', 'f_files', 'f_flag',
            'f_frsize', 'f_namemax'))

    def unlink(self, path):
        self._l('unlink(%s)' % (path,))
        raise FuseOSError(errno.EINVAL)

    def symlink(self, name, target):
        self._l('symlink(%s, %s)' % (name, target))
        raise FuseOSError(errno.EACCES)

    def rename(self, old, new):
        self._l('rename(%s, %s)' % (old, new))
        raise FuseOSError(errno.EACCES)

    def link(self, target, name):
        self._l('link(%s, %s)' % (target, name))
        raise FuseOSError(errno.EACCES)

    def utimens(self, path, times=None):
        self._l('utimens(%s, %s)' % (path, times))
        raise FuseOSError(errno.EACCES)

    # File methods
    # ============

    def _modestring(self, flags):
        modes = ''
        if flags & os.O_APPEND:
            modes += 'a'
        if flags & os.O_RDWR:
            modes += 'r+'
        elif flags & os.O_RDONLY:
            modes = 'r'
        elif flags & os.O_WRONLY:
            modes = 'w'
        return (modes or 'r')

    def open(self, path, flags):
        self._l('open(%s, %s)' % (path, flags))
        try:
            fh = len(self.fhs) + 1
            self.fhs[fh] = (path, self.ifap.open(path, self._modestring(flags)))
            return fh
        except Exception as e:
            self._l(' -> %s' % (e,))
            raise FuseOSError(errno.EACCES)

    def create(self, path, mode, fi=None):
        self._l('create(%s, %s, %s)' % (path, mode, fi))
        self.stat_cache[path] = self._make_stat(mode)
        return self.open(path, os.O_WRONLY | os.O_CREAT)

    def read(self, path, length, offset, fh):
        self._l('read(%s, %s, %s, %s)' % (path, length, offset, fh))
        try:
            fd = self.fhs[fh][1]
            fd.seek(offset)
            return fd.read(length)
        except KeyError:
            raise FuseOSError(errno.EBADFD)

    def write(self, path, buf, offset, fh):
        self._l('write(%s, %s, %s, %s)' % (path, buf, offset, fh))
        try:
            fd = self.fhs[fh][1]
            fd.seek(offset)
            fd.write(buf)
            return len(buf)
        except KeyError:
            raise FuseOSError(errno.EBADFD)

    def truncate(self, path, length, fh=None):
        self._l('truncate(%s, %s, %s)' % (path, length, fh))
        try:
            if fh:
                fd = self.fhs[fh][1]
                return fd.truncate(length)
            else:
                for fn, fo in self.fhs.values():
                    if fn == path:
                        fo.truncate(length)
                    return 0
                fd = self.ifap.open(path, 'w')
                fd.truncate(length)
                fd.close()
                self.ifap.synchronize()
                return 0
        except KeyError:
            raise FuseOSError(errno.EBADFD)

    def flush(self, path, fh):
        return self._l('flush(%s, %s)' % (path, fh), 0)

    def release(self, path, fh):
        self._l('release(%s, %s)' % (path, fh))
        try:
            self.fhs[fh][1].close()  
            del self.fhs[fh]
            self.ifap.synchronize()
            return 0
        except KeyError as e:
            self._l(' -> ' % (e,))
            raise FuseOSError(errno.EBADFD)

    def fsync(self, path, fdatasync, fh):
        self.ifap.synchronize()
        return self._l('fsync(%s, %s, %s)' % (path, fdatasync, fh), 0)


def mount(ifap, mountpoint, verbose=False):
    with ifap: 
        FUSE(
            IFAP_Fuse(ifap, verbose), mountpoint,
            nothreads=True, foreground=True)
