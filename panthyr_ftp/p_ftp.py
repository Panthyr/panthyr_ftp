#! /usr/bin/python3
# coding: utf-8

# Module: panthyr_ftp
# Authors: Dieter Vansteenwegen
# Institution: VLIZ (Vlaams Institute voor de Zee)

__author__ = 'Dieter Vansteenwegen'
__version__ = '0.1b'
__credits__ = 'Dieter Vansteenwegen'
__email__ = 'dieter.vansteenwegen@vliz.be'
__status__ = 'Development'
__project__ = 'Panthyr'
__project_link__ = 'https://waterhypernet.org/equipment/'

import ftplib
import os
import logging
from typing import List, Union
import socket

TIMEOUTDEFAULT = 10  # FTP server timeout


def initialize_logger() -> logging.Logger:
    """Set up logger
    If the module is ran as a module, name logger accordingly as a sublogger.
    Returns:
        logging.Logger: logger instance
    """
    if __name__ == '__main__':
        return logging.getLogger('__main__')
    else:
        return logging.getLogger('__main__.{}'.format(__name__))


class FileExistsOnServer(Exception):
    """Trying to upload a file that already exists on server"""
    pass


class UploadFailed(Exception):
    """Tried uploading a file, but file does not exist on server afterwards."""
    pass


class pFTP:
    """Access to the FTP server for storing data and logs"""
    def __init__(self,
                 server: str,
                 user: str,
                 pw: str,
                 use_sftp: bool = True,
                 timeout: int = TIMEOUTDEFAULT) -> None:
        self.log = initialize_logger()
        self.server = server
        self.user = user
        self.pw = pw
        self.secure = use_sftp
        self.timeout = timeout
        try:
            if self.secure:
                self.ftp = ftplib.FTP(host=self.server, timeout=self.timeout)
            else:
                self.ftp = ftplib.FTP_TLS(host=self.server, timeout=self.timeout)
        except socket.gaierror as e:
            self.log.error(f'could not connect to {self.server}: {e}', exc_info=True)
            raise

    def __enter__(self):
        """Use as context handler"""
        self.login()

    def __exit__(self):
        """Use as context handler"""
        self.ftp.close()

    def login(self) -> None:
        try:
            self.ftp.login(user=self.user, passwd=self.pw)
        except ftplib.error_perm as e:
            self.log.error(f'could not log in to {self.server}: {e}', exc_info=True)
            raise

    def _cwd(self, target_dir: str) -> None:
        try:
            self.ftp.cwd(target_dir)
        except ftplib.error_perm as e:
            self.log.error(f'could not change directory to {target_dir}: {e}', exc_info=True)
            raise

    def get_contents(self, dir='.') -> List[List[str]]:

        cmd = f'LIST {dir}'  # command to be sent to the server
        ret_ftp: List[str] = []  # empty list to hold lines returned by ftp command
        self.ftp.retrlines(cmd, callback=ret_ftp.append)

        ret: List[List[str]] = [[], []]
        for line in ret_ftp:
            if line[0] == 'd':  # Line is directory
                ret[0].append(' '.join(line.split()[8:]))
            else:  # Line is file
                ret[1].append(' '.join(line.split()[8:]))

        return ret

    def upload_file(self, file: str, overwrite: bool = True) -> None:
        """[summary]

        [extended_summary]

        Args:
            file (str): [description]
            overwrite (bool, optional): [description]. Defaults to True.

        Raises:
            ValueError: [description]
            FileExistsOnServer: [description]
            UploadFailed: [description]
        """
        if not os.path.isfile(file):
            raise ValueError(f'File {file} does not exist.')

        target_filename = os.path.basename(file)
        if not overwrite and self._file_exists(target_filename):
            raise FileExistsOnServer

        # first argument to STOR is the target filename on the server, including path
        ret_ftp = self.ftp.storbinary(f'STOR {target_filename}', open(file, 'rb'))

        if not self._file_exists(target_filename):
            self.log.error(
                f'Uploading {file} failed, doesn\'t exist on server. Return from STOR: {ret_ftp}')
            raise UploadFailed(ret_ftp)

    def _file_exists(self, file: str) -> bool:
        files = self.get_contents()[1]
        return any(file.lower() == file_ftp.lower() for file_ftp in files)

    def get_size(self, file: str) -> Union[int, None]:
        # some servers respond with 550 SIZE not allowed in ASCII mode if not set to TYPE I
        self.ftp.voidcmd('TYPE I')
        return self.ftp.size(file)  # returns None if not succesful

    def quit(self) -> None:
        """Send a QUIT command to the server and close the connection.

        from ftplib: This is the “polite” way to close a connection, but it may raise an exception 
                        if the server responds with an error to the QUIT command. 
                        This implies a call to the close() method which renders 
                        the FTP instance useless for subsequent calls.
        """
        self.ftp.quit()
