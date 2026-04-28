"""Helpers related to sending/receiving HTTP requests."""

import datetime
import json
import logging
import ssl
import typing

from typing import Optional
from urllib.parse import quote

import urllib3

from pydantic import SecretStr

from snowflake.core.exceptions import SnowflakePythonError

from ._common import TokenType
from ._constants import SESSION_TOKEN_EXPIRED_ERROR_CODE


if typing.TYPE_CHECKING:
    import snowflake.core

logger = logging.getLogger(__name__)

PrimitiveTypes = typing.TypeVar('PrimitiveTypes', float, bool, bytes, bytearray, str, int)
PRIMITIVE_TYPES = (float, bool, bytes, bytearray, str, int)
NATIVE_TYPES_MAPPING = {
    'int': int,
    'long': int,  # TODO remove as only py3 is supported?
    'float': float,
    'str': str,
    'bool': bool,
    'date': datetime.date,
    'datetime': datetime.datetime,
    'object': object,
    'bytes': bytes,
    'bytearray': bytearray,
}
DEFAULT_RETRY_TIMEOUT_SECONDS = 600.0  # default 10 minutes for query retries
STATUS_CODES_MAPPING = {
    200: "OK",
    202: "Long Running Query",
    429: "Rate Limited",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


def resolve_url(
    resource_path: str,
    path_params: dict[str, str],
    collection_formats: dict[typing.Any, typing.Any],
    safe_quoting: str,
) -> str:
    if path_params:
        path_params = sanitize_for_serialization(path_params)
        path_params_list = parameters_to_tuples(
            path_params,
            collection_formats,
        )
        resource_path = resource_path.format(
            **{
                k: quote(str(v), safe=safe_quoting)
                for k, v in path_params_list
            }
        )
    return resource_path

class ToDictProto(typing.Protocol):
    def to_dict(self) -> dict[typing.Any, typing.Any]:
        ...

@typing.overload
def sanitize_for_serialization(obj: None) -> None:
    ...
@typing.overload
def sanitize_for_serialization(obj: PrimitiveTypes) -> PrimitiveTypes:
    ...
@typing.overload
def sanitize_for_serialization(obj: list[typing.Any]) -> list[typing.Any]:
    ...
@typing.overload
def sanitize_for_serialization(obj: tuple[typing.Any, ...]) -> tuple[typing.Any, ...]:
    ...
@typing.overload
def sanitize_for_serialization(
    obj: typing.Union[
        datetime.datetime,
        datetime.date,
    ]) -> str:
    ...
@typing.overload
def sanitize_for_serialization(obj: dict[typing.Any, typing.Any]) -> dict[typing.Any, typing.Any]:
    ...
@typing.overload
def sanitize_for_serialization(obj: ToDictProto) -> dict[typing.Any, typing.Any]:
    ...
def sanitize_for_serialization(
    obj: typing.Union[
        None,
        float,
        bool,
        bytes,
        str,
        int,
        list[typing.Any],
        tuple[typing.Any, ...],
        datetime.datetime,
        datetime.date,
        dict[typing.Any, typing.Any],
        ToDictProto,
    ],
) -> typing.Union[
    None,
    float,
    bool,
    bytes,
    str,
    int,
    list[typing.Any],
    tuple[typing.Any, ...],
    dict[typing.Any, typing.Any],
]:
    """Build a JSON POST object.

    If obj is None, return None.
    If obj is str, int, long, float, bool, return directly.
    If obj is datetime.datetime, datetime.date
        convert to string in iso8601 format.
    If obj is list, sanitize each element in the list.
    If obj is dict, return the dict.
    If obj is OpenAPI model, return the properties dict.

    :param obj: The data to serialize.
    :return: The serialized form of data.
    """
    if obj is None:
        return None
    elif isinstance(obj, PRIMITIVE_TYPES):
        return obj
    elif isinstance(obj, SecretStr):
        return obj.get_secret_value()
    elif isinstance(obj, list):
        return [
            sanitize_for_serialization(sub_obj) for sub_obj in obj
        ]
    elif isinstance(obj, tuple):
        return tuple(
            sanitize_for_serialization(sub_obj) for sub_obj in obj
        )
    elif isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()

    if isinstance(obj, dict):
        obj_dict = obj
    else:
        # Convert model obj to dict except
        # attributes `openapi_types`, `attribute_map`
        # and attributes which value is not None.
        # Convert attribute name to json key in
        # model definition for request.
        # Prefer to use `to_dict_without_readonly_properties` if the model has
        if hasattr(obj, 'to_dict_without_readonly_properties'):
            obj_dict = obj.to_dict_without_readonly_properties()
        else:
            obj_dict = obj.to_dict()

    return {
        key: sanitize_for_serialization(val)
        for key, val in obj_dict.items()
    }

def parameters_to_tuples(
    params: typing.Union[
        dict[typing.Any, typing.Any],
        list[tuple[typing.Any, typing.Any]]
    ],
    collection_formats: typing.Optional[dict[typing.Any, typing.Any]],
) -> list[tuple[typing.Any, typing.Any]]:
    """Get parameters as list of tuples, formatting collections.

    :param params: Parameters as dict or list of two-tuples
    :param dict collection_formats: Parameter collection formats
    :return: Parameters as list of tuples, collections formatted
    """
    new_params: list[tuple[typing.Any, typing.Any]] = []
    if collection_formats is None:
        collection_formats = {}
    for k, v in params.items() if isinstance(params, dict) else params:
        if k in collection_formats:
            collection_format = collection_formats[k]
            if collection_format == 'multi':
                new_params.extend((k, value) for value in v)
            else:
                if collection_format == 'ssv':
                    delimiter = ' '
                elif collection_format == 'tsv':
                    delimiter = '\t'
                elif collection_format == 'pipes':
                    delimiter = '|'
                else:  # csv is the default
                    delimiter = ','
                new_params.append(
                    (k, delimiter.join(str(value) for value in v)))
        else:
            new_params.append((k, v))
    return new_params

class SFPoolManager(urllib3.PoolManager):
    # Having this typed is non-trivial across multiple
    #  urllib3 major versions
    def request(  # type: ignore[no-untyped-def,override]
        self,
        root: "snowflake.core.Root",
        method: str,
        url: str,
        fields=None,
        headers: typing.Optional[dict[str, str]] = None,
        **urlopen_kw: typing.Any,
    ):
        if headers is None:
            headers = dict()
        need_auth = url_needs_auth(url)
        if need_auth:
            if root._session_token is None:
                # This should never trigger
                raise Exception("session token is missing while making a request")
            headers.update(get_session_headers(root.token_type, root._session_token, root.external_session_id))
        logger.debug("making an http %s call to '%s'", method.upper(), url)
        try:
            r = super().request(
                method=method,
                url=url,
                fields=fields,
                headers=headers,
                **urlopen_kw
            )
        except urllib3.exceptions.MaxRetryError as e:
            if (
                isinstance(e.reason, urllib3.exceptions.SSLError)
                and isinstance(e.reason.args[0], ssl.SSLCertVerificationError)
                and "Hostname mismatch" in e.reason.args[0].verify_message
            ):
                raise SnowflakePythonError(
                    "This SSL error occurs when the hostname contains underscores, please see "
                    "https://docs.snowflake.com/en/user-guide/organizations-connect for how to set "
                    "the hostname correctly."
                ) from e
            raise

        try:
            content_type = r.headers.get('Content-Type')
            if content_type in {'text/event-stream', 'application/octet-stream'}:
                resp_json = dict()
            else:
                resp_json = json.loads(r.data)
        except Exception:
            resp_json = dict()
        if (
            need_auth
            and isinstance(resp_json, dict)
            and resp_json.get("error_code") == SESSION_TOKEN_EXPIRED_ERROR_CODE
            and hasattr(root, "_connection")
            and root._connection.rest is not None
        ):
            # Try renewing session token and try request again
            logger.debug("session expired, renewing session")
            root._connection.rest._renew_session()
            if need_auth:
                if root._session_token is None:
                    # This should never trigger
                    raise Exception("session token is missing right after renewal")
                headers.update(get_session_headers(root.token_type, root._session_token, root.external_session_id))
            logger.debug(
                "repeating an http with new session token %s call to '%s'",
                method.upper(),
                url,
            )
            r = super().request(
                method=method,
                url=url,
                fields=fields,
                headers=headers,
                **urlopen_kw
            )
        return r

# Use a connection pool singleton for every resource
CONNECTION_POOL: typing.Optional[SFPoolManager] = None


def get_session_headers(token_type: TokenType, session_token: str, external_session_id: Optional[str] = None) -> dict[
    str, str]:
    if token_type is TokenType.EXTERNAL_SESSION_WITH_PAT:
        return {
            "Authorization": f"Bearer {session_token}",
            "X-Snowflake-External-Session-ID": external_session_id or "",
            "X-Snowflake-Authorization-Token-Type": "PAT_WITH_EXTERNAL_SESSION_ID",
        }
    return {"Authorization": f"Snowflake Token=\"{session_token}\""}


def url_needs_auth(url: str) -> bool:
    """Whether a URL needs the authentication headers to work.

    For now this is all URLs, since Python connector takes care of authentication and
    session related actions.
    """
    return True


# TODO: We could create the single connection pool at import time
#  instead of having this function at all
# TODO: Configuration classes have no single parent class
def create_connection_pool(  # type: ignore[no-untyped-def]
        configuration,
        pools_size: int = 4,
        maxsize: typing.Optional[int] = None,
) -> SFPoolManager:
    # TODO: locking?
    global CONNECTION_POOL
    if CONNECTION_POOL is None:
        # urllib3.PoolManager will pass all kw parameters to connectionpool
        # https://github.com/shazow/urllib3/blob/f9409436f83aeb79fbaf090181cd81b784f1b8ce/urllib3/poolmanager.py#L75  # noqa: E501
        # https://github.com/shazow/urllib3/blob/f9409436f83aeb79fbaf090181cd81b784f1b8ce/urllib3/connectionpool.py#L680  # noqa: E501
        # maxsize is the number of requests to host that are allowed in parallel
        # Custom SSL certificates and client certificates: http://urllib3.readthedocs.io/en/latest/advanced-usage.html  # noqa: E501

        # cert_reqs
        if configuration.verify_ssl:
            cert_reqs = ssl.CERT_REQUIRED
        else:
            cert_reqs = ssl.CERT_NONE

        addition_pool_args = {}
        if configuration.assert_hostname is not None:
            addition_pool_args['assert_hostname'] = configuration.assert_hostname

        if configuration.retries is not None:
            addition_pool_args['retries'] = configuration.retries

        if configuration.socket_options is not None:
            addition_pool_args['socket_options'] = configuration.socket_options

        if maxsize is None:
            if configuration.connection_pool_maxsize is not None:
                maxsize = configuration.connection_pool_maxsize
            else:
                maxsize = 4

        # https pool manager
        cp_kwargs ={
            "num_pools": pools_size,
            "maxsize": maxsize,
            "cert_reqs": cert_reqs,
            "ca_certs": configuration.ssl_ca_cert,
            "cert_file": configuration.cert_file,
            "key_file": configuration.key_file,
            **addition_pool_args
        }
        if configuration.proxy:
            cp_kwargs.update(
                {
                    "proxy_url": configuration.proxy,
                    "proxy_headers": configuration.proxy_headers,
                }
            )
        logger.debug("created a new SFPoolManager")
        CONNECTION_POOL = SFPoolManager(**cp_kwargs)
    return CONNECTION_POOL
