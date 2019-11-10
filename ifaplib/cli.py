# This file is part of ifaplib
#
# Ifaplib is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version.
#
# Ifaplib is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with ifaplib. If not, see <https://www.gnu.org/licenses/>.
#
"""\
Command Line Interface for interacting with IFAP filesystems.

Run `python -m ifaplib help` for instructions.
"""
import base64
import getopt
import getpass
import hashlib
import imaplib
import json
import os
import sys

from . import IFAP
from .backends import FilesystemIMAP


def _fail(msg, code=1):
    sys.stderr.write(msg+'\n')
    sys.exit(code)


def _loginfile():
    return os.path.expanduser('~/.ifap-login')


def _load_creds():
    try:
        creds = {}
        with open(_loginfile(), 'r') as fd:
            creds.update(json.loads(base64.b64decode(fd.read())))
        return creds
    except (OSError, IOError, ValueError):
        return None


def _get_ifap(creds=None):
    if creds is None:
        creds = _load_creds()
        if creds is None:
            _fail('Please log in first.', code=2)

    host, port = creds['imap'].split(':')
    if host == 'maildir':
        imap = FilesystemIMAP(port, create=0o700)
    else:
        port = int(port)
        while not creds.get('password'):
            creds['password'] = getpass.getpass(
                'IMAP password for %(username)s@%(imap)s: ' % creds).strip()
        try:
            cls = (imaplib.IMAP4 if (port == 143) else imaplib.IMAP4_SSL)
            imap = cls(host, port)
            imap.login(creds['username'], creds['password'])
        except cls.error as e:
            _fail('IMAP login failed: %s' % e, code=3)

    ifap = IFAP(imap, creds['mailbox'])
    if creds['key'] and creds['key'] != 'None':
        ifap.set_encryption_key(creds['key'])
    return ifap


def _clean_path(path):
    while path[:1] == '/':
        path = path[1:]
    while path[-1:] == '/':
        path = path[:-1]
    return path.replace('//', '/')


def _put_command(opts, args):
    """Put a file or files in IFAP (upload)

Example: python -m ifaplib put README.md setup.py /tmp
Options:
    -v, --verbose     Report progress on stdout
    -r, --recurse     Upload entire directory trees

The last argument should be the destination directory."""
    dest = _clean_path(args.pop(-1))
    opts = dict(opts)
    for fn in args:
        if not os.path.exists(fn):
            raise OSError("File not found: %s" % fn)
    if not args:
        return True
    with _get_ifap() as ifap:
        for fn in args:
            if dest:
                dest_fn = os.path.join(dest, os.path.basename(fn))
            else:
                dest_fn = os.path.basename(fn)
            with open(fn, 'r') as fd:
                data = fd.read()
            with ifap.open(dest_fn, 'w') as fd:
                fd.write(data)
            if '--verbose' in opts or '-v' in opts:
                print("%s -> ifap:%s" % (fn, dest_fn))
    return True


def _get_command(opts, args):
    """Fetch a file or files from IFAP (download)

This command will fetch its arguments from IFAP and store as local
files. The name of the created files will be derived in the obvious
way from the name in IFAP.

Example: python -m ifaplib get /tmp/README.md /tmp/README.txt .
Options:
    -v, --verbose     Report progress on stdout
    -r, --recurse     Fetch entire directory trees
    -f, --force       Overwrite already existing files, if necessary
    --version=N       Request a specific versions of the files

The last argument should be the destination directory. When requesting a
specific version, it doesn't make sense to request multiple files."""
    dest_dir = args.pop(-1)
    if not os.path.exists(dest_dir) or not os.path.isdir(dest_dir):
         _fail('Not a directory: %s' % dest_dir)
    ifap = _get_ifap()

    full_path = False
    def _fn(fn):
        while fn[:1] == '/':
            fn = fn[1:]
        if full_path:
            target = os.path.join(dest_dir, fn)
        else:
            target = os.path.join(dest_dir, os.path.basename(fn))
        return target

    def _pmkdir(fn):
        dn = os.path.dirname(fn)
        if not os.path.exists(dn):
            if dn and fn != dn:
                _pmkdir(dn)
                os.mkdir(dn)

    opts = dict(opts)
    if '--recurse' in opts or '-r' in opts:
        full_path = True
        files = []
        with ifap:
            ls = sorted(ifap._tree.keys())
        for prefix in args:
            while prefix[:1] == '/':
                prefix = prefix[1:]
            files.extend([f for f in ls if f.startswith(prefix)])
        args = sorted(list(set(files)))

    if '--force' not in opts and '-f' not in opts:
        for fn in args:
            target = _fn(fn)
            if os.path.exists(target):
                _fail('Cravenly refusing to overwrite %s' % target)

    version = int(opts.get('-V', opts.get('--version', 0))) or None
    if version and len(args) > 1:
        _fail('Multiple files and --version are incompatible.')
    with ifap:
        for fn in args:
            while fn[:1] == '/':
                fn = fn[1:]
            target = _fn(fn)
            if full_path:
                _pmkdir(target)
            data = ifap.open(fn, 'r', version=version).read()
            open(target, 'w').write(data)
            if '--verbose' in opts or '-v' in opts:
                print("ifap:%s -> %s" % (fn, target))
    return True


