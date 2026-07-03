from dataclasses import dataclass, field
import ftplib
from pathlib import Path, PosixPath
from typing import Iterable, Optional, List
from urllib.parse import urlparse

import ftputil

from snakemake_interface_storage_plugins.settings import StorageProviderSettingsBase
from snakemake_interface_storage_plugins.storage_provider import (  # noqa: F401
    StorageProviderBase,
    StorageQueryValidationResult,
    ExampleQuery,
    Operation,
    QueryType,
)
from snakemake_interface_storage_plugins.storage_object import (
    StorageObjectRead,
    StorageObjectWrite,
    StorageObjectGlob,
    retry_decorator,
)
from snakemake_interface_storage_plugins.io import (
    IOCacheStorageInterface,
    get_constant_prefix,
)


@dataclass
class StorageProviderSettings(StorageProviderSettingsBase):
    username: Optional[str] = field(
        default=None,
        metadata={
            "help": "FTP username",
            "env_var": True,
            "required": False,
        },
    )
    password: Optional[str] = field(
        default=None,
        metadata={
            "help": "FTP password",
            "env_var": True,
            "required": False,
        },
    )
    active_mode: bool = field(
        default=False,
        metadata={
            "help": "Use active mode instead of passive mode",
            "env_var": True,
            "required": False,
        },
    )


class StorageProvider(StorageProviderBase):
    # For compatibility with future changes, you should not overwrite the __init__
    # method. Instead, use __post_init__ to set additional attributes and initialize
    # futher stuff.

    def __post_init__(self):
        # This is optional and can be removed if not needed.
        # Alternatively, you can e.g. prepare a connection to your storage backend here.
        # and set additional attributes.
        self.conn_pool = dict()

    @classmethod
    def example_queries(cls) -> List[ExampleQuery]:
        """Return an example query with description for this storage provider."""
        return [
            ExampleQuery(
                query="ftp://ftpserver.com:21/myfile.txt",
                type=QueryType.ANY,
                description="A file on an ftp server. "
                "The port is optional and defaults to 21.",
            ),
            ExampleQuery(
                query="ftps://ftpserver.com:21/myfile.txt",
                type=QueryType.ANY,
                description="A file on an ftp server (using encrypted transport). "
                "The port is optional and defaults to 21.",
            ),
        ]

    def use_rate_limiter(self) -> bool:
        """Return False if no rate limiting is needed for this provider."""
        return True

    def default_max_requests_per_second(self) -> float:
        """Return the default maximum number of requests per second for this storage
        provider."""
        return 10.0

    def rate_limiter_key(self, query: str, operation: Operation):
        """Return a key for identifying a rate limiter given a query and an operation.

        This is used to identify a rate limiter for the query.
        E.g. for a storage provider like http that would be the host name.
        For s3 it might be just the endpoint URL.
        """
        parsed = urlparse(query)
        return parsed.netloc

    @classmethod
    def is_valid_query(cls, query: str) -> StorageQueryValidationResult:
        """Return whether the given query is valid for this storage provider."""
        # Ensure that also queries containing wildcards (e.g. {sample}) are accepted
        # and considered valid. The wildcards will be resolved before the storage
        # object is actually used.
        parsed = urlparse(query)
        if (parsed.scheme == "ftp" or parsed.scheme == "ftps") and parsed.path:
            return StorageQueryValidationResult(valid=True, query=query)
        else:
            return StorageQueryValidationResult(
                valid=False,
                query=query,
                reason="Query does not start with ftp:// or ftps:// or does not "
                "contain a path to a file or directory.",
            )

    def get_conn(self, hostname: str, port: Optional[int] = 22, protocol: str = "ftp"):
        key = hostname, port, protocol
        if key not in self.conn_pool:
            cls = ftplib.FTP_TLS if protocol == "ftps" else ftplib.FTP
            session_factory = ftputil.session.session_factory(
                base_class=cls,
                port=port,
                use_passive_mode=not self.settings.active_mode,
                encrypt_data_channel=True,
            )
            conn = ftputil.FTPHost(
                hostname,
                self.settings.username,
                self.settings.password,
                session_factory=session_factory,
            )
            self.conn_pool[key] = conn
            return conn
        return self.conn_pool[key]


