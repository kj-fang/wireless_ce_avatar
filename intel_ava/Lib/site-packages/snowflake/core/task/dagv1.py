"""High level, client-side representation of task graphs.

This set of higher-level classes provides a more convenient way to create, deploy, and manage task graphs
than the lower-level Task APIs in `snowflake.core.task`. Task graphs are directed acyclic graphs (DAG) of tasks.

Example 1: Create a task graph that has two Tasks.
    >>> from snowflake.snowpark.functions import sum as sum_
    >>> from snowflake.core.task import StoredProcedureCall
    >>> from snowflake.core.task.dagv1 import DAG, DAGTask, DAGOperation
    >>> def dosomething(session: Session) -> None:
    ...     df = session.table("target")
    ...     df.group_by("a").agg(sum_("b")).save_as_table("agg_table")
    >>> with DAG("my_dag", schedule=timedelta(days=1)) as dag:
    ...     # Create a task that runs some SQL.
    ...     dag_task1 = DAGTask(
    ...         "dagtask1",
    ...         "MERGE INTO target USING source_stream WHEN MATCHED THEN UPDATE SET target.v = source_stream.v")
    ...     # Create a task that runs a Python function.
    ...     dag_task2 = DAGTask(
    ...         StoredProcedureCall(
    ...             dosomething, stage_location="@mystage",
    ...             packages=["snowflake-snowpark-python"]
    ...         ),
    ...         warehouse="test_warehouse")
    ...     )
    >>> # Shift right and left operators can specify task relationships.
    >>> dag_task1 >> dag_task2
    >>> schema = root.databases["MYDB"].schemas["MYSCHEMA"]
    >>> dag_op = DAGOperation(schema)
    >>> dag_op.deploy(dag)

Example 2: Create a task graph that uses Cron, Branch, and function return value as Task return value
    >>> from snowflake.snowpark import Session
    >>> from snowflake.core import Root
    >>> from snowflake.core._common import CreateMode
    >>> from snowflake.core.task import Cron
    >>> from snowflake.core.task.dagv1 import DAG, DAGTask, DAGOperation, DAGTaskBranch
    >>> session = Session.builder.create()
    >>> test_stage = "mystage"
    >>> test_dag = "mydag"
    >>> test_db = "mydb"
    >>> test_schema = "public"
    >>> test_warehouse = "testwh_python"
    >>> root = Root(session)
    >>> schema = root.databases[test_db].schemas[test_schema]
    >>> def task_handler1(session: Session) -> None:
    ...     pass  # do something
    >>> def task_handler2(session: Session) -> None:
    ...     pass  # do something
    >>> def task_handler3(session: Session) -> None:
    ...     pass  # do something
    >>> def task_branch_handler(session: Session) -> str:
    ...     # do something
    ...     return "task3"
    >>> try:
    ...     with DAG(
    ...         test_dag,
    ...         schedule=Cron("10 * * * *", "America/Los_Angeles"),
    ...         stage_location=test_stage,
    ...         packages=["snowflake-snowpark-python"],
    ...         warehouse=test_warehouse,
    ...         use_func_return_value=True,
    ...     ) as dag:
    ...         task1 = DAGTask(
    ...             "task1",
    ...             task_handler1,
    ...         )
    ...         task1_branch = DAGTaskBranch("task1_branch", task_branch_handler, warehouse=test_warehouse)
    ...         task2 = DAGTask("task2", task_handler2)
    ...         task1 >> task1_branch
    ...         task1_branch >> [task2, task_handler3]  # after >> you can use a DAGTask or a function.
    ...     op = DAGOperation(schema)
    ...     op.deploy(dag, mode=CreateMode.or_replace)
    >>> finally:
    ...     session.close()
"""
import json
import logging
import warnings

from collections import deque
from collections.abc import Iterable, Iterator
from contextlib import suppress
from datetime import datetime, timedelta
from functools import wraps
from types import ModuleType, TracebackType
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Optional,
    Union,
)

from typing_extensions import Self

from snowflake.core import FQN
from snowflake.core._common import CreateMode
from snowflake.core._internal.telemetry import api_telemetry
from snowflake.core._internal.utils import deprecated, normalize_name
from snowflake.core.exceptions import InvalidOperationError, NotFoundError
from snowflake.core.schema import SchemaResource
from snowflake.core.task import Cron, StoredProcedureCall, Task
from snowflake.core.task._generated import TaskRun


