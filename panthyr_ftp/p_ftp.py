#! /usr/bin/python3
# -*- coding: utf-8 -*-
# Authors: Dieter Vansteenwegen
# Institution: VLIZ (Vlaams Instituut voor de Zee)

__author__ = 'Dieter Vansteenwegen'
__email__ = 'dieter.vansteenwegen@vliz.be'
__status__ = 'Production'
__project__ = 'Panthyr'
__project_link__ = 'https://waterhypernet.org/equipment/'

__all__ = [
    'pFTP',
    'FTPFileExistsOnServer',
    'FTPUploadFailed',
    'enable_debug_logging',
    'test_connection_stability',
]

import logging
import os
import re
import socket
import time
from datetime import datetime as dt
from functools import wraps
from typing import Any, Callable, List, Union

import ftputil  # ftputil for high-level FTP operations
import ftputil.error

TIMEOUTDEFAULT = 20  # FTP server timeout
MAX_RETRIES = 2  # Maximum number of retry attempts for unstable connections
RETRY_DELAY_BASE = 2  # Base delay in seconds for exponential backoff
CONNECTION_CHECK_INTERVAL = 30  # Seconds between connection health checks


def current_year_str() -> str:
    return dt.now(tz=dt.now().astimezone().tzinfo).strftime('%Y')


def format_file_size(size_bytes: int) -> str:
    """Format file size in bytes to kB with appropriate precision.

    Args:
        size_bytes: File size in bytes

    Returns:
        str: Formatted size string (e.g., "1.5 kB", "1023 bytes")
    """
    if size_bytes < 1024:
        return f'{size_bytes} bytes'
    else:
        size_kb = size_bytes / 1024
        if size_kb >= 100:
            return f'{size_kb:.0f} kB'
        else:
            return f'{size_kb:.1f} kB'


