from setuptools import setup

from ifaplib import __version__, __author__

classifiers = [
    'Development Status :: 4 - Beta',
    'Intended Audience :: Developers',
    'License :: OSI Approved :: GNU Lesser General Public License v3 or later (LGPLv3+)',
    'Programming Language :: Python',
   #'Programming Language :: Python :: 3',
    'Topic :: System :: Filesystems',
    'Topic :: Communications :: Email :: Post-Office :: IMAP',
    'Topic :: Security :: Cryptography',
    'Topic :: Software Development :: Libraries :: Python Modules']

setup(
    name = 'ifaplib',
    version = __version__,
    author = __author__,
    license = 'LGPLv3+',
    description = 'IMAP File Access Protocol',
    url = 'https://github.com/BjarniRunar/python-ifaplib',
    download_url = 'https://github.com/BjarniRunar/python-ifaplib/archive/v0.0.1.tar.gz',
    keywords = 'imap imap4 imaplib fuse cryptography',
    install_requires = ['cryptography'],
    classifiers = classifiers,
    packages = ['ifaplib'])