if TYPE_CHECKING:
    from snowflake.snowpark import Session


logger = logging.getLogger(__name__)
_dag_context_stack: deque["DAG"] = deque()


def _get_current_dag() -> Optional["DAG"]:
    if _dag_context_stack:
        return _dag_context_stack[-1]
    return None


def _use_func_return_value(f: Callable[["Session"], Optional[str]]) -> Callable[["Session"], Optional[str]]:
    @wraps(f)
    def wrapper(session: "Session") -> Optional[str]:
        f_return_value = f(session)
        if f_return_value is not None:
            try:
                session.call("system$set_return_value", str(f_return_value))
            except Exception as sse:
                if "Function SYSTEM$SET_RETURN_VALUE must be called from within a task" not in str(sse):
                    raise
        return f_return_value

    return wrapper


def _convert_func_to_task(
    other: Union[
        "DAGTask",
        Iterable[Union["DAGTask", Callable[["Session"], Optional[str]]]],
        Callable[["Session"], Optional[str]],
    ],
) -> Union["DAGTask", Iterable["DAGTask"]]:
    tasks: Union[DAGTask, Iterable[DAGTask]]
    if callable(other):
        tasks = DAGTask(other.__name__, other)
    elif isinstance(other, DAGTask):
        tasks = other
    elif isinstance(other, Iterable):
        tasks = [DAGTask(t.__name__, t) if callable(t) else t for t in other]
    else:
        raise TypeError(f"Expect a DAGTAsk, a function, or a list of them. {type(other)} is provided.")
    return tasks


def _add_condition(full_name: str, successor: "DAGTask") -> None:
    validate_branch = f"SYSTEM$GET_PREDECESSOR_RETURN_VALUE('{normalize_name(full_name)}') = '{successor.name}'"
    if successor.condition:
        successor.condition = f"{validate_branch} and {successor.condition}"
    else:
        successor.condition = validate_branch


