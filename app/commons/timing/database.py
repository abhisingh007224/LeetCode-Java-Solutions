import contextlib
import functools
import sys
import structlog

from enum import Enum
from types import FrameType, TracebackType
from typing import Tuple, Any, Callable, List, Dict, Optional, Type
from typing_extensions import Literal, Protocol, runtime_checkable
from doordash_python_stats import ddstats

from app.commons import tracing
from app.commons.timing.base import format_exc_name
from app.commons.context.logger import root_logger as default_logger
from app.commons.stats import get_service_stats_client, get_request_logger

import asyncio
import concurrent.futures

try:
    from psycopg2.errors import QueryCanceledError
except ImportError:
    from psycopg2.extensions import QueryCanceledError


@runtime_checkable
class Database(Protocol):
    database_name: str
    instance_name: str


@runtime_checkable
class TrackedTransaction(Protocol):
    tracker: Optional["QueryTimer"]
    stack: Optional[contextlib.ExitStack]


class QueryStatus(str, Enum):
    """
    Request Status categories for Database Queries
    """

    success = "success"
    timeout = "timeout"
    error = "error"


class QueryType(str, Enum):
    unknown = ""
    select = "select"
    insert = "insert"
    update = "update"
    delete = "delete"
    transaction = "transaction"


class TransactionStatus(str, Enum):
    commit = "commit"
    rollback = "rollback"
    error = "error"


def _discover_caller(additional_ignores=None) -> Tuple[FrameType, str]:
    """
    Remove all app.commons.tracing calls and return the relevant app frame.

    (borrowed from "structlog._frames")

    :param additional_ignores: Additional names with which the first frame must
        not start.
    :type additional_ignores: `list` of `str` or `None`

    :rtype: tuple of (frame, name)
    """
    ignores = [
        # current module
        __name__,
        # tracing module
        "app.commons.tracing",
        # context managers
        "contextlib",
    ] + (additional_ignores or [])
    f = sys._getframe()
    name = f.f_globals.get("__name__") or "?"
    while any(tuple(name.startswith(i) for i in ignores)):
        if f.f_back is None:
            name = "?"
            break
        f = f.f_back
        name = f.f_globals.get("__name__") or "?"
    return f, name


class QueryTimer(tracing.BaseTimer):
    database: Database
    calling_module_name: str
    calling_function_name: str
    stack_frame: Any = None

    request_status: str = QueryStatus.success
    exception_name: str = ""

    def __init__(
        self,
        database: Database,
        *,
        calling_function_name: str = "",
        calling_module_name: str = "",
        additional_ignores=None,
    ):
        super().__init__()
        self.database = database
        self.additional_ignores = additional_ignores
        self.calling_function_name = calling_function_name
        self.calling_module_name = calling_module_name

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> Optional[Literal[False]]:
        # ensure we get timing information
        super().__exit__(exc_type, exc_value, traceback)

        # no error, request is successful
        if exc_type is None:
            return False

        # record exception info
        self.exception_name = format_exc_name(exc_type)

        # don't override the request status if it's already set
        if self.request_status and self.request_status != QueryStatus.success:
            return False

        if isinstance(
            # known timeout errors
            exc_value,
            (
                asyncio.TimeoutError,
                asyncio.CancelledError,
                concurrent.futures.TimeoutError,
                QueryCanceledError,
            ),
        ):
            self.request_status = QueryStatus.timeout
        else:
            # something else
            self.request_status = QueryStatus.error

        return False


class ExecuteTimer(QueryTimer):
    """
    query timer with auto-discovery of calling function
    """

    def __enter__(self):
        super().__enter__()
        self.stack_frame, self.calling_module_name = _discover_caller(
            additional_ignores=self.additional_ignores
        )
        self.calling_function_name = self.stack_frame.f_code.co_name
        return self


class TransactionTimer(ExecuteTimer):
    """
    query timer with process_result that lists the operation type
    """

    request_status: str = TransactionStatus.commit

    def process_result(self, result: TransactionStatus):
        self.request_status = result
        return super().process_result(result)


def track_execute(func):
    """
    track the execution of a query, looking up the name from the calling function
    """
    return ExecuteTimingManager(message="query complete")(func)


def track_query(func: Callable):
    """
    track the execution of a query, looking up the name from the wrapped function
    """
    calling_function_name = func.__name__
    calling_module_name = func.__module__
    return QueryTimingManager(
        message="query complete",
        calling_module_name=calling_module_name,
        calling_function_name=calling_function_name,
    )(func)


