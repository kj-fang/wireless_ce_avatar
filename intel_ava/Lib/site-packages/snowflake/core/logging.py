import logging
import pathlib
import tempfile
import typing


tempdir = pathlib.Path(tempfile.gettempdir())

def simple_file_logging(
    path: typing.Union[str, pathlib.Path, None] = None,
    *,
    level: int = logging.DEBUG,
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
) -> None:
    """Set up snowflake.core logging to a file.

    If path is not given the tempfile module will be used to determine the
    appropiate temporary location and a file named "snowflake_core.log"
    will be created.
    The parameter level controls what logging level is enabled in the file.
    The format parameter allows to modify the formatter output in the file.
    """
    if path is None:
        path = tempdir / "snowflake_core.log"
    logger = logging.getLogger("snowflake.core")
    # Increase log level in case the log level of
    #  the top-level logger is lower than necessary
    if not logger.isEnabledFor(level):
        logger.setLevel(level)
    fh = logging.FileHandler(path)
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(format))
    logger.addHandler(fh)