class DAG:
    """A graph of tasks composed of a single root task and additional tasks, organized by their dependencies.

    Snowflake doesn't have a first-class task graph entity, so this is a client-side object representation which
    manages Task relationship. A root :class:`Task` and its successors logically form a task graph or DAG
    (Directed Acyclic Graph).  Refer to
    `Task graphs <https://docs.snowflake.com/en/user-guide/tasks-intro#task-graphs>`_.

    When a task graph is deployed, all child tasks are created in Snowflake. A dummy Task is created as the root.
    A task's predecessor is the dummy task if it's added to the task graph with no other predecessors.

    Example:
        >>> dag = DAG("TEST_DAG",
        ...     schedule=timedelta(minutes=10),
        ...     use_func_return_value=True,
        ...     warehouse="TESTWH_DAG",
        ...     packages=["snowflake-snowpark-python"],
        ...     stage_location="@TESTDB_DAG.TESTSCHEMA_DAG.TEST_STAGE_DAG"
        ... )
        >>> def task1(session: Session) -> None:
        ...     session.sql("select 'task1'").collect()
        >>> def task2(session: Session) -> None:
        ...     session.sql("select 'task2'").collect()
        >>> def cond(session: Session) -> str:
        ...     return 'TASK1'
        >>> with dag:
        ...     task1 = DAGTask("TASK1", definition=task1, warehouse="TESTWH_DAG")
        ...     task2 = DAGTask("TASK2", definition=task2, warehouse="TESTWH_DAG")
        ...     condition = DAGTaskBranch("COND", definition=cond, warehouse="TESTWH_DAG")
        ...     condition >> [task1, task2]
        >>> dag_op = DAGOperation(schema)
        >>> dag_op.deploy(dag, mode="orReplace")
        >>> dag_op.run(dag)
        Note:
            When defining a task branch handler, simply return the task name you want to jump to. The task name is
            case-sensitive, and it has to match the name property in DAGTask. For exmaple, in above sample code, return
            'TASK1' instead of 'TEST_DAG$TASK1', 'task1' or 'Task1' will not be considered as a exact match.

    Refer to :class:`snowflake.core.task.Task` for the details of each property.
    """

    def __init__(
        self,
        name: str,
        *,
        schedule: Optional[Union[Cron, timedelta]] = None,
        warehouse: Optional[str] = None,
        user_task_managed_initial_warehouse_size: Optional[str] = None,
        error_integration: Optional[str] = None,
        comment: Optional[str] = None,
        task_auto_retry_attempts: Optional[int] = None,
        allow_overlapping_execution: Optional[bool] = None,
        user_task_timeout_ms: Optional[int] = None,
        suspend_task_after_num_failures: Optional[int] = None,
        config: Optional[dict[str, Any]] = None,
        session_parameters: Optional[dict[str, Any]] = None,
        stage_location: Optional[str] = None,
        imports: Optional[list[Union[str, tuple[str, str]]]] = None,
        packages: Optional[list[Union[str, ModuleType]]] = None,
        use_func_return_value: bool = False,
    ) -> None:
        #: Name of the task graph and the dummy root task.
        self.name = name
        #: Refer to :attr:`snowflake.core.task.Task.warehouse`.
        self.warehouse = warehouse
        #: comment of the task graph.
        self.comment = comment
        #: Schedule of the task graph. Refer to :attr:`snowflake.core.task.Task.schedule`.
        self.schedule = schedule
        #: Refer to :attr:`snowflake.core.task.Task.error_integration`.
        self.error_integration = error_integration
        #: Refer to :attr:`snowflake.core.task.Task.allow_overlapping_execution`.
        self.allow_overlapping_execution = allow_overlapping_execution
        #: Refer to :attr:`snowflake.core.task.Task.user_task_timeout_ms`.
        self.user_task_timeout_ms = user_task_timeout_ms
        #: Refer to :attr:`snowflake.core.task.Task.suspend_task_after_num_failures`.
        self.suspend_task_after_num_failures = suspend_task_after_num_failures
        #: Refer to :attr:`snowflake.core.task.Task.config`.
        self.config = config
        #: Refer to :attr:`snowflake.core.task.Task.session_parameters`.
        self.session_parameters = session_parameters
        self._tasks: dict[str, DAGTask] = dict()
        self._task_list: Optional[list[DAGTask]] = None  # as a cache for property tasks

        #: The default stage location where this task graph's tasks code will be stored
        #: if creating the tasks from Python functions.
        self.stage_location = stage_location
        #: The default imports for all tasks of this task graph if creating the tasks from Python functions.
        self.imports = imports
        #: The default packages for the tasks of this task graph if creating the tasks from Python functions.
        self.packages = packages

        #: Use the Python function's return value as Task return value if ``use_func_return_value`` is True.
        #: Default False.
        self.use_func_return_value = use_func_return_value

        # name of the finalizer task, this can be none because it is not necessary to have a finalizer task
        # remember that there can only be one finalizer task
        self._finalizer_task: Optional[str] = None

        #: Refer to :attr:`snowflake.core.task.Task.task_auto_retry_attempts`.
        self.task_auto_retry_attempts = task_auto_retry_attempts

        if user_task_managed_initial_warehouse_size is not None:
            warnings.warn(
                "Providing user_task_managed_initial_warehouse_size for a dummy root Task is deprecated and "
                "will be removed in the next major version. You can specify user_task_managed_initial_warehouse_size "
                "for the child Tasks instead.",
                category=DeprecationWarning,
                stacklevel=1,
            )

    def _to_low_level_task(self) -> Task:
        return Task(
            name=f"{self.name}",
            definition="select 'dag dummy root'",
            schedule=self.schedule,
            warehouse=self.warehouse,
            error_integration=self.error_integration,
            comment=self.comment,
            task_auto_retry_attempts=self.task_auto_retry_attempts,
            allow_overlapping_execution=self.allow_overlapping_execution,
            user_task_timeout_ms=self.user_task_timeout_ms,
            suspend_task_after_num_failures=self.suspend_task_after_num_failures,
            session_parameters=self.session_parameters,
            config=self.config,
        )

    def add_task(self, task: "DAGTask") -> None:
        """Add a child task to this task graph.

        Parameters
        __________
        task: DAGTask
            The child task to be added to this task graph.

        Examples
        ________
        Add a task to previously created DAG

        >>> child_task = DagTask(
        ...     "child_task",
        ...     "select 'child_task'",
        ...     warehouse="test_warehouse"
        ... )
        >>> dag.add_task(child_task)
        )
        """
        if task.dag is not self:
            raise ValueError("Cannot move a task to a different task graph")
        self._tasks[task.name] = task
        self._task_list = None

    def get_task(self, task_name: str) -> Optional["DAGTask"]:
        """Get a child task from this task graph based on task name.

        Parameters
        __________
        task_name: str
            The name of the task to be retrieved from this task graph.

        Examples
        ________
        Get a task from previously created DAG

        >>> task = dag.get_task("child_task")
        """
        return self._tasks.get(task_name)

    def get_finalizer_task(self) -> Optional["DAGTask"]:
        """Get the finalizer task for the dag.

        Examples
        ________
        Get the finalizer task from previously created DAG:

        >>> finalizer_task = dag.get_finalizer_task()
        """
        if self._finalizer_task is not None:
            return self._tasks.get(str(self._finalizer_task))
        return None

    @property
    def tasks(self) -> list["DAGTask"]:
        """Returns a list of tasks this task graph has."""
        if self._task_list is None:
            self._task_list = list(self._tasks.values())
        return self._task_list

    def __repr__(self) -> str:
        return f"<DAG: {self.name}>"

    def __contains__(self, task: Union["DAGTask", str]) -> bool:
        task_name = task.name if isinstance(task, DAGTask) else task
        return task_name in self._tasks

    def __enter__(self) -> Self:
        _dag_context_stack.append(self)
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        _dag_context_stack.pop()