def _cat_command(opts, args):
    """Print the contents of a file or files from IFAP

Example: python -m ifaplib cat /tmp/README.md
Options:
    --version=N       Request a specific versions of the file

When requesting a specific version, it doesn't make sense to request
multiple files."""
    opts = dict(opts)
    version = int(opts.get('-V', opts.get('--version', 0))) or None
    if version and len(args) > 1:
        _fail('Multiple files and --version are incompatible.')
    with _get_ifap() as ifap:
        for fn in args:
            while fn[:1] == '/':
                fn = fn[1:]
            with ifap.open(fn, 'r', version=version) as fd:
                sys.stdout.write(fd.read())
    return True


def _vers_command(opts, args):
    """Set the desired number of versions for a file

Example: python -m ifaplib vers 4 /tmp/README.md

"""
    opts = dict(opts)
    versions = int(args.pop(0))
    with _get_ifap() as ifap:
        for fn in args:
            with ifap.open(fn, 'r+') as fd:
                fd.metadata['versions'] = versions
        ifap.synchronize(snapshot=True)
    return True


def _rm_command(opts, args):
    """Remove a file or files

Example: python -m ifaplib rm /tmp/README.md
Options:
    --version=N       Remove a specific versions of the file

Note: removing the deletion marker will undelete the file!
"""
    opts = dict(opts)
    version = int(opts.get('-V', opts.get('--version', 0)))
    if version and len(args) != 1:
        _fail('Multiple files and --version are incompatible.')
    with _get_ifap() as ifap:
        for fn in args:
            ifap.remove(fn, versions=([version] if version else None))
        ifap.synchronize(cleanup=True, snapshot=True)
    return True


def _ls_command(opts, args):
    """List files

Example: python -m ifaplib ls -l /
Options:
    -l, --metadata     List full metadata for each file
    -a, --all          List all files

Defaults to listing the root directory, if any arguments are present it
will list those directories instead."""
    opts = dict(opts)

    verbose = ('-l' in opts or '--long' in opts or '--metadata' in opts)
    def _ls(ifap, files):
        if verbose:
            ll = max(len(f) for f in files)
            fmt = '%%-%d.%ds %%s' % (ll, ll)
            for f in files:
                if f in ('.', '..'):
                    continue
                if f in ifap._tree:
                    print(fmt % (f, json.dumps({
                        'metadata': ifap._tree[f][1],
                        'versions': sorted(list(ifap._tree[f][2]))},
                        sort_keys=True)))
                else:
                    print(fmt % (f, '{}'))
        else:
            print('\n'.join(files))

    with _get_ifap() as ifap:
        if '-a' in opts or '--all' in opts:
            flist = sorted(ifap._tree.keys())
        elif not args:
            flist = ifap.listdir('/')
        else:
            flist = []
            for prefix in args:
                flist.extend(ifap.listdir(prefix))
        if flist:
            _ls(ifap, sorted(list(set(flist))))
    return True


def _mount_command(opts, args):
    """Mount an IFAP filesystem using FUSE

Example: python -m ifaplib mount ./tmp
Options:
    -v, --verbose     Log activity to STDERR.

The process will hang, you can put it in the background yourself if you
prefer."""
    try:
        from .fuse_driver import mount
    except ImportError as e:
        _fail('Is fusepy installed? Error: %s' % e, 98)
    opts = dict(opts)
    verbose = ('-v' in opts or '--verbose' in opts)
    mount(_get_ifap(), args[0], verbose=verbose)
    return True