def retry_on_connection_error(max_retries: int = MAX_RETRIES, base_delay: float = RETRY_DELAY_BASE):
    """Decorator to retry FTP operations on connection errors with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Base delay in seconds for exponential backoff
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(self, *args, **kwargs) -> Any:
            last_exception = None

            for attempt in range(max_retries + 1):  # +1 for initial attempt
                self.log.debug(
                    f'[RETRY_DECORATOR] Attempt {attempt + 1}/{max_retries + 1} for {func.__name__}'
                )
                try:
                    # Check if connection is healthy before attempting operation
                    if hasattr(self, 'ftp') and self.ftp and hasattr(self, '_check_connection'):
                        self.log.debug(
                            f'[RETRY_DECORATOR] Checking connection health before {func.__name__}'
                        )
                        self._check_connection()

                    self.log.debug(f'[RETRY_DECORATOR] Executing {func.__name__}')
                    result = func(self, *args, **kwargs)
                    self.log.debug(f'[RETRY_DECORATOR] {func.__name__} completed successfully')
                    return result

                except (
                    ftputil.error.FTPError,
                    OSError,
                    socket.error,
                    ConnectionResetError,
                    ConnectionAbortedError,
                    socket.timeout,
                    socket.gaierror,
                ) as e:
                    last_exception = e
                    self.log.debug(
                        f'[RETRY_DECORATOR] Exception caught in {func.__name__}: {type(e).__name__}: {e}'
                    )

                    if attempt < max_retries:
                        delay = base_delay * (2**attempt)  # Exponential backoff
                        self.log.warning(
                            f'[RETRY_DECORATOR] Connection error in {func.__name__} (attempt {attempt + 1}/'
                            f'{max_retries + 1}): {e}. '
                            f'Retrying in {delay}s...'
                        )

                        # Attempt to reconnect if connection is lost
                        if hasattr(self, '_reconnect'):
                            try:
                                self.log.debug(
                                    f'[RETRY_DECORATOR] Attempting reconnection after {func.__name__} failure'
                                )
                                self._reconnect()
                            except Exception as reconnect_error:
                                self.log.debug(
                                    f'[RETRY_DECORATOR] Reconnection attempt failed: {reconnect_error}'
                                )

                        self.log.debug(f'[RETRY_DECORATOR] Sleeping {delay}s before retry')
                        time.sleep(delay)
                    else:
                        self.log.error(
                            f'[RETRY_DECORATOR] Operation {func.__name__} failed after {max_retries + 1} attempts: {e}'
                        )
                        raise last_exception

            return None  # Should never reach here

        return wrapper

    return decorator


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
    """Access to the FTP server with speed limiting and robust connection handling.

    This class provides enhanced FTP functionality including:
    - Atomic uploads using temporary files and rename operations
    - Upload speed limiting and custom chunk sizes
    - Automatic retry logic for unstable connections
    - Connection health monitoring and recovery
    - Comprehensive debug logging
    - Robust upload verification
    """

    def __init__(
        self,
        server: str,
        user: str,
        pw: str,
        timeout: int = TIMEOUTDEFAULT,
        max_retries: int = MAX_RETRIES,
        retry_delay: float = RETRY_DELAY_BASE,
        upload_speed_limit_kbps: Union[int, None] = 25,
        upload_chunk_size: int = 8192,
    ) -> None:
        """Initialize FTP client with enhanced reliability and speed control features.

        Args:
            server (str): FTP server hostname or IP address
            user (str): Username for FTP authentication
            pw (str): Password for FTP authentication
            timeout (int, optional): Connection timeout in seconds. Defaults to 20.
            max_retries (int, optional): Maximum retry attempts for failed operations.
                                            Defaults to 2.
            retry_delay (float, optional): Base delay between retries in seconds. Defaults to 2.0.
            upload_speed_limit_kbps (Union[int, None], optional): Maximum upload speed in kB/s.
                                                    None for unlimited speed. Defaults to None.
            upload_chunk_size (int, optional): Size of chunks for file transfer in bytes.
                                              Defaults to 8192.
        """
        self.log = logging.getLogger(__name__)
        # Set logger to DEBUG level for maximum troubleshooting info
        self.log.setLevel(logging.DEBUG)

        self.server = server
        self.user = user
        self.pw = pw
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.upload_speed_limit_kbps = upload_speed_limit_kbps
        self.upload_chunk_size = upload_chunk_size
        self.ftp = None  # Will be initialized in login()
        self._last_connection_check = 0  # Timestamp of last connection health check
        self._connection_lost = False  # Track if we know connection is lost

        speed_limit_str = (
            f'{upload_speed_limit_kbps} kB/s' if upload_speed_limit_kbps else 'unlimited'
        )
        self.log.debug(
            f'Initializing pFTP for server: {server}, user: {user}, '
            f'timeout: {timeout}s, max_retries: {max_retries}, retry_delay: {retry_delay}s, '
            f'upload_speed_limit: {speed_limit_str}, chunk_size: {upload_chunk_size} bytes'
        )

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
            ftputil.error.FTPError: if connection fails.
        """
        self.log.debug(f'[LOGIN] Starting login process to {self.server}')
        try:
            self.log.debug(f'[LOGIN] Creating FTPHost connection to {self.server}')
            # ftputil.FTPHost automatically handles login during connection
            self.ftp = ftputil.FTPHost(self.server, self.user, self.pw)
            self.log.debug(f'[LOGIN] Successfully connected to {self.server}')

            # Enable passive mode for better firewall/NAT compatibility
            self.log.debug('[LOGIN] Enabling passive mode')
            # Access the underlying ftplib connection to set passive mode
            # ftputil uses _session for the control connection
            try:
                if hasattr(self.ftp, '_session'):
                    self.ftp._session.set_pasv(True)
                    self.log.debug('[LOGIN] Passive mode enabled successfully via _session')
                else:
                    self.log.warning('[LOGIN] Could not access _session to set passive mode')
            except AttributeError as e:
                self.log.warning(f'[LOGIN] Could not set passive mode: {e}')

            # Set binary mode for reliable file transfers and SIZE command support
            self.log.debug('[LOGIN] Setting binary transfer mode')
            try:
                if hasattr(self.ftp, '_session'):
                    self.ftp._session.voidcmd('TYPE I')  # TYPE I = binary mode
                    self.log.debug('[LOGIN] Binary mode enabled successfully')
                else:
                    self.log.warning('[LOGIN] Could not access _session to set binary mode')
            except (ftputil.error.FTPError, OSError) as e:
                self.log.warning(f'[LOGIN] Could not set binary mode: {e}')

            # Set timeout if supported
            self.log.debug(f'[LOGIN] Setting timeout to {self.timeout}s')
            if hasattr(self.ftp, 'set_timeout'):
                self.ftp.set_timeout(self.timeout)
                self.log.debug(f'[LOGIN] Timeout set successfully')

            # Log current working directory after login
            self.log.debug(f'[LOGIN] Getting initial working directory')
            try:
                current_dir = self.ftp.getcwd()
                self.log.debug(f'[LOGIN] Initial working directory: {current_dir}')
            except Exception as e:
                self.log.debug(f'[LOGIN] Could not get initial working directory: {e}')

        except (ftputil.error.FTPError, OSError, socket.gaierror) as e:
            self.log.error(f'[LOGIN] Failed to connect/log in to {self.server}: {e}')
            self._connection_lost = True
            raise FTPCannotLoginError from e
        else:
            self._connection_lost = False
            self._last_connection_check = time.time()
            self.log.debug(f'[LOGIN] Login process completed successfully')

    def _check_connection(self) -> bool:
        """Check if FTP connection is still healthy.

        Returns:
            bool: True if connection is healthy, False otherwise
        """
        self.log.debug('[CHECK_CONNECTION] Starting connection health check')
        current_time = time.time()

        # Only check periodically to avoid overhead
        time_since_last_check = current_time - self._last_connection_check
        if time_since_last_check < CONNECTION_CHECK_INTERVAL:
            self.log.debug(
                f'[CHECK_CONNECTION] Skipping check, last check was {time_since_last_check:.1f}s ago'
            )
            return not self._connection_lost

        self.log.debug('[CHECK_CONNECTION] Performing connection health check...')

        try:
            if self.ftp:
                self.log.debug('[CHECK_CONNECTION] Testing connection with getcwd()')
                # Try a simple operation to test connection
                current_dir = self.ftp.getcwd()
                self.log.debug(
                    f'[CHECK_CONNECTION] Connection test successful, current dir: {current_dir}'
                )
                self._connection_lost = False
                self._last_connection_check = current_time
                return True
            else:
                self.log.debug('[CHECK_CONNECTION] No FTP connection object exists')
                self._connection_lost = True
        except Exception as e:
            self.log.warning(f'[CHECK_CONNECTION] Connection health check failed: {e}')
            self._connection_lost = True

        return False

    def _reconnect(self) -> None:
        """Attempt to reconnect to the FTP server."""
        self.log.info(f'[RECONNECT] Attempting to reconnect to {self.server}...')

        # Close existing connection if any
        if self.ftp:
            self.log.debug('[RECONNECT] Closing existing FTP connection')
            try:
                self.ftp.close()
                self.log.debug('[RECONNECT] Existing connection closed successfully')
            except Exception as e:  # noqa: S110
                self.log.debug(f'[RECONNECT] Error closing existing connection (ignored): {e}')
            finally:
                self.ftp = None
                self.log.debug('[RECONNECT] FTP connection object set to None')

        # Attempt fresh login
        self.log.debug('[RECONNECT] Attempting fresh login')
        self.login()
        self.log.debug('[RECONNECT] Reconnection completed successfully')

    def _ensure_binary_mode(self) -> None:
        """Ensure the connection is in binary mode.

        This is important because some FTP commands (like retrlines for LIST)
        automatically switch to ASCII mode and don't switch back.
        """
        try:
            if self.ftp and hasattr(self.ftp, '_session'):
                self.log.debug('[ENSURE_BINARY] Setting binary transfer mode')
                self.ftp._session.voidcmd('TYPE I')  # TYPE I = binary mode
                self.log.debug('[ENSURE_BINARY] Binary mode confirmed')
        except (ftputil.error.FTPError, OSError) as e:
            self.log.warning(f'[ENSURE_BINARY] Could not ensure binary mode: {e}')

    @retry_on_connection_error()
    def cwd(self, target_dir: str) -> None:
        """Change the working directory on the server.

        Always changes to the current year subdirectory from target_dir.

        Args:
            target_dir (str): directory to change to.
        """
        self.log.debug(f'[CWD] Changing to directory: {target_dir}')
        target_dir_checked = re.sub('[^0-9a-zA-Z_]+', '_', target_dir)
        if target_dir_checked != target_dir:
            self.log.warning(
                f'[CWD] Invalid characters in directory name. Replaced [{target_dir}] '
                f'with [{target_dir_checked}].'
            )

        try:
            current_dir = self.ftp.getcwd()
            self.log.debug(f'[CWD] Current directory before change: {current_dir}')

            self.log.debug(f'[CWD] Preparing/changing to main directory: {target_dir_checked}')
            self._prep_dir(target_dir_checked)
            self.ftp.chdir(target_dir_checked)

            year_str = current_year_str()
            self.log.debug(f'[CWD] Preparing/changing to year subdirectory: {year_str}')
            self._prep_dir(year_str)
            self.ftp.chdir(year_str)

            final_dir = self.ftp.getcwd()
            self.log.debug(f'[CWD] Successfully changed to final directory: {final_dir}')

        except (ftputil.error.FTPError, OSError) as e:
            self.log.error(f'[CWD] Could not change directory to [{target_dir}]: {e}')
            raise FTPError from e

    def _prep_dir(self, dir_name: str) -> None:
        """Check if subdirectory exists in the current working directory. If not, create.

        Args:
            dir_name (str): subdirectory to check/create
        """
        self.log.debug(f'[PREP_DIR] Checking if directory exists: {dir_name}')
        try:
            # Try to change to the directory - if it works, it exists
            current_dir = self.ftp.getcwd()
            self.ftp.chdir(dir_name)
            self.ftp.chdir(current_dir)  # Change back
            self.log.debug(f'[PREP_DIR] Directory [{dir_name}] already exists')
        except (ftputil.error.FTPError, OSError):
            # Directory doesn't exist, create it
            self.log.debug(f'[PREP_DIR] Directory [{dir_name}] does not exist, creating...')
            self.ftp.mkdir(dir_name)
            self.log.debug(f'[PREP_DIR] Successfully created directory: {dir_name}')

    def _temp_cwd(self, target_dir: Union[str, None]) -> Union[str, None]:
        """Temporarily change the working directory.

        If target_dir, get the current working directory, change to target,
            then return the initial working directory.

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
        return self.ftp.getcwd()

    @retry_on_connection_error()
    def get_contents(self, directory='.') -> List[List[str]]:
        """Return files and subdirectories of directory on server.

        Args:
            directory (str, optional): path to directory to get contents of.
                Defaults to '.' (current working directory)

        Returns:
            List[List[str]]: List of two lists,
                first containing all directories
                second containing all files.
                Both are empty if there are no files/directories
        """
        ret: List[List[str]] = [[], []]

        try:
            # Use nlst() which is more reliable than listdir() + path.isdir()
            # Parse the full directory listing to avoid hanging path operations
            current_dir = self.ftp.getcwd()
            if directory != '.' and directory != current_dir:
                self.ftp.chdir(directory)

            try:
                # Get detailed listing
                lines = []
                self.ftp._session.retrlines('LIST', lines.append)

                # Restore binary mode after retrlines (which switches to ASCII)
                self._ensure_binary_mode()

                for line in lines:
                    # Parse LIST output - first character indicates type
                    if line.startswith('d'):  # Directory
                        # Extract filename from end of line
                        parts = line.split()
                        if len(parts) >= 9:
                            filename = ' '.join(parts[8:])  # Handle filenames with spaces
                            ret[0].append(filename)
                    elif line.startswith('-'):  # Regular file
                        parts = line.split()
                        if len(parts) >= 9:
                            filename = ' '.join(parts[8:])
                            ret[1].append(filename)
            finally:
                # Return to original directory if we changed it
                if directory != '.' and directory != current_dir:
                    self.ftp.chdir(current_dir)

            self.log.debug(
                f'Directory scan complete. Found {len(ret[0])} dirs, {len(ret[1])} files'
            )

        except (ftputil.error.FTPError, OSError) as e:
            self.log.error(f'Failed to get directory contents for {directory}: {e}')
            raise

        return ret

    def _speed_limited_upload(self, local_file: str, remote_file: str) -> None:
        """Upload file with speed limiting and custom chunk size.

        Args:
            local_file: Path to local file to upload
            remote_file: Target filename on remote server
        """
        self.log.debug(f'[SPEED_LIMITED_UPLOAD] Starting upload from {local_file} to {remote_file}')
        chunk_size = self.upload_chunk_size
        speed_limit_bytes_per_sec = (
            self.upload_speed_limit_kbps * 1024 if self.upload_speed_limit_kbps else None
        )

        self.log.debug(
            f'[SPEED_LIMITED_UPLOAD] Parameters: chunk_size={chunk_size}, '
            f'speed_limit={self.upload_speed_limit_kbps} kB/s'
        )

        self.log.debug(f'[SPEED_LIMITED_UPLOAD] Opening local file: {local_file}')
        with open(local_file, 'rb') as local_fp:
            self.log.debug(f'[SPEED_LIMITED_UPLOAD] Opening remote file for writing: {remote_file}')
            with self.ftp.open(remote_file, 'wb') as remote_fp:
                bytes_transferred = 0
                start_time = time.time()
                chunk_count = 0

                self.log.debug('[SPEED_LIMITED_UPLOAD] Starting chunk transfer loop')
                while True:
                    chunk_start_time = time.time()
                    # self.log.debug(
                    #     f'[SPEED_LIMITED_UPLOAD] Reading chunk {chunk_count + 1}, size {chunk_size}'
                    # )
                    chunk = local_fp.read(chunk_size)

                    if not chunk:
                        self.log.debug('[SPEED_LIMITED_UPLOAD] End of file reached, breaking loop')
                        break

                    chunk_count += 1
                    # self.log.debug(
                    #     f'[SPEED_LIMITED_UPLOAD] Writing chunk {chunk_count}, size {len(chunk)} bytes'
                    # )
                    remote_fp.write(chunk)
                    bytes_transferred += len(chunk)

                    # Apply speed limiting if configured
                    if speed_limit_bytes_per_sec:
                        chunk_transfer_time = time.time() - chunk_start_time
                        expected_time = len(chunk) / speed_limit_bytes_per_sec

                        if chunk_transfer_time < expected_time:
                            sleep_time = expected_time - chunk_transfer_time
                            # self.log.debug(
                            #     f'[SPEED_LIMITED_UPLOAD] Speed limiting: sleeping {sleep_time:.3f}s'
                            # )
                            time.sleep(sleep_time)

                    if chunk_count % 100 == 0:  # Log progress every 100 chunks
                        self.log.debug(
                            f'[SPEED_LIMITED_UPLOAD] Progress: {chunk_count} chunks, {format_file_size(bytes_transferred)} transferred'
                        )

            total_time = time.time() - start_time
            if total_time > 0:
                actual_speed_kbps = (bytes_transferred / 1024) / total_time
                self.log.debug(
                    f'[SPEED_LIMITED_UPLOAD] Upload completed. Chunks: {chunk_count}, '
                    f'Transferred: {format_file_size(bytes_transferred)}, '
                    f'Time: {total_time:.1f}s, Avg speed: {actual_speed_kbps:.1f} kB/s'
                )

    @retry_on_connection_error()
    def upload_file(
        self,
        file: str,
        # target_dir: Union[None, str] = '.',
        overwrite: bool = True,
        target_filename: Union[str, None] = None,
    ) -> None:
        """Upload file from local system to server with atomic operations, speed limiting and verification.

        File is uploaded atomically using a temporary filename "uploading.now", then renamed to the
        final target filename once upload is complete and verified. This prevents partial files
        from being visible on the server during upload.

        If overwrite is set to False, first check if file exists on remote.
        If file exists, raise FileExistsOnServer and exit.

        Upload speed and chunk size are controlled by instance parameters:
        - upload_speed_limit_kbps: Maximum upload speed in kB/s (None = unlimited)
        - upload_chunk_size: Size of each chunk transferred (default 8192 bytes)

        Args:
            file (str): path to file to be uploaded
            target_dir (Union[None, str]): remote target directory for the upload.
                                    Defaults to None (current working directory)
            overwrite (bool, optional): Silently overwrite file if it exists on server.
                                            Defaults to True.
            target_filename (Union[str, None], optional): Target filename on server.
                                            Defaults to None (use original filename).

        Raises:
            ValueError: source file does not exist on local system.
            FileExistsOnServer: target file exists on server and overwrite == False
            UploadFailed: issue during upload, verification, or atomic rename operation
        """
        self.log.debug(f'[UPLOAD_FILE] Starting upload for file: {file}')
        if not os.path.isfile(file):
            msg = f'File {file} does not exist.'
            self.log.error(f'[UPLOAD_FILE] {msg}')
            raise ValueError(msg)

        # initial_dir = self._temp_cwd(target_dir)
        if not target_filename:
            target_filename = os.path.basename(file)
        self.log.debug(f'[UPLOAD_FILE] Target filename: {target_filename}, overwrite: {overwrite}')

        if not overwrite:
            self.log.debug('[UPLOAD_FILE] Checking if file exists on server (overwrite=False)')
            if self._file_exists(target_filename):
                self.log.warning(
                    f'[UPLOAD_FILE] File {target_filename} already exists and overwrite=False'
                )
                raise FTPFileExistsOnServer

        # Enhanced upload with progress tracking and verification
        # Use atomic upload: upload to temporary name, then rename to final name
        temp_filename = 'uploading.now'
        self.log.debug(
            f'[UPLOAD_FILE] Using atomic upload with temporary filename: {temp_filename}'
        )

        try:
            # Get local file size for verification and progress tracking
            self.log.debug(f'[UPLOAD_FILE] Getting local file size for {file}')
            local_size = os.path.getsize(file)
            self.log.debug(f'[UPLOAD_FILE] Local file size: {format_file_size(local_size)}')

            # For unstable connections, use multiple verification steps
            upload_attempts = 0
            max_upload_attempts = 2  # Allow one retry for upload itself
            self.log.debug(
                f'[UPLOAD_FILE] Starting upload loop, max attempts: {max_upload_attempts}'
            )

            while upload_attempts < max_upload_attempts:
                upload_attempts += 1
                self.log.debug(
                    f'[UPLOAD_FILE] === Upload attempt {upload_attempts}/{max_upload_attempts} ==='
                )

                try:
                    # Clean up any existing temporary file first
                    self.log.debug(
                        f'[UPLOAD_FILE] Checking for existing temp file: {temp_filename}'
                    )
                    if self._file_exists(temp_filename):
                        self.log.debug(
                            f'[UPLOAD_FILE] Removing existing temp file: {temp_filename}'
                        )
                        self.ftp.remove(temp_filename)

                    # Perform the upload with speed limiting to temporary filename
                    self.log.debug(
                        f'[UPLOAD_FILE] Starting speed-limited upload to {temp_filename}'
                    )
                    self._speed_limited_upload(file, temp_filename)
                    self.log.debug(
                        f'[UPLOAD_FILE] Upload command completed for temporary file {temp_filename}'
                    )

                    # Size verification with retry on temporary file
                    remote_size = None
                    for size_check_attempt in range(3):  # Try up to 3 times to get size
                        try:
                            remote_size = self.get_size(temp_filename)
                            if remote_size is not None:
                                break
                        except Exception as size_error:
                            if size_check_attempt == 2:  # Last attempt
                                self.log.warning(
                                    f'Could not verify file size after upload: {size_error}'
                                )
                            else:
                                time.sleep(1)  # Brief pause between size check attempts

                    # Verify file integrity before rename
                    if remote_size is not None:
                        if remote_size == local_size:
                            self.log.debug(
                                f'Upload to temporary file successful and verified. '
                                f'Size: {format_file_size(remote_size)}'
                            )

                            # Now perform atomic rename to final filename
                            try:
                                # Remove target file if it exists (for overwrite)
                                if self._file_exists(target_filename):
                                    self.ftp.remove(target_filename)

                                # Rename temporary file to final name
                                self.log.debug(f'Renaming {temp_filename} to {target_filename}')
                                self.ftp.rename(temp_filename, target_filename)

                                # Success! Exit the retry loop
                                self.log.debug(
                                    f'Atomic upload completed successfully: {target_filename}'
                                )
                                return

                            except (ftputil.error.FTPError, OSError) as rename_error:
                                # Clean up temporary file on rename failure
                                try:
                                    if self._file_exists(temp_filename):
                                        self.ftp.remove(temp_filename)
                                        self.log.debug(
                                            f'Cleaned up temporary file after rename failure'
                                        )
                                except Exception:
                                    pass  # Don't fail on cleanup errors
                                raise FTPUploadFailed(
                                    f'Failed to rename {temp_filename} to {target_filename}: {rename_error}'
                                )
                        else:
                            error_msg = (
                                f'Size mismatch - Local: {format_file_size(local_size)}, '
                                f'Remote: {format_file_size(remote_size)}'
                            )
                            self.log.warning(error_msg)
                            # Clean up temporary file before retry
                            try:
                                if self._file_exists(temp_filename):
                                    self.ftp.remove(temp_filename)
                            except Exception:
                                pass

                            if upload_attempts < max_upload_attempts:
                                self.log.info('Retrying upload due to size mismatch...')
                                continue
                            else:
                                raise FTPUploadFailed(error_msg)

                except (ftputil.error.FTPError, OSError, socket.error) as upload_error:
                    # Clean up temporary file on upload error
                    try:
                        if self._file_exists(temp_filename):
                            self.ftp.remove(temp_filename)
                            self.log.debug('Cleaned up temporary file after upload error')
                    except Exception:
                        pass  # Don't fail on cleanup errors

                    if upload_attempts < max_upload_attempts:
                        self.log.warning(
                            f'Upload attempt {upload_attempts} failed: {upload_error}. Retrying...'
                        )
                        time.sleep(2)  # Brief pause before retry
                        continue
                    else:
                        raise FTPUploadFailed(
                            f'Upload failed after {max_upload_attempts} attempts: {upload_error}'
                        ) from upload_error

        except FTPUploadFailed:
            # Clean up temporary file on any FTP upload failure
            try:
                if self._file_exists(temp_filename):
                    self.ftp.remove(temp_filename)
            except Exception:
                pass  # Don't fail on cleanup errors
            raise  # Re-raise FTP upload failures
        except Exception as e:
            self.log.error(f'Unexpected error during upload: {e}')
            raise FTPUploadFailed('Unexpected upload error') from e

        # self._temp_cwd(initial_dir)

    @retry_on_connection_error()
    def _file_exists(self, file: str) -> bool:
        """Check if file exists in current directory.

        Args:
            file (str): file to be checked

        Returns:
            bool: True if file exists, False otherwise
        """
        self.log.debug(f'[FILE_EXISTS] Checking if file exists: {file}')
        try:
            # Use ftputil's listdir() method to get file list
            files = self.ftp.listdir('.')
            exists = file in files
            self.log.debug(f'[FILE_EXISTS] File {file} exists: {exists}')
        except (ftputil.error.FTPError, OSError) as e:
            self.log.debug(f'[FILE_EXISTS] Error checking file existence: {e}')
            # Fallback: try using the underlying session's nlst command
            try:
                self.log.debug(f'[FILE_EXISTS] Trying fallback method with _session.nlst()')
                files = self.ftp._session.nlst()
                exists = file in files
                self.log.debug(f'[FILE_EXISTS] File {file} exists (via fallback): {exists}')
            except (ftputil.error.FTPError, OSError) as fallback_error:
                self.log.debug(f'[FILE_EXISTS] Fallback also failed: {fallback_error}')
                exists = False
                self.log.debug(f'[FILE_EXISTS] File {file} exists: {exists}')
        return exists

    @retry_on_connection_error()
    def get_size(self, file: str) -> Union[int, None]:
        """Get size of file on server.

        Args:
            file (str): filename to get size of

        Returns:
            Union[int, None]: file size in bytes or None if not successful.
        """
        self.log.debug(f'[GET_SIZE] Getting size for file: {file}')
        try:
            # Use LIST command directly to avoid SIZE command issues in ASCII mode
            self.log.debug(f'[GET_SIZE] Using LIST command for file: {file}')
            lines = []
            self.ftp._session.retrlines(f'LIST {file}', lines.append)

            # Restore binary mode after retrlines (which switches to ASCII)
            self._ensure_binary_mode()

            if lines:
                # Parse the LIST line to extract file size
                parts = lines[0].split()
                if len(parts) >= 5 and parts[0].startswith('-'):  # Regular file
                    size = int(parts[4])  # Size is typically the 5th field
                    self.log.debug(
                        f'[GET_SIZE] File {file} size via LIST: {format_file_size(size)}'
                    )
                    return size

            self.log.debug(f'[GET_SIZE] Could not parse file size from LIST output')
            return None
        except (ftputil.error.FTPError, OSError) as e:
            self.log.debug(f'[GET_SIZE] Could not get size of {file}: {e}')
            return None

    def quit(self) -> None:
        """Send a QUIT command to the server and close the connection.

        This is the "polite" way to close a connection, but it may raise an exception
        if the server responds with an error to the QUIT command.
        This renders the FTP instance useless for subsequent calls.
        """
        self.log.debug('[QUIT] Starting FTP connection close process')
        if self.ftp:
            self.log.debug(f'[QUIT] Closing FTP connection to {self.server}')
            try:
                self.ftp.close()
                self.log.debug('[QUIT] FTP connection closed successfully')
            except Exception as e:
                self.log.warning(f'[QUIT] Exception occurred while closing FTP connection: {e}')
            finally:
                self.ftp = None
                self.log.debug('[QUIT] FTP connection object set to None')
        else:
            self.log.debug('[QUIT] No FTP connection to close')


def enable_debug_logging():
    """Enable maximum debug logging for troubleshooting FTP operations.

    This function sets up detailed logging to help troubleshoot FTP connection
    and operation issues. Call this before creating pFTP instances.
    """
    # Also enable ftputil's internal logging if available
    ftputil_logger = logging.getLogger('ftputil')
    ftputil_logger.setLevel(logging.DEBUG)
    ftputil_logger.addHandler(logging.StreamHandler())

    print('Debug logging enabled for FTP operations')


enable_debug_logging()


def test_connection_stability(server: str, user: str, pw: str, test_duration: int = 60) -> dict:
    """Test FTP connection stability over time.

    Args:
        server: FTP server address
        user: Username
        pw: Password
        test_duration: Test duration in seconds

    Returns:
        dict: Test results with connection statistics
    """
    print(f'Testing connection stability to {server} for {test_duration} seconds...')

    results = {
        'total_attempts': 0,
        'successful_connections': 0,
        'failed_connections': 0,
        'connection_errors': [],
        'average_response_time': 0,
        'test_duration': test_duration,
    }

    start_time = time.time()
    response_times = []

    while time.time() - start_time < test_duration:
        results['total_attempts'] += 1

        try:
            attempt_start = time.time()

            # Test basic connection
            with pFTP(server, user, pw, timeout=10) as ftp:
                ftp.pwd()  # Simple operation to test connection

            response_time = time.time() - attempt_start
            response_times.append(response_time)
            results['successful_connections'] += 1

            print(f'✓ Connection {results["total_attempts"]}: {response_time:.2f}s')

        except Exception as e:
            results['failed_connections'] += 1
            results['connection_errors'].append(str(e))
            print(f'✗ Connection {results["total_attempts"]}: {e}')

        time.sleep(5)  # Wait between tests

    if response_times:
        results['average_response_time'] = sum(response_times) / len(response_times)

    # Print summary
    success_rate = (results['successful_connections'] / results['total_attempts']) * 100
    print(f'\n--- Connection Stability Test Results ---')
    print(
        f'Success Rate: {success_rate:.1f}% ({results["successful_connections"]}/'
        f'{results["total_attempts"]})'
    )
    print(f'Average Response Time: {results["average_response_time"]:.2f}s')

    if results['connection_errors']:
        print(f'Common Errors: {set(results["connection_errors"])}')

    return results
