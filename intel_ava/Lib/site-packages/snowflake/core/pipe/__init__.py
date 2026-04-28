"""Manages Snowflake Pipes.

Example:
    >>> pipes: PipeCollection = root.databases["mydb"].schemas["myschema"].pipes
    >>> mypipe = pipes.create(Pipe("mypipe"))
    >>> pipe_iter = pipes.iter(like="my%")
    >>> pipe = pipes["mypipe"]
    >>> an_existing_pipe = pipes["an_existing_pipe"]

Refer to :class:`snowflake.core.Root` to create the ``root``.
"""


from ._pipe import Pipe, PipeCollection, PipeResource


__all__ = [
    "Pipe",
    "PipeCollection",
    "PipeResource",
]
