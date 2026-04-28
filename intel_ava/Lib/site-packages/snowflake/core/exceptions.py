import json
import logging
import typing


if typing.TYPE_CHECKING:
    from urllib3.response import HTTPHeaderDict  # type: ignore[attr-defined]

    from snowflake.core import Root

    from .rest import RESTResponse

logger = logging.getLogger(__name__)


class SnowflakePythonError(Exception):
    """The base exception class for all Snowflake Python related Errors."""

    def __init__(self, *args: typing.Any) -> None:
        super().__init__(*args)


class _OpenAPIError(SnowflakePythonError):
    """The abstract class for all APIErrors such as TypeErrors, ValueErrors, and API Error Codes."""

    def __init__(self, *args: typing.Any) -> None:
        super().__init__(*args)
        class_name = self.__class__.__name__
        logger.debug(
            "An exception of type %s was created, it's probably going to be raised.",
            class_name,
        )
        logger.debug(
            "This exception has the following args: %r.",
            args,
        )


class _APITypeError(_OpenAPIError, TypeError):
    """Raises an exception for TypeErrors."""

    def __init__(
        self,
        msg: str,
        path_to_item: typing.Optional[list[typing.Union[str, int]]] = None,
        valid_classes: typing.Optional[tuple[type[typing.Any], ...]] = None,
        key_type: typing.Optional[bool] = None,
    ) -> None:
        """Initialize an APITypeError.

        Parameters
        __________
        msg: str
            The exception message.
        path_to_item: list, optional
            A list of keys and indices to get to the current_item. Default is None.
        valid_classes: tuple, optional
            The primitive classes that the current_item should be an instance of. Default is None.
        key_type: bool, optional
            The key_type of the current_item. True if it is a key in a dict,
            False if our value is a value in a dict or if our item is an item in a list.
            Default is None.

        """
        self.path_to_item = path_to_item
        self.valid_classes = valid_classes
        self.key_type = key_type
        full_msg = msg
        if path_to_item:
            full_msg = f"{msg} at {_render_path(path_to_item)}"
        super().__init__(full_msg)


class _APIValueError(_OpenAPIError, ValueError):
    """Raises an exception for ValueErrors."""

    def __init__(
        self,
        msg: str,
        path_to_item: typing.Optional[list[typing.Union[str, int]]] = None,
    ) -> None:
        """Initialize an APIValueError.

        Parameters
        __________
        msg: str
            The exception message.
        path_to_item: list, optional
            The path to the exception in the received_data dict. Default is None.

        """
        self.path_to_item = path_to_item
        full_msg = msg
        if path_to_item:
            full_msg = f"{msg} at {_render_path(path_to_item)}"
        super().__init__(full_msg)


class APIError(_OpenAPIError):
    """Raised when there is an exception with any of the API Error Codes."""

    def __init__(
        self,
        root: "Root",
        status: typing.Optional[int] = None,
        reason: typing.Optional[str] = None,
        http_resp: typing.Optional["RESTResponse"] = None,
        request_id: typing.Optional[str] = None,
    ) -> None:
        self.status = status
        self.reason = reason
        self.body: typing.Optional[typing.Union[bytes, typing.Any]] = None
        self.headers: typing.Optional[HTTPHeaderDict] = None
        self.request_id = request_id
        self.root = root
        if http_resp:
            self.status = http_resp.status
            self.reason = http_resp.reason
            self.body = http_resp.data
            self.headers = http_resp.getheaders()
            # Let's try to find request id if we can
            try:
                self.request_id = json.loads(self.body).get("request_id", "missing")
            except Exception:
                self.request_id = "missing"
                logger.error("Failed to extract request_id from the response body for an Error")
                logger.debug("The response body is: %r", self.body)
        super().__init__(str(self))

    def __str__(self) -> str:
        """Provide a custom error message for the exception."""
        error_message = f"({self.status})\nReason: {self.reason}\n"
        body = ""
        if self.body:
            if isinstance(self.body, bytes):
                body = self.body.decode()
            else:
                body = self.body
        if body:
            json_body = json.loads(body)
            message = json_body.get("message", "").strip()
            request_id = json_body.get("request_id", "")
            error_code = json_body.get("error_code", "")
            if not error_code:
                error_code = json_body.get("code", "")
            error_message += f"Error Message: {message}\n"
            error_message += f"HTTP response code: {self.status}\n"
            error_message += f"Request ID: {request_id}\n"
            error_message += f"Error Code: {error_code}\n"

        return error_message