def log_query_timing(tracker: "QueryTimingManager", timer: QueryTimer):
    if not isinstance(timer.database, Database):
        return
    log: structlog.stdlib.BoundLogger = get_request_logger(default=default_logger)
    breadcrumb = tracing.get_current_breadcrumb()

    # these are not currently available through context
    database = {
        "database_name": timer.database.database_name,
        "instance_name": timer.database.instance_name,
        "transaction": bool(breadcrumb.transaction_name),
        "transaction_name": breadcrumb.transaction_name,
        "request_status": timer.request_status,
    }
    caller = breadcrumb.dict(
        include={"application_name", "processor_name", "repository_name"},
        skip_defaults=True,
    )
    if timer.calling_module_name:
        caller["module_name"] = timer.calling_module_name
    if timer.exception_name:
        database["exception_name"] = timer.exception_name

    log.info(
        tracker.message,
        query=timer.calling_function_name,
        latency_ms=round(timer.delta_ms, 3),
        database=database,
        caller=caller,
    )


def log_transaction_timing(tracker: "QueryTimingManager", timer: QueryTimer):
    if not isinstance(timer.database, Database):
        return
    log: structlog.stdlib.BoundLogger = get_request_logger(default=default_logger)
    breadcrumb = tracing.get_current_breadcrumb()

    # these are not currently available through context
    database = {
        "database_name": timer.database.database_name,
        "instance_name": timer.database.instance_name,
        "request_status": timer.request_status,
    }

    caller = breadcrumb.dict(
        include={"application_name", "processor_name", "repository_name"},
        skip_defaults=True,
    )
    if timer.calling_module_name:
        caller["module_name"] = timer.calling_module_name
    if timer.exception_name:
        database["exception_name"] = timer.exception_name

    log.info(
        tracker.message,
        transaction=timer.calling_function_name,
        latency_ms=round(timer.delta_ms, 3),
        database=database,
        caller=caller,
    )


def _stat_query_timing(
    tracker: "QueryTimingManager", timer: QueryTimer, *, query_type=""
):
    if not isinstance(timer.database, Database):
        return

    stats: ddstats.DoorStatsProxyMultiServer = get_service_stats_client()
    log: structlog.stdlib.BoundLogger = get_request_logger(default=default_logger)
    breadcrumb = tracing.get_current_breadcrumb()

    stat_name = "io.db.latency"
    tags = breadcrumb.dict(
        include={
            "application_name",
            # "database_name",
            # "instance_name",
            "transaction_name",
        },
        skip_defaults=True,
    )
    if query_type:
        tags["query_type"] = query_type

    if timer.calling_function_name:
        tags["query_name"] = timer.calling_function_name

    # not yet available in breadcrumbs
    if timer.database.database_name:
        tags["database_name"] = timer.database.database_name
    if timer.database.instance_name:
        tags["instance_name"] = timer.database.instance_name

    if timer.request_status:
        tags["request_status"] = timer.request_status

    # emit stats
    stats.timing(stat_name, timer.delta_ms, tags=tags)
    log.debug("statsd: %s", stat_name, latency_ms=timer.delta_ms, tags=tags)


def stat_query_timing(tracker: "QueryTimingManager", timer: QueryTimer):
    _stat_query_timing(tracker, timer)


def stat_transaction_timing(tracker: "QueryTimingManager", timer: QueryTimer):
    _stat_query_timing(tracker, timer, query_type="transaction")


class QueryTimingManager(tracing.TimingManager[QueryTimer]):
    """
    Tracker for database queries and transactions
    """

    message: str

    log: structlog.stdlib.BoundLogger
    stats: ddstats.DoorStatsProxyMultiServer

    # database query processors
    processors = [log_query_timing, stat_query_timing]
    additional_ignores: List[str]
    calling_module_name: str
    calling_function_name: str

    def __init__(
        self,
        *,
        message: str,
        calling_function_name: str = "",
        calling_module_name: str = "",
    ):
        super().__init__()
        self.message = message
        self.additional_ignores = ["app.commons.database"]
        self.calling_module_name = calling_module_name
        self.calling_function_name = calling_function_name

    def create_tracker(
        self,
        obj=tracing.Unspecified,
        *,
        func: Callable,
        args: List[Any],
        kwargs: Dict[str, Any],
    ) -> QueryTimer:
        return QueryTimer(
            database=obj,
            additional_ignores=self.additional_ignores,
            calling_module_name=self.calling_module_name,
            calling_function_name=self.calling_function_name,
        )

    def __call__(self, func_or_class):
        # NOTE: we assume that this is only called to decorate a class
        return self._decorate_class_method(func_or_class)