def _logout_command(opts, args):
    """Log out from an IMAP/IFAP server

This will delete your IMAP password from ~/.ifap-login.  Note that it
will leave the secret key and other settings intact, remove the file by
hand if you want them gone too.
"""
    creds = _load_creds()
    del creds['password']
    with open(_loginfile(), 'w') as fd:
        os.chmod(_loginfile(), 0o600)
        fd.write(base64.encodestring(json.dumps(creds)))
    sys.stderr.write('OK: Deleted password from %s\n' % _loginfile())
    return True


def _login_command(opts, args):
    """Log in to an IMAP/IFAP server

Options:
    --imap=host:port       Defaults to "localhost:143"
    --mailbox=mailbox      Defaults to "FILE_STORAGE"
    --username=username    Defaults to $USER
    --password=password    Defaults to prompting the user
    --key=random_string    Defaults to generating a new, strong key

If the key is set to the string "None" (without the quotes), that
will disable IFAP's encryption.

Setting the IMAP server to maildir:/path/to/folder will use the
built-in local Maildir storage, instead of real IMAP.

Warning: This will store your IMAP and IFAP access credentials,
lightly obfuscated, in ~/.ifap-login. Use the logout command to
delete the IMAP password from this file."""
    defaults = _load_creds() or {}
    opts = dict(opts)
    creds = {
        'imap': opts.get('--imap', defaults.get('imap', 'localhost:143')),
        'mailbox': opts.get('--mailbox', defaults.get('mailbox', 'FILE_STORAGE')),
        'username': opts.get('--username', defaults.get('username', os.getenv('USER'))),
        'password': opts.get('--password', defaults.get('password')),
        'key': opts.get('--key', defaults.get('key'))}
    if not creds['imap'].startswith('maildir:'):
        while not creds['password']:
            creds['password'] = getpass.getpass(
                'IMAP password for %(username)s@%(imap)s: ' % creds).strip()
    if creds['key'] is None:
        creds['key'] = base64.b64encode(os.urandom(32)).strip()
        sys.stderr.write('Generated key: %s\n' % creds['key'])

    _get_ifap(creds).synchronize()

    with open(_loginfile(), 'w') as fd:
        os.chmod(_loginfile(), 0o600)
        fd.write(base64.encodestring(json.dumps(creds)))
    return True


def _help_command(opts, args):
    """Get help

You can get further instructions on each command by running
`help command`."""
    for cmd in args:
        print('%s: %s' % (cmd, dict(_COMMANDS)[cmd][0].__doc__))
    if not args:
        print("""\
This is the Command Line Interface for the IMAP File Access Protocol.

Usage: python -m ifaplib <command> [options] [arguments...]
Commands:

%(commands)s

Examples:
    python -m ifaplib help login
    python -m ifaplib cat /project/README.md
    python -m ifaplib ls -l
""" % {'commands': '\n'.join([
            '    %-10.10s %s' % (cmd, synopsis[0].__doc__.splitlines()[0])
            for cmd, synopsis in _COMMANDS])})
    return True


_COMMANDS = [
    ('help',   (_help_command,   '',      [])),
    ('ls',     (_ls_command,     'al',    ['all', 'long', 'metadata'])),
    ('put',    (_put_command,    'vr',    ['verbose', 'recurse'])),
    ('get',    (_get_command,    'vrfV:', ['verbose', 'recurse', 'force',
                                           'version='])),
    ('cat',    (_cat_command,    'V:',    ['version='])),
    ('rm',     (_rm_command,     'V:',    ['version='])),
    ('vers',   (_vers_command,   '',      [])),
    ('mount',  (_mount_command,  'v',     ['verbose'])),
    ('login',  (_login_command,  '',      ['imap=', 'username=', 'mailbox=',
                                           'password=', '--key='])),
    ('logout', (_logout_command, '',     []))]


def cli():
    try:
        cmd, shortlist, longlist = dict(_COMMANDS)[sys.argv[1]]
        if not cmd(*getopt.getopt(sys.argv[2:], shortlist, longlist)):
            sys.exit(1)
    except KeyboardInterrupt:
        sys.stderr.write('Interrupted\n')
        sys.exit(99)
    except (getopt.GetoptError, IndexError) as e:
        _help_command([], [])
        if len(sys.argv) > 1:
            sys.stderr.write('Error(%s): %s\n' % (sys.argv[1], e))
        sys.exit(1)
