===============================
Panthyr FTP
===============================

Example code:
.. code:: python

    >>> from panthyr_ftp.p_ftp import pFTP
    >>> f=pFTP('ftp.xxx.com', user = 'xxx', pw = 'yyy')
    >>> f.login()
    >>> f.get_contents()
    [['DIR1', 'public'], []]
    >>> f.upload_file(file = 'README.md', target_dir = '/public/')
    >>> f.pwd()
    '/'
    >>> f.cwd('public')
    >>> f.pwd()
    '/public'
    >>> f.get_contents()
    [[],[]]
    >>> f.upload_file('README2.md')     
    >>> f.get_contents()
    [[], ['README.md', 'README2.md']]
    >>> f.get_size('README.md')     
    274
