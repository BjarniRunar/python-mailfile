from setuptools import setup

from ifaplib import __version__, __author__

classifiers = [
    'Development Status :: 4 - Beta',
    'Intended Audience :: Developers',
    'License :: OSI Approved :: BSD License',
    'Programming Language :: Python',
    'Programming Language :: Python :: 3',
    'Topic :: Security :: Cryptography',
    'Topic :: Software Development :: Libraries :: Python Modules']

setup(
    name = 'ifaplib',
    version = __version__,
    author = __author__,
    license = 'BSD',
    description = 'IMAP File Access Protocol',
    url = 'https://github.com/BjarniRunar/python-ifaplib',
    download_url = 'https://github.com/BjarniRunar/python-ifaplib/archive/v0.0.1.tar.gz',
    keywords = 'imap imap4 imaplib crypto cryptography',
    install_requires = ['cryptography'],
    classifiers = classifiers,
    packages = ['ifaplib'])
