#! /usr/bin/python3
# -*- coding: utf-8 -*-
# Authors: Dieter Vansteenwegen
# Institution: VLIZ (Vlaams Instituut voor de Zee)

__author__ = 'Dieter Vansteenwegen'
__email__ = 'dieter.vansteenwegen@vliz.be'
__status__ = 'Production'
__project__ = 'Panthyr'
__project_link__ = 'https://waterhypernet.org/equipment/'

__all__ = ['pFTP', 'FTPFileExistsOnServer', 'FTPUploadFailed']

import ftplib  # nosec B402  # ftp over TLS if used. Not ideal, but that is what is available.
import os
import logging
from typing import List, Union
import socket
import re
from datetime import datetime as dt

TIMEOUTDEFAULT = 20  # FTP server timeout


def current_year_str() -> str:
    return dt.now().strftime('%Y')


class FTPError(Exception):
    pass


class FTPCannotConnectError(FTPError):
    pass


class FTPCannotLoginError(FTPError):
    pass


class FTPFileExistsOnServer(FTPError):
    """Trying to upload a file that already exists on server"""
    pass


class FTPUploadFailed(FTPError):
    """
    Tried uploading a file, but uploading failed and
    the file does not exist on server afterwards.
    """
    pass


class pFTP:
    """Access to the FTP server for storing data and logs"""

    def __init__(
        self,
        server: str,
        user: str,
        pw: str,
        timeout: int = TIMEOUTDEFAULT,
    ) -> None:
        self.log = logging.getLogger(__name__)
        self.server = server
        self.user = user
        self.pw = pw
        self.timeout = timeout
        try:
            self.ftp = ftplib.FTP_TLS(host=self.server, timeout=self.timeout)  # nosec B321
        except socket.gaierror as e:
            self.log.exception(f'could not connect to {self.server}: {e}', exc_info=True)
            raise FTPCannotConnectError from e

    def __enter__(self):
        """Use as context handler"""
        self.login()
        return self

    def __exit__(self, exc_type, exc_value, tb):
        """Use as context handler"""
        self.quit()

    def login(self) -> None:
        """Log on to the server with provided credentials.

        Raises:
            ftplib.error_perm: if connection fails.
        """
        try:
            self.ftp.login(user=self.user, passwd=self.pw)
        except ftplib.error_perm as e:
            self.log.exception(f'could not log in to {self.server}', exc_info=True)
            raise FTPCannotLoginError from e

    def cwd(self, target_dir: str) -> None:
        """Change the working directory on the server.

        Always changes to the current year subdirectory from target_dir.

        Args:
            target_dir (str): directory to change to.
        """
        target_dir_checked = re.sub('[^0-9a-zA-Z_]+', '_', target_dir)
        if target_dir_checked != target_dir:
            self.log.warning(
                f'Invalid characters in directory name. Replaced [{target_dir}] '
                f'with [{target_dir_checked}].', )

        try:
            self._prep_dir(target_dir_checked)
            self.ftp.cwd(target_dir_checked)
            year_str = current_year_str()
            self._prep_dir(year_str)
            self.ftp.cwd(year_str)
        except ftplib.error_perm as e:
            self.log.exception(f'Could not change directory to [{target_dir}]')
            raise FTPError from e

    def _prep_dir(self, dir: str) -> None:
        """Check if subdirectory exists in the current working directory. If not, create.

        Args:
            dir (str): subdirectory to check/create
        """
        dir_lst = self.get_contents('.')[0]
        dir_lst.append('.')
        if dir not in dir_lst:
            self.log.debug(f'Creating directory [{dir}] on server...')
            self.ftp.mkd(dir)

    def _temp_cwd(self, target_dir: Union[str, None]) -> Union[str, None]:
        """Temporarily change the working directory.

        If target_dir, get the current working directory, change to target,
            then return the inital working directory.

        Args:
            target_dir (Union[str, None]): target directory for running operation.

        Returns:
            Union[str, None]: initial working directory if target_dir is set, otherwise None
        """
        ret = None
        if target_dir:
            ret = self.pwd()
            self.cwd(target_dir)
        return ret

    def pwd(self) -> str:
        """Get the current working directory on the server.

        Returns:
            str: current working directory
        """
        return self.ftp.pwd()

    def get_contents(self, dir='.') -> List[List[str]]:
        """Return files and subdirectories of dir on server.

        Args:
            dir (str, optional): path to directory to get contents of.
                Defaults to '.' (current working directory)

        Returns:
            List[List[str]]: List of two lists,
                first containing all directories
                second containing all files.
                Both are empty if there are no files/directories
        """
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

    def upload_file(
        self,
        file: str,
        # target_dir: Union[None, str] = '.',
        overwrite: bool = True,
        target_filename: Union[str, None] = None,
    ) -> None:
        """Upload file from local system to server.

        File is uploaded to target_dir.
        If overwrite is set to False, first check if file exists on remote.
        If file exists, raise FileExistsOnServer and exit.

        Args:
            file (str): path to file to be uploaded
            target_dir (Union[None, str]): remote target directory for the upload.
                                    Defaults to None (current working directory)
            overwrite (bool, optional): Silently overwrite file if it exists on server.
                                            Defaults to True.

        Raises:
            ValueError: source file does not exist on local system.
            FileExistsOnServer: target file exists on server and overwrite == False
            UploadFailed: issue during upload of file
        """
        if not os.path.isfile(file):
            raise ValueError(f'File {file} does not exist.')

        # initial_dir = self._temp_cwd(target_dir)
        if not target_filename:
            target_filename = os.path.basename(file)
        if not overwrite and self._file_exists(target_filename):
            raise FTPFileExistsOnServer

        # first argument to STOR is the target filename on the server, including path
        ret_ftp = self.ftp.storbinary(f'STOR {target_filename}', open(file, 'rb'))

        if not self._file_exists(target_filename):
            self.log.error(
                f'Uploading {file} failed, doesn\'t exist on server. Return from STOR: {ret_ftp}',
            )
            raise FTPUploadFailed(ret_ftp)

        # self._temp_cwd(initial_dir)

    def _file_exists(self, file: str) -> bool:
        """Check if file exists in current directory.

        Args:
            file (str): file to be checked

        Returns:
            bool: [description]
        """
        files = self.get_contents()[1]
        return any(file.lower() == file_ftp.lower() for file_ftp in files)

    def get_size(self, file: str) -> Union[int, None]:
        """Get size of file on server.

        Args:
            file (str): filename to get size of

        Returns:
            Union[int, None]: file size in bytes or None if not succesful.
        """
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