class DAGTask:
    """Represents a child Task of a task graph.

    A child task has a subset of properties of :class:`snowflake.core.task.Task`. For instance, it doesn't
    have ``schedule`` because it's triggered after the predecessors are finished.

    Parameters
    __________
    is_finalizer: bool, optional
        Whether this child task is a finalizer task for the DAG.
    is_serverless: bool
        Whether the child task is serverless. If set, no warehouse can be specified for the task. If not set
        and no warehouse is specified for the child Task, then the warehouse setting is inherited from the root Task.
        This means that if both the child Task and its DAG do not have a warehouse specified then the child Task will
        be created as serverless even if is_serverless is set to False. Default is False.
    dag: DAG, optional
        The DAG this child Task belong to.


    Refer to :class:`snowflake.core.task.Task` for the details of the remaining properties.
    """

    def __init__(
        self,
        name: str,
        definition: Union[str, Callable[..., Any], StoredProcedureCall],
        *,
        condition: Optional[str] = None,
        warehouse: Optional[str] = None,
        session_parameters: Optional[dict[str, Any]] = None,
        user_task_managed_initial_warehouse_size: Optional[str] = None,
        target_completion_interval: Optional[timedelta] = None,
        serverless_task_min_statement_size: Optional[str] = None,
        serverless_task_max_statement_size: Optional[str] = None,
        user_task_timeout_ms: Optional[int] = None,
        error_integration: Optional[str] = None,
        comment: Optional[str] = None,
        is_finalizer: Optional[bool] = None,
        is_serverless: bool = False,
        dag: Optional[DAG] = None,
    ) -> None:
        self.name = name  #: Name of the Task.
        self.definition = definition  #: refer to :attr:`snowflake.core.task.Task.definition`.
        self.condition = condition  #: refer to :attr:`snowflake.core.task.Task.condition`.
        self.session_parameters = session_parameters  #: refer to :attr:`snowflake.core.task.Task.session_parameter`.
        self.comment = comment  #: comment for the task.
        # refer to :attr:`snowflake.core.task.Task.user_task_managed_initial_warehouse_size`.
        self.user_task_managed_initial_warehouse_size = user_task_managed_initial_warehouse_size
        #: refer to :attr:`snowflake.core.task.Task.target_completion_interval`.
        self.target_completion_interval = target_completion_interval
        #: refer to :attr:`snowflake.core.task.Task.serverless_task_min_statement_size`.
        self.serverless_task_min_statement_size = serverless_task_min_statement_size
        #: refer to :attr:`snowflake.core.task.Task.serverless_task_max_statement_size`.
        self.serverless_task_max_statement_size = serverless_task_max_statement_size
        self.user_task_timeout_ms = user_task_timeout_ms  #: refer to :attr:`snowflake.core.task.user_task_timeout_ms`
        dag = dag or _get_current_dag()
        if dag is None:
            raise ValueError("Parameter 'dag' must be set when creating a DAGTask.")
        if is_serverless and warehouse is not None:
            raise ValueError("Warehouse was provided for a serverless task.")
        self.warehouse = dag.warehouse if (warehouse is None and not is_serverless) else warehouse  # : refer to
        # :attr:`snowflake.core.task.Task.warehouse`.

        if error_integration is not None:
            warnings.warn(
                "error_integration cannot be specified for a child Task. This argument will be removed "
                "in the next major version. Provide the error_integration for the DAG instead.",
                category=DeprecationWarning,
                stacklevel=1,
            )

        self._dag = dag
        # This is for creating a finalizer task for the dag
        self._is_finalizer = is_finalizer
        if is_finalizer:
            if self._dag._finalizer_task is None:
                self._dag._finalizer_task = self.name
            else:
                raise InvalidOperationError(
                    f"Finalizer task for this Dag {self.name} already exists named: {self._dag._finalizer_task}"
                )
        self._dag.add_task(self)
        self._predecessors: set[DAGTask] = set()

    @property
    def full_name(self) -> str:
        """The full name of the child task."""
        return f"{self.dag.name}${self.name}"

    @property
    def dag(self) -> DAG:
        """Return the :class:`DAG` of this task."""
        return self._dag

    def add_predecessors(
        self,
        other: Union[
            "DAGTask",
            Iterable[Union["DAGTask", Callable[["Session"], Optional[str]]]],
            Callable[["Session"], Optional[str]],
        ],
    ) -> None:
        """Add a task or a task list to the direct predecessors of the current task.

        Parameters
        __________
        other: Union[DAGTask, Iterable[DAGTask], Callable[["Session"], Optional[str]]]
            The task or task list to be added as the direct predecessors of the current task.

        Examples
        ________
        Add a task to the predecessors of the current task:

        >>> task1 = DAGTask("task1", "select 'task1'")
        >>> task2 = DAGTask("task2", "select 'task2'")
        >>> task1.add_predecessors(task2)
        """
        if self._is_finalizer:
            raise InvalidOperationError(f"Finalizer task {self.name} cannot have any predecessors")
        tasks = _convert_func_to_task(other)
        if isinstance(tasks, DAGTask):
            tasks = [tasks]

        for t in tasks:
            if t.dag is not self.dag:
                raise InvalidOperationError(f"Task {t.name} belongs to a different task graph")
            if t._is_finalizer:
                raise InvalidOperationError(f"Finalizer task {t.name} cannot be predecessor of any task")

        for t in tasks:
            if isinstance(t, DAGTaskBranch):
                _add_condition(t.full_name, self)
        self._predecessors.update(tasks)

    def add_successors(
        self,
        other: Union[
            "DAGTask",
            Iterable[Union["DAGTask", Callable[["Session"], Optional[str]]]],
            Callable[["Session"], Optional[str]],
        ],
    ) -> None:
        """Add a task or a task list to be the direct successor of the current task.

        Parameters
        __________
        other: Union[DAGTask, Iterable[DAGTask], Callable[["Session"], Optional[str]]]
            The task or task list to be added as the direct successors of the current task.

        Examples
        ________
        Add a task to the successors of the current task:

        >>> task1 = DAGTask("task1", "select 'task1'")
        >>> task2 = DAGTask("task2", "select 'task2'")
        >>> task1.add_successors(task2)
        """
        if self._is_finalizer:
            raise InvalidOperationError(f"Finalizer task {self.name} cannot have any successors")
        tasks = _convert_func_to_task(other)
        if isinstance(tasks, DAGTask):
            tasks = [tasks]

        for t in tasks:
            if t.dag is not self.dag:
                raise InvalidOperationError(f"Task {t.name} belongs to a different task graph")
            if t._is_finalizer:
                raise InvalidOperationError(f"Finalizer task {t.name} cannot have any predecessors")

        for t in tasks:
            t.add_predecessors(self)

    @property
    def predecessors(self) -> set["DAGTask"]:
        """Return the predecessors of the Task."""
        return self._predecessors

    def __repr__(self) -> str:
        return f"<DAGTask: {self.name}>"

    def __lshift__(
        self,
        other: Union[
            "DAGTask",
            Iterable[Union["DAGTask", Callable[["Session"], Optional[str]]]],
            Callable[["Session"], Optional[str]],
        ],
    ) -> Union["DAGTask", Iterable["DAGTask"]]:
        """Implement task1 << task2."""
        if self._is_finalizer:
            raise InvalidOperationError(f"Finalizer task {self.name} cannot have any predecessors")
        tasks = _convert_func_to_task(other)
        self.add_predecessors(tasks)
        return tasks

    def __rshift__(
        self,
        other: Union[
            "DAGTask",
            Iterable[Union["DAGTask", Callable[["Session"], Optional[str]]]],
            Callable[["Session"], Optional[str]],
        ],
    ) -> Union["DAGTask", Iterable["DAGTask"]]:
        """Implement task1 >> task2."""
        if self._is_finalizer:
            raise InvalidOperationError(f"Finalizer task {self.name} cannot have any successors")
        tasks = _convert_func_to_task(other)
        self.add_successors(tasks)
        return tasks

    def _to_low_level_task(self) -> Task:
        if isinstance(self.definition, StoredProcedureCall):
            func = self.definition.func
            if isinstance(func, Callable) and self.dag.use_func_return_value:  # type: ignore
                func = _use_func_return_value(func)
            definition = StoredProcedureCall(
                func,
                args=self.definition._args,
                return_type=self.definition._return_type,
                input_types=self.definition._input_types,
                stage_location=self.definition._stage_location or self.dag.stage_location,
                imports=self.definition._imports or self.dag.imports,
                packages=self.definition._packages or self.dag.packages,
            )
        elif isinstance(self.definition, Callable):  # type: ignore
            definition = StoredProcedureCall(
                _use_func_return_value(self.definition) if self.dag.use_func_return_value else self.definition,  # type: ignore
                stage_location=self.dag.stage_location,
                imports=self.dag.imports,
                packages=self.dag.packages,
            )
        else:
            definition = self.definition  # type: ignore

        predecessors = None
        if self.predecessors:
            predecessors = [x.full_name for x in self.predecessors]
        elif not self._is_finalizer:
            predecessors = [self._dag.name]
        return Task(
            name=self.full_name,
            definition=definition,
            condition=self.condition,
            warehouse=self.warehouse,
            user_task_managed_initial_warehouse_size=self.user_task_managed_initial_warehouse_size,
            target_completion_interval=self.target_completion_interval,
            serverless_task_min_statement_size=self.serverless_task_min_statement_size,
            serverless_task_max_statement_size=self.serverless_task_max_statement_size,
            comment=self.comment,
            user_task_timeout_ms=self.user_task_timeout_ms,
            session_parameters=self.session_parameters,
            predecessors=predecessors,
            finalize=self._dag.name if self._is_finalizer else None,
        )


