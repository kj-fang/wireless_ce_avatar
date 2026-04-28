import enum
import functools
import os
import warnings

from re import compile
from typing import TYPE_CHECKING, Callable, Optional, TypeVar, Union

from typing_extensions import ParamSpec

from snowflake.connector import ProgrammingError
from snowflake.connector.description import (
    OPERATING_SYSTEM,
    PLATFORM,
)
from snowflake.core.exceptions import FileGetError, FilePutError


if TYPE_CHECKING:
    from snowflake.core._root import Root


# The following code is copied from snowpark's code /snowflake/snowpark/_internal/utils.py to avoid being broken
# when snowpark changes the code.
# We'll need to move the code to a common place.
# Another solution is to move snowpark to the mono repo so the merge gate will find the breaking changes.
# To address later.

EMPTY_STRING = ""
DOUBLE_QUOTE = '"'
ALREADY_QUOTED = compile('^(".+")$')
UNQUOTED_CASE_INSENSITIVE = compile("^([_A-Za-z]+[_A-Za-z0-9$]*)$")
# https://docs.snowflake.com/en/sql-reference/identifiers-syntax.html
SNOWFLAKE_UNQUOTED_ID_PATTERN = r"([a-zA-Z_][\w\$]{0,255})"
SNOWFLAKE_QUOTED_ID_PATTERN = '("([^"]|""){1,255}")'
SNOWFLAKE_ID_PATTERN = f"({SNOWFLAKE_UNQUOTED_ID_PATTERN}|{SNOWFLAKE_QUOTED_ID_PATTERN})"
SNOWFLAKE_OBJECT_RE_PATTERN = compile(
    f"^(({SNOWFLAKE_ID_PATTERN}\\.){{0,2}}|({SNOWFLAKE_ID_PATTERN}\\.\\.)){SNOWFLAKE_ID_PATTERN}$"
)