# Required:
# Implementation of storage object. If certain methods cannot be supported by your
# storage (e.g. because it is read-only see
# snakemake-storage-http for comparison), remove the corresponding base classes
# from the list of inherited items.
class StorageObject(StorageObjectRead, StorageObjectWrite, StorageObjectGlob):
    # For compatibility with future changes, you should not overwrite the __init__
    # method. Instead, use __post_init__ to set additional attributes and initialize
    # futher stuff.

    def __post_init__(self):
        # This is optional and can be removed if not needed.
        # Alternatively, you can e.g. prepare a connection to your storage backend here.
        # and set additional attributes.
        self.parsed_query = urlparse(self.query)
        self.conn = self.provider.get_conn(
            self.parsed_query.hostname,
            self.parsed_query.port or 21,
            self.parsed_query.scheme,
        )

    async def inventory(self, cache: IOCacheStorageInterface):
        """From this file, try to find as much existence and modification date
        information as possible. Only retrieve that information that comes for free
        given the current object.
        """
        # This is optional and can be left as is

        # If this is implemented in a storage object, results have to be stored in
        # the given IOCache object, using self.cache_key() as key.
        # Optionally, this can take a custom local suffix, needed e.g. when you want
        # to cache more items than the current query: self.cache_key(local_suffix=...)
        pass

    def get_inventory_parent(self) -> Optional[str]:
        """Return the parent directory of this object."""
        # this is optional and can be left as is
        return None

    def local_suffix(self) -> str:
        """Return a unique suffix for the local path, determined from self.query."""
        return f"{self.parsed_query.netloc}/{self.parsed_query.path}"

    def cleanup(self):
        """Perform local cleanup of any remainders of the storage object."""
        # self.local_path() should not be removed, as this is taken care of by
        # Snakemake.
        pass

    # Fallible methods should implement some retry logic.
    # The easiest way to do this (but not the only one) is to use the retry_decorator
    # provided by snakemake-interface-storage-plugins.
    @retry_decorator
    def exists(self) -> bool:
        # return True if the object exists
        return self.conn.path.exists(self.parsed_query.path)

    @retry_decorator
    def mtime(self) -> float:
        # return the modification time
        return self.conn.path.getmtime(self.parsed_query.path)

    @retry_decorator
    def size(self) -> int:
        # return the size in bytes
        return self.conn.path.getsize(self.parsed_query.path)

    @retry_decorator
    def retrieve_object(self):
        # Ensure that the object is accessible locally under self.local_path()
        def download(path: PosixPath, local_path: Path):
            if self.conn.path.isdir(path):
                local_path.mkdir(exist_ok=True, parents=True)
                for name in self.conn.listdir(path):
                    p = path / name
                    download(p, local_path / name)
            else:
                self.conn.download(path, local_path)

        download(PosixPath(self.parsed_query.path), self.local_path())

    # The following to methods are only required if the class inherits from
    # StorageObjectReadWrite.

    @retry_decorator
    def store_object(self):
        # Ensure that the object is stored at the location specified by
        # self.local_path().
        parent = PosixPath(self.parsed_query.path).parent
        if str(parent) != ".":
            self.conn.makedirs(parent, exist_ok=True)
        self.conn.upload(self.local_path(), self.parsed_query.path)

    @retry_decorator
    def remove(self):
        # Remove the object from the storage.
        if self.conn.path.isdir(self.parsed_query.path):
            self.conn.rmtree(self.parsed_query.path)
        else:
            self.conn.remove(self.parsed_query.path)

    # The following to methods are only required if the class inherits from
    # StorageObjectGlob.

    @retry_decorator
    def list_candidate_matches(self) -> Iterable[str]:
        """Return a list of candidate matches in the storage for the query."""
        # This is used by glob_wildcards() to find matches for wildcards in the query.
        # The method has to return concretized queries without any remaining wildcards.
        # Use snakemake_executor_plugins.io.get_constant_prefix(self.query) to get the
        # prefix of the query before the first wildcard.
        prefix = get_constant_prefix(self.query, strip_incomplete_parts=True)
        items = []
        if self.conn.path.isdir(prefix):
            for dirpath, dirnames, filenames in self.conn.walk(prefix):
                dirpath = PosixPath(dirpath)
                for d in dirnames:
                    if not self.conn.listdir(str(prefix / d)):
                        items.append(str(prefix / d))
                for f in filenames:
                    items.append(str(prefix / f))
        elif self.conn.path.exists(prefix):
            items.append(prefix)
        return items