class DAGTaskBranch(DAGTask):
    pass


class DAGRun:
    """Contains the history of a task graph run in Snowflake.

    From https://docs.snowflake.com/en/sql-reference/functions/current_task_graphs#output,
    but tweaked a little bit to fit into task graph context.
    """

    dag_name: str  #: Name of the root task and the task graph.
    database_name: str  #: Name of the database that contains the graph.
    schema_name: str  #: Name of the schema that contains the graph.
    state: str  #: state of the task graph run. One of ["SCHEDULED", "EXECUTING", "SUCCEEDED", "FAILED", "CANCELLED"].
    first_error_task_name: Optional[str]  #: Name of the first task in the graph that returned an error.
    first_error_code: Optional[int]  #: Error code of the error returned by the task named in ``first_error_task_name``.
    first_error_message: Optional[
        str
    ]  #: Error message of the error returned by the task named in ``first_error_task_name``.
    scheduled_time: Optional[datetime]  #: Time when the root task was scheduled to start running.
    query_start_time: Optional[datetime]  #: Time when the query in the root task definition started to run.
    # Time when the root task is next scheduled to start running, assuming the current run of the task graph started
    #  at the scheduled_time completes in time.
    next_scheduled_time: datetime
    run_id: int  #: epoch time in milliseconds of the root task's original scheduled start time.
    graph_version: int  #: Integer identifying the version of the task graph that was run, or is scheduled to be run.

    @classmethod
    def _from_taskrun(cls, taskrun: TaskRun) -> "DAGRun":
        dagrun = cls()
        dagrun.database_name = taskrun.database_name
        dagrun.schema_name = taskrun.schema_name
        dagrun.state = taskrun.state
        dagrun.first_error_task_name = taskrun.first_error_task_name
        dagrun.first_error_code = taskrun.first_error_code
        dagrun.first_error_message = taskrun.first_error_message
        dagrun.scheduled_time = taskrun.scheduled_time
        dagrun.query_start_time = taskrun.query_start_time
        dagrun.next_scheduled_time = taskrun.next_scheduled_time
        dagrun.run_id = taskrun.run_id
        dagrun.graph_version = taskrun.graph_version
        dagrun.dag_name = taskrun.root_task_name
        return dagrun

    def __str__(self) -> str:
        return f"""run_id: {self.run_id}, dag_name: {self.dag_name}, database_name: {self.database_name}, schema_name:
{self.schema_name}, state: {self.state}, first_error_task_name: {self.first_error_task_name},
first_error_code: {self.first_error_code}, first_error_message: {self.first_error_message},
scheduled_time: {self.scheduled_time}, query_start_time: {self.query_start_time},
next_scheduled_time: {self.next_scheduled_time}, graph_version: {self.graph_version}
"""

    def _repr_html_(self) -> str:
        return f"""<table border="1">
<tr><th>Property</th><th>Value</th></tr>
<tr><td>run_id</td><td>{self.run_id}</td></tr>
<tr><td>dag_name</td><td>{self.dag_name}</td></tr>
<tr><td>database_name</td><td>{self.database_name}</td></tr>
<tr><td>schema_name</td><td>{self.schema_name}</td></tr>
<tr><td>state</td><td>{self.state}</td></tr>
<tr><td>first_error_task_name</td><td>{self.first_error_task_name}</td></tr>
<tr><td>first_error_code</td><td>{self.first_error_code}</td></tr>
<tr><td>first_error_message</td><td>{self.first_error_message}</td></tr>
<tr><td>scheduled_time</td><td>{self.scheduled_time}</td></tr>
<tr><td>query_start_time</td><td>{self.query_start_time}</td></tr>
<tr><td>next_scheduled_time</td><td>{self.next_scheduled_time}</td></tr>
<tr><td>graph_version</td><td>{self.graph_version}</td></tr>
</table>
"""


