"""Manages Snowflake Notebooks.

Examples:
    >>> notebooks: NotebookCollection = root.databases["my_db"].schemas["my_schema"].notebooks
    >>> my_notebook = notebooks.create(Notebook("my_notebook"))
    >>> notebook_iter = notebooks.iter(like="my%")
    >>> notebook = notebooks["my_notebook"]
    >>> an_existing_notebook = notebooks["an_existing_notebook"]

Refer to :class:`snowflake.core.Root` to create the ``root``.
"""


from ._notebook import Notebook, NotebookCollection, NotebookResource, VersionDetails


__all__ = [
    "Notebook",
    "NotebookCollection",
    "NotebookResource",
    "VersionDetails",
]