def normalize_datatype(datatype: str) -> str:
    """Convert equivalent datatypes.

    These are according to https://docs.snowflake.com/en/sql-reference/intro-summary-data-types
    All string types to varchar, and all numeric types to number and float.
    """
    datatype = datatype.upper()
    datatype = datatype.replace(" ", "")
    if datatype in ("INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "BYTEINT", "NUMBER"):
        return "NUMBER(38,0)"
    if datatype in ("DOUBLE", "DOUBLEPRECISION", "REAL"):
        return "FLOAT"
    if datatype in ("STRING", "TEXT", "VARCHAR"):
        return "VARCHAR(16777216)"
    if datatype in ("CHAR", "CHARACTER"):
        return "VARCHAR(1)"
    if datatype in ("VARBINARY"):
        return "BINARY"
    datatype = datatype.replace("DECIMAL", "NUMBER").replace("NUMERIC", "NUMBER")\
        .replace("STRING", "VARCHAR").replace("TEXT", "VARCHAR")
    return datatype


def validate_object_name(name: str) -> None:
    if not SNOWFLAKE_OBJECT_RE_PATTERN.match(name):
        raise ValueError(f"The object name '{name}' is invalid.")


def is_running_inside_stored_procedure() -> bool:
    """
    Check if snowpy is running inside a stored procedure.

    Returns:
        bool: True if snowpy is running inside a stored procedure, False otherwise.
    """
    return PLATFORM == "XP"


def validate_quoted_name(name: str) -> str:
    if DOUBLE_QUOTE in name[1:-1].replace(DOUBLE_QUOTE + DOUBLE_QUOTE, EMPTY_STRING):
        raise ValueError(
            f"Invalid Identifier {name}. "
            f"The inside double quotes need to be escaped when the name itself is double quoted."
        )
    else:
        return name


def escape_quotes(unescaped: str) -> str:
    return unescaped.replace(DOUBLE_QUOTE, DOUBLE_QUOTE + DOUBLE_QUOTE)


def normalize_name(name: str) -> str:
    if ALREADY_QUOTED.match(name):
        return validate_quoted_name(name)
    elif UNQUOTED_CASE_INSENSITIVE.match(name):
        return escape_quotes(name.upper())
    else:
        return DOUBLE_QUOTE + escape_quotes(name) + DOUBLE_QUOTE


def normalize_and_unquote_name(name: str) -> str:
    return unquote_name(normalize_name(name))


def unquote_name(name: str) -> str:
    if len(name) > 1 and name[0] == name[-1] == '"':
        return name[1:-1]
    return name

P = ParamSpec("P")
R = TypeVar("R")


def deprecated(
    alternative: Optional[str] = None
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        """Mark methods as deprecated with a warning message."""
        deprecation_message = f"The :func:`{func.__name__}` method is deprecated;"
        if alternative:
            deprecation_message += f" use :func:`{alternative}` instead."

        @functools.wraps(func)
        def deprecate_wrapper(
                *args: P.args,
                **kwargs: P.kwargs) -> R:
            warnings.warn(deprecation_message, category=DeprecationWarning, stacklevel=2)
            return func(*args, **kwargs)

        if deprecate_wrapper.__doc__ is None:
            deprecate_wrapper.__doc__ = deprecation_message
        else:
            deprecate_wrapper.__doc__ = deprecation_message + "\n\n" + deprecate_wrapper.__doc__

        return deprecate_wrapper
    return decorator

@enum.unique
class TelemetryField(enum.Enum):
    # Copied over from snowflake.snowpark
    # constants
    MESSAGE = "message"
    NAME = "name"
    ERROR_CODE = "error_code"
    STACK_TRACE = "stack_trace"
    # Types of telemetry
    TYPE_PERFORMANCE_DATA = "snowpark_performance_data"
    TYPE_FUNCTION_USAGE = "snowpark_function_usage"
    TYPE_SESSION_CREATED = "snowpark_session_created"
    TYPE_SQL_SIMPLIFIER_ENABLED = "snowpark_sql_simplifier_enabled"
    TYPE_CTE_OPTIMIZATION_ENABLED = "snowpark_cte_optimization_enabled"
    # telemetry for optimization that eliminates the extra cast expression generated for expressions
    TYPE_ELIMINATE_NUMERIC_SQL_VALUE_CAST_ENABLED = (
        "snowpark_eliminate_numeric_sql_value_cast_enabled"
    )
    TYPE_AUTO_CLEAN_UP_TEMP_TABLE_ENABLED = "snowpark_auto_clean_up_temp_table_enabled"
    TYPE_ERROR = "snowpark_error"
    # Message keys for telemetry
    KEY_START_TIME = "start_time"
    KEY_DURATION = "duration"
    KEY_FUNC_NAME = "func_name"
    KEY_MSG = "msg"
    KEY_ERROR_MSG = "error_msg"
    KEY_VERSION = "version"
    KEY_PYTHON_VERSION = "python_version"
    KEY_CLIENT_LANGUAGE = "client_language"
    KEY_OS = "operating_system"
    KEY_DATA = "data"
    KEY_CATEGORY = "category"
    KEY_CREATED_BY_SNOWPARK = "created_by_snowpark"
    KEY_API_CALLS = "api_calls"
    KEY_SFQIDS = "sfqids"
    KEY_SUBCALLS = "subcalls"
    # function categories
    FUNC_CAT_ACTION = "action"
    FUNC_CAT_USAGE = "usage"
    FUNC_CAT_JOIN = "join"
    FUNC_CAT_COPY = "copy"
    FUNC_CAT_CREATE = "create"
    # performance categories
    PERF_CAT_UPLOAD_FILE = "upload_file"
    # sql simplifier
    SESSION_ID = "session_id"
    SQL_SIMPLIFIER_ENABLED = "sql_simplifier_enabled"
    CTE_OPTIMIZATION_ENABLED = "cte_optimization_enabled"
    # dataframe query stats
    QUERY_PLAN_HEIGHT = "query_plan_height"
    QUERY_PLAN_NUM_DUPLICATE_NODES = "query_plan_num_duplicate_nodes"
    QUERY_PLAN_COMPLEXITY = "query_plan_complexity"

STAGE_PREFIX = "@"
SNOWURL_PREFIX = "snow://"
SNOWFLAKE_PATH_PREFIXES = [
    STAGE_PREFIX,
    SNOWURL_PREFIX,
]

def is_single_quoted(name: str) -> bool:
    return name.startswith("'") and name.endswith("'") and len(name) >= 2

def normalize_path(path: str, is_local: bool) -> str:
    """Get a normalized path of a local file or remote stage location for PUT/GET commands.

    If there are any special characters including spaces in the path, it needs to be
    quoted with single quote. For example, 'file:///tmp/load data' for a path containing
    a directory named "load data". Therefore, if `path` is already wrapped by single quotes,
    we do nothing.
    """
    prefixes = ["file://"] if is_local else SNOWFLAKE_PATH_PREFIXES
    if is_single_quoted(path):
        return path
    if is_local and OPERATING_SYSTEM == "Windows":
        path = path.replace("\\", "/")
    path = path.strip().replace("'", "\\'")
    if not any(path.startswith(prefix) for prefix in prefixes):
        path = f"{prefixes[0]}{path}"
    return f"'{path}'"

def get_local_file_path(file: str) -> str:
    trim_file = file.strip()
    if is_single_quoted(trim_file):
        trim_file = trim_file[1:-1]  # remove the pair of single quotes
    if trim_file.startswith("file://"):
        return trim_file[7:]  # remove "file://"
    return trim_file

def get_file(
    root: "Root",
    stage_location: str,
    target_directory: str,
    *,
    parallel: int = 10,
    pattern: Optional[str] = None,
) -> None:
    options: dict[str, Union[int, str]] = {"parallel": parallel}
    if pattern is not None:
        if not is_single_quoted(pattern):
            pattern_escape_single_quote = pattern.replace("'", "\\'")
            pattern = f"'{pattern_escape_single_quote}'"  # snowflake pattern is a string with single quote
        options["pattern"] = pattern

    if is_running_inside_stored_procedure():  # pragma: no cover
        try:
            cur = root.connection.cursor()
            cur._download(stage_location, target_directory, options)
            cur.fetchall()  # Make sure no errors were raised
        except ProgrammingError as pe:
            raise FileGetError(
                pe.msg,
            ) from None
    else:
        os.makedirs(get_local_file_path(target_directory), exist_ok=True)
        with root.connection.cursor() as cur:
            cur.execute(
                "GET /* snowflake.core.stage.StageResource.get */ "
                + normalize_path(stage_location, is_local=False) + " "
                + normalize_path(target_directory, is_local=True) + " "
                + f"PARALLEL = {parallel}"
                + (f" PATTERN = {pattern};" if pattern is not None else ";")
            )
            cur.fetchall()  # Make sure no errors were raised

def put_file(
    root: "Root",
    local_file_name: str,
    stage_location: str,
    *,
    parallel: int = 4,
    auto_compress: bool = True,
    source_compression: str = "AUTO_DETECT",
    overwrite: bool = False,
) -> None:
    if is_running_inside_stored_procedure():  # pragma: no cover
        try:
            cur = root.connection.cursor()
            cur._upload(
                local_file_name,
                stage_location,
                {
                    "parallel": parallel,
                    "source_compression": source_compression,
                    "auto_compress": auto_compress,
                    "overwrite": overwrite,
                },
            )
            cur.fetchall()  # Make sure no errors were raised
        except ProgrammingError as pe:
            raise FilePutError(
                pe.msg,
            ) from None
    else:
        with root.connection.cursor() as cur:
            cur.execute(
                "PUT /* snowflake.core.stage.StageResource.put */ "
                + normalize_path(local_file_name, is_local=True) + " "
                + normalize_path(stage_location, is_local=False) + " "
                + f"PARALLEL = {parallel} "
                + f"AUTO_COMPRESS = {auto_compress} "
                + f"SOURCE_COMPRESSION = {source_compression} "
                + f"OVERWRITE = {overwrite};"
            )
            cur.fetchall()  # Make sure no errors were raised