class DAGOperation:
    """APIs to manage task graph child task operations."""

    def __init__(self, schema: SchemaResource) -> None:
        self.schema = schema
        """The schema that the task graph's child tasks will be read from or create into."""

    def iter_dags(self, *, like: str) -> list[str]:
        """Return the task graph names under this schema.

        Parameters
        __________
        like: str
            A case-insensitive string functioning as a filter, with support for SQL
            wildcard characters (% and _).

        Examples
        ________
        Get all task graph names under the schema:

        >>> dags = dag_op.iter_dags(like="your-dag-name")
        """
        return [x.name for x in self.schema.tasks.iter(like=like, root_only=True)]

    @api_telemetry
    @deprecated("drop")
    def delete(self, dag: Union[DAG, str]) -> None:
        self.drop(dag=dag)

    @api_telemetry
    def drop(self, dag: Union[DAG, str], *, drop_finalizer: bool = False) -> None:
        """Remove a task graph and all child tasks.

        Parameters
        __________
        dag: Union[DAG, str]
            Name of the task graph to be dropped or a ``DAG`` instance.
        drop_finalizer: bool
            If true, a finalizer task will also be dropped if it exists.

        Examples
        ________
        Drop a task graph:

        >>> dag_op.drop("your-dag-name")
        """
        def _drop(name: str) -> None:
            task = self.schema.tasks[name]
            with suppress(NotFoundError):
                task.suspend()
                task.drop()

        if drop_finalizer is False:
            warnings.warn(
                "Setting drop_finalizer to False is deprecated. This argument will be removed in the next major "
                "version and finalizer task will always be dropped alongside the DAG.",
                category=DeprecationWarning,
                stacklevel=1,
            )

        dag_name = dag.name if isinstance(dag, DAG) else dag
        root_task = self.schema.tasks[dag_name]
        try:
            root_task_details = root_task.fetch()
        except NotFoundError:
            logger.info("Root task '%s' already dropped", dag_name)
            return

        if root_task_details.state == "started":
            root_task.suspend()

        tasks = self.schema.tasks[dag_name].fetch_task_dependents()
        for t in reversed(tasks):
            _drop(t.name)

        if drop_finalizer:
            finalizer = self._extract_finalizer(root_task_details)
            if finalizer:
                _drop(finalizer.name)

    @api_telemetry
    def deploy(self, dag: DAG, mode: CreateMode = CreateMode.error_if_exists) -> None:
        """Deploys (create) this task graph including all child tasks under a specific schema in Snowflake.

        Parameters
        __________
        dag: The :class:`DAG` instance.
        mode: CreateMode, optional
            One of the following strings.

            ``CreateMode.error_if_exists``: Throw an :class:`snowflake.core.exceptions.ConflictError`
            if the task already exists in Snowflake. Equivalent to SQL ``create task <name> ...``.

            ``CreateMode.or_replace``: Replace if the task already exists in Snowflake. Equivalent to SQL
            ``create or replace task <name> ...``.

            ``CreateMode.if_not_exists``: Do nothing if the task already exists in Snowflake.
            Equivalent to SQL ``create task <name> if not exists...``

            Default value is ``CreateMode.error_if_exists``.
        """

        def topological_sort() -> Iterator[DAGTask]:
            """Topologically sort by using Kahn algorithm.

            Sort with predecessors then reverse the sorted result.
            """
            from collections import defaultdict, deque

            # Step 1: Compute in-degree for each vertex.
            in_degree: defaultdict[DAGTask, int] = defaultdict(int)
            for node in dag.tasks:
                for neighbor in node.predecessors:
                    in_degree[neighbor] += 1

            # Step 2: Initialize a queue and add nodes with in-degree 0.
            queue: deque[DAGTask] = deque()
            for node in dag.tasks:
                if in_degree[node] == 0:
                    queue.append(node)

            # Step 3: Process nodes in the queue and update in-degrees.
            sorted_nodes: list[DAGTask] = []
            while queue:
                node = queue.popleft()
                sorted_nodes.append(node)
                for neighbor in node.predecessors:
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        queue.append(neighbor)

            # Step 4: Check for a cycle in the graph.
            if len(sorted_nodes) != len(dag.tasks):
                raise ValueError("There is a cycle in the task graph.")

            return reversed(sorted_nodes)

        mode = CreateMode[mode]
        if mode is CreateMode.or_replace:
            with suppress(NotFoundError):
                self.drop(dag)

        self.schema.tasks.create(dag._to_low_level_task())
        for dag_task in topological_sort():
            # 2023-10-04(bwarsaw) TODO: pass the enum value to mode instead of the string.
            self.schema.tasks.create(dag_task._to_low_level_task(), mode=mode)
            self.schema.tasks[dag_task.full_name].resume()
        if dag.schedule is not None:
            self.schema.tasks[dag.name].resume()

    @api_telemetry
    def run(self, dag: Union[DAG, str], *, retry_last: bool = False) -> None:
        """Execute the task graph once without waiting for the schedule.

        It resumes the task graph then executes it.

        Parameters
        __________
        retry_last: bool, optional
            Re-execute the last failed task of the task graph. Default is False.

        Examples
        ________
        Run a task graph:

        >>> dag_op.run("your-dag-name")
        """
        schedule = dag.schedule if isinstance(dag, DAG) else self.schema.tasks[dag].fetch().schedule
        if schedule is not None:
            self.schema.tasks[dag.name if isinstance(dag, DAG) else dag].resume()
        self.schema.tasks[dag.name if isinstance(dag, DAG) else dag].execute(retry_last=retry_last)

    @api_telemetry
    def get_complete_dag_runs(self, dag: Union[DAG, str], *, error_only: bool = True) -> list[DAGRun]:
        """Get the complete task graph runs within 60 minutes in Snowflake.

        Parameters
        __________
        dag: Union[DAG, str]
            The task graph to get the task graph runs.
        error_only: bool, optional
            If True, only return the task graph runs that have failed. Default is True.

        Examples
        ________
        Get the complete task graph runs:

        >>> dag_op.get_complete_dag_runs("your-dag-name")
        """
        return [
            DAGRun._from_taskrun(taskrun)
            for taskrun in self.schema.tasks[dag.name if isinstance(dag, DAG) else dag].get_complete_graphs(
                error_only=error_only
            )
        ]

    @api_telemetry
    def get_current_dag_runs(self, dag: Union[DAG, str]) -> list[DAGRun]:
        """Get the current task graph runs or next schedule dag run for next 8 days in Snowflake.

        Parameters
        __________
        dag: Union[DAG, str]
            The task graph to get the task graph runs.

        Examples
        ________
        Get the current task graph runs:

        >>> dag_op.get_current_dag_runs("your-dag-name")
        """
        return [
            DAGRun._from_taskrun(taskrun)
            for taskrun in self.schema.tasks[dag.name if isinstance(dag, DAG) else dag].get_current_graphs()
        ]

    @staticmethod
    def _extract_finalizer(task: Task) -> Optional[FQN]:
        if task.task_relations:
            identifier = json.loads(task.task_relations).get("FinalizerTask")
            return FQN.from_string(identifier) if identifier else None
        return None


__all__ = [
    "DAG",
    "DAGTask",
    "DAGRun",
    "DAGOperation"
]