class ExecuteTimingManager(QueryTimingManager):
    def create_tracker(
        self,
        obj=tracing.Unspecified,
        *,
        func: Callable,
        args: List[Any],
        kwargs: Dict[str, Any],
    ) -> QueryTimer:
        return ExecuteTimer(database=obj, additional_ignores=self.additional_ignores)


class TransactionTimingManager(ExecuteTimingManager):
    # database transaction processors
    processors = [log_transaction_timing, stat_transaction_timing]
    """
    decorators for transactions
    - track_start
    - track_commit
    - track_rollback
    """

    def __init__(self, *, message: str):
        super().__init__(message=message)

    def create_tracker(
        self,
        obj=tracing.Unspecified,
        *,
        func: Callable,
        args: List[Any],
        kwargs: Dict[str, Any],
    ) -> QueryTimer:
        return TransactionTimer(
            database=obj,
            additional_ignores=self.additional_ignores,
            calling_module_name=self.calling_module_name,
            calling_function_name=self.calling_function_name,
        )

    def start(self, *, obj: TrackedTransaction, func, args, kwargs) -> QueryTimer:
        obj.stack = contextlib.ExitStack()
        obj.stack.__enter__()
        obj.tracker = self._start_tracker(
            obj.stack, obj=obj, func=func, args=args, kwargs=kwargs
        )
        # tracing: set transaction name
        obj.stack.enter_context(
            tracing.breadcrumb_ctxt_manager(
                tracing.Breadcrumb(transaction_name=obj.tracker.calling_function_name),
                # the contextmanager will not be awaited in the same task
                restore=False,
            )
        )
        return obj.tracker

    def commit(self, *, obj: TrackedTransaction):
        # process result
        if obj.tracker is not None:
            self._exit_tracker(obj.tracker, "commit")
        # exit stack
        if obj.stack is not None:
            obj.stack.__exit__(None, None, None)

    def rollback(
        self,
        exc_type: Optional[Type[BaseException]] = None,
        exc_value: Optional[BaseException] = None,
        traceback: Optional[TracebackType] = None,
        *,
        obj: TrackedTransaction,
    ):
        # process result
        if obj.tracker is not None:
            self._exit_tracker(obj.tracker, "rollback")
        # exit stack
        if obj.stack is not None:
            obj.stack.__exit__(exc_type, exc_value, traceback)

    def error(
        self,
        exc_type: Optional[Type[BaseException]] = None,
        exc_value: Optional[BaseException] = None,
        traceback: Optional[TracebackType] = None,
        *,
        obj: TrackedTransaction,
    ):
        # no result to process
        if obj.tracker is not None:
            self._exit_tracker(obj.tracker, "error")
        # exit stack
        if obj.stack is not None:
            obj.stack.__exit__(exc_type, exc_value, traceback)

    def track_start(self, func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            obj = args[0]
            if isinstance(obj, TrackedTransaction):
                self.start(obj=obj, func=func, args=args, kwargs=kwargs)
            try:
                return await func(*args, **kwargs)
            except:
                # error starting the transaction
                exc_type, exc_val, exc_tb = sys.exc_info()
                self.error(exc_type, exc_val, exc_tb, obj=obj)
                raise

        return async_wrapper

    def track_commit(self, func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            try:
                result = await func(*args, **kwargs)
                # successful commit
                obj = args[0]
                if isinstance(obj, TrackedTransaction):
                    self.commit(obj=obj)
                return result
            except:
                # error executing the commit itself
                obj = args[0]
                exc_type, exc_val, exc_tb = sys.exc_info()
                self.error(exc_type, exc_val, exc_tb, obj=obj)
                raise

        return async_wrapper

    def track_rollback(self, func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            try:
                result = await func(*args, **kwargs)
                # successful rollback
                obj, *exc_args = args
                if isinstance(obj, TrackedTransaction):
                    self.rollback(*exc_args, obj=obj)
                return result
            except:
                # error executing the rollback itself
                obj = args[0]
                exc_type, exc_val, exc_tb = sys.exc_info()
                self.error(exc_type, exc_val, exc_tb, obj=obj)
                raise

        return async_wrapper

    def __call__(self, func):
        raise NotImplementedError()