class NotFoundError(APIError):
    """Raised when we encounter an HTTP error code of 404."""

    def __init__(
        self,
        root: "Root",
        status: typing.Optional[int] = None,
        reason: typing.Optional[str] = None,
        http_resp: typing.Optional["RESTResponse"] = None,
    ) -> None:
        super().__init__(root, status, reason, http_resp)


class UnauthorizedError(APIError):
    """Raised when we encounter an HTTP error code of 401."""

    def __init__(
        self,
        root: "Root",
        status: typing.Optional[int] = None,
        reason: typing.Optional[str] = None,
        http_resp: typing.Optional["RESTResponse"] = None,
    ) -> None:
        super().__init__(root, status, reason, http_resp)


class ForbiddenError(APIError):
    """Raised when we encounter an HTTP error code of 403."""

    def __init__(
        self,
        root: "Root",
        status: typing.Optional[int] = None,
        reason: typing.Optional[str] = None,
        http_resp: typing.Optional["RESTResponse"] = None,
    ) -> None:
        super().__init__(root, status, reason, http_resp)


class ServerError(APIError):
    """Raised when we encounter an HTTP error code of 5NN."""

    def __init__(
        self,
        root: "Root",
        status: typing.Optional[int] = None,
        reason: typing.Optional[str] = None,
        http_resp: typing.Optional["RESTResponse"] = None,
    ) -> None:
        super().__init__(root, status, reason, http_resp)


class ConflictError(APIError):
    """Raised when we encounter an HTTP error code of 409."""

    def __init__(
        self,
        root: "Root",
        status: typing.Optional[int] = None,
        reason: typing.Optional[str] = None,
        http_resp: typing.Optional["RESTResponse"] = None,
    ) -> None:
        super().__init__(root, status, reason, http_resp)


class UnexpectedResponseError(APIError):
    """Raised when spark-connect endpoint returns a response other than the expected protobuf."""

    def __init__(
        self,
        root: "Root",
        resp:typing.Any,
    ):
        super().__init__(root, status=200, reason="Unexpected response")
        self.body = json.dumps(resp)


def _render_path(
    path_to_item: typing.Optional[list[typing.Union[str, int]]],
) -> str:
    """Return a string representation of a path."""
    result = ""
    if path_to_item:
        for pth in path_to_item:
            if isinstance(pth, int):
                result += f"[{pth}]"
            else:
                result += f"['{pth}']"
    return result


class InvalidActionError(SnowflakePythonError):
    """Raised when the user makes an invalid action such as invalid parameters or when the API response is invalid.

    Base class for all errors such as InvalidArgumentsError, InvalidResponseError, etc.
    """

    def __init__(self, *args: typing.Any) -> None:
        super().__init__(*args)


class InvalidResponseError(InvalidActionError):
    """Raised when the api response is invalid."""

    def __init__(
        self,
        reason: typing.Optional[str] = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidArgumentsError(InvalidActionError):
    """Raised when function args are invalid."""

    def __init__(
        self,
        reason: typing.Optional[str] = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidResultError(InvalidActionError):
    """Raised when function result is invalid."""

    def __init__(
        self,
        reason: typing.Optional[str] = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason


class InvalidOperationError(InvalidActionError):
    """Raised when user tries to make an invalid operation."""

    def __init__(
        self,
        reason: typing.Optional[str] = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason


class RetryTimeoutError(SnowflakePythonError):
    """Raised when a request times out after any amount of retries."""

    def __init__(
            self,
            reason: typing.Optional[str] = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason


class MissingModuleError(SnowflakePythonError):
    """Exception for missing modules."""

    def __init__(self, module: str, post_error_blurb: typing.Optional[str] = None) -> None:
        error_msg = f"Missing module: {module}"
        if post_error_blurb is not None:
            error_msg += f" {post_error_blurb}"
        super().__init__(error_msg)


class FileOperationError(SnowflakePythonError): ...


class FilePutError(FileOperationError): ...


class FileGetError(FileOperationError): ...


class InvalidIdentifierError(SnowflakePythonError):
    """Raised when an identifier does not meet identifier requirements."""

    def __init__(self, identifier: str) -> None:
        super().__init__(f"'{identifier}' is not a valid identifier.")
