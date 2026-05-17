from __future__ import annotations

import enum
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type, TypeVar, Union, cast

T = TypeVar("T")

_SENTINEL = object()

_logger = logging.getLogger(__name__)


class ServiceLifecycle(enum.Enum):
    """服务生命周期模式。"""
    SINGLETON = "singleton"   # 首次 get 时创建，后续复用同一实例
    TRANSIENT = "transient"   # 每次 get 都创建新实例


class ServiceRegistry:
    """轻量级服务注册与依赖注入容器。

    支持单例模式与工厂模式注册，提供线程安全的延迟加载能力。
    支持 Scoped Registry (分层容器) 查找。
    支持 Transient 生命周期（每次 get 都创建新实例）。
    支持 close() 安全释放所有持有 close/cleanup 方法的单例实例。
    """

    def __init__(self, parent: Optional[ServiceRegistry] = None, name: str = "root") -> None:
        self.name = name
        self.parent = parent
        self._instances: Dict[Union[str, Type], Any] = {}
        self._factories: Dict[Union[str, Type], Callable[..., Any]] = {}
        self._transient_factories: Dict[Union[str, Type], Callable[..., Any]] = {}
        self._lock = threading.RLock()  # leaf lock: never held while acquiring a LockLevel lock
        self._closed = False

    def register_instance(self, key: Union[str, Type[T]], instance: T, override: bool = False) -> None:
        """注册一个已存在的单例对象。"""
        with self._lock:
            if not override and (key in self._instances or key in self._factories or key in self._transient_factories):
                raise ValueError(f"Service {key} already registered in registry '{self.name}'")
            self._instances[key] = instance
            self._factories.pop(key, None)
            self._transient_factories.pop(key, None)

    def register_factory(self, key: Union[str, Type[T]], factory: Callable[..., T], override: bool = False) -> None:
        """注册一个工厂函数，用于延迟加载单例对象。"""
        with self._lock:
            if not override and (key in self._instances or key in self._factories or key in self._transient_factories):
                raise ValueError(f"Service {key} already registered in registry '{self.name}'")
            self._factories[key] = factory
            self._instances.pop(key, None)
            self._transient_factories.pop(key, None)

    def register_transient(self, key: Union[str, Type[T]], factory: Callable[..., T], override: bool = False) -> None:
        """注册一个 Transient 工厂函数：每次 get 都创建新实例。"""
        with self._lock:
            if not override and (key in self._instances or key in self._factories or key in self._transient_factories):
                raise ValueError(f"Service {key} already registered in registry '{self.name}'")
            self._transient_factories[key] = factory
            self._instances.pop(key, None)
            self._factories.pop(key, None)

    def register_instance_if_absent(self, key: Union[str, Type[T]], instance: T) -> bool:
        """如果服务未注册，则注册一个已存在的单例对象。"""
        with self._lock:
            if key not in self._instances and key not in self._factories and key not in self._transient_factories:
                self._instances[key] = instance
                return True
            return False

    def register_factory_if_absent(self, key: Union[str, Type[T]], factory: Callable[..., T]) -> bool:
        """如果服务未注册，则注册一个工厂函数。"""
        with self._lock:
            if key not in self._instances and key not in self._factories and key not in self._transient_factories:
                self._factories[key] = factory
                return True
            return False

    def unregister(self, key: Union[str, Type]) -> bool:
        """取消注册一个服务（通常用于测试清理）。"""
        with self._lock:
            found = False
            if key in self._instances:
                del self._instances[key]
                found = True
            if key in self._factories:
                del self._factories[key]
                found = True
            if key in self._transient_factories:
                del self._transient_factories[key]
                found = True
            return found

    def get(self, key: Union[str, Type[T]], default: Any = _SENTINEL) -> T:
        """获取服务实例。如果是工厂注册，则在第一次调用时实例化。

        Transient 工厂每次调用都创建新实例。
        如果当前容器未找到，则递归向上在 parent 中查找。
        """
        with self._lock:
            if key in self._instances:
                return cast(T, self._instances[key])

            if key in self._factories:
                factory = self._factories[key]
                instance = factory()
                self._instances[key] = instance
                return cast(T, instance)

            if key in self._transient_factories:
                factory = self._transient_factories[key]
                return cast(T, factory())

        # 向上级容器查找
        if self.parent:
            return self.parent.get(key, default=default)

        if default is not _SENTINEL:
            return default

        raise KeyError(f"Service {key} not found in registry '{self.name}' (nor in its parents)")

    def has(self, key: Union[str, Type], local_only: bool = False) -> bool:
        """检查是否已注册该服务。"""
        with self._lock:
            exists = key in self._instances or key in self._factories or key in self._transient_factories
            if exists or local_only or not self.parent:
                return exists
            return self.parent.has(key)

    def reset(self, local_only: bool = True) -> None:
        """清理所有实例（保留工厂），常用于测试或系统重置。"""
        with self._lock:
            self._instances.clear()
            self._closed = False
            if not local_only and self.parent:
                self.parent.reset(local_only=False)

    def create_scope(self, name: str) -> ServiceRegistry:
        """创建一个子容器。"""
        return ServiceRegistry(parent=self, name=name)

    def list_services(self, local_only: bool = True) -> List[dict]:
        """列出所有已注册的服务（用于诊断）。"""
        result: List[dict] = []
        with self._lock:
            for key in self._instances:
                result.append({
                    "key": str(key),
                    "lifecycle": ServiceLifecycle.SINGLETON.value,
                    "instantiated": True,
                    "registry": self.name,
                })
            for key in self._factories:
                if key not in self._instances:
                    result.append({
                        "key": str(key),
                        "lifecycle": ServiceLifecycle.SINGLETON.value,
                        "instantiated": False,
                        "registry": self.name,
                    })
            for key in self._transient_factories:
                result.append({
                    "key": str(key),
                    "lifecycle": ServiceLifecycle.TRANSIENT.value,
                    "instantiated": False,
                    "registry": self.name,
                })
        if not local_only and self.parent:
            result.extend(self.parent.list_services(local_only=False))
        return result

    def close(self) -> None:
        """安全关闭所有持有 close/cleanup 方法的单例实例（反序释放）。

        仅关闭当前容器的实例，不触及 parent。幂等：多次调用安全。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            instances = list(reversed(list(self._instances.values())))

        for inst in instances:
            for method_name in ("close", "cleanup"):
                fn = getattr(inst, method_name, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception as e:
                        _logger.debug(
                            "ServiceRegistry[%s] %s.%s() failed: %s",
                            self.name, type(inst).__name__, method_name, e,
                        )
                    break  # 只调用第一个找到的方法

# 全局默认注册表
_default_registry = ServiceRegistry()


@dataclass(frozen=True, order=True)
class CleanupTask:
    """A task to be executed during cleanup.

    Priority: Lower values run first (default 100).
    """
    priority: int = field(default=100)
    fn: Callable[[], Any] = field(default=lambda: None, compare=False)
    name: str = field(default="unnamed_cleanup", compare=False)
    timeout: float = field(default=5.0, compare=False)


class CleanupRegistry:
    """Deterministic resource cleanup registry.

    Allows components to register cleanup functions that will be executed
    in order of priority (lower values first, then reverse registration order for same priority)
    when cleanup() is called.

    Each task has a timeout — if a cleanup function exceeds its timeout,
    execution is abandoned (best-effort) and the next task proceeds.
    """

    def __init__(self, name: str = "Global"):
        self.name = name
        self._items: list[CleanupTask] = []
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._cleaned = False

    @property
    def count(self) -> int:
        """Return the number of currently registered cleanup tasks."""
        with self._lock:
            return len(self._items)

    def register(self, name: str, cleanup_fn: Callable[[], Any], priority: int = 100, timeout: float = 5.0) -> Callable[[], None]:
        """Register a cleanup function with a name, priority and timeout.

        Returns a callable that can be used to unregister this task.
        """
        with self._lock:
            if self._cleaned:
                # If already cleaned up, run immediately (safety fallback)
                try:
                    cleanup_fn()
                except Exception:
                    _logger.debug("cleanup_fn failed during immediate execution", exc_info=True)
                return lambda: None
            task = CleanupTask(priority=priority, fn=cleanup_fn, name=name, timeout=timeout)
            self._items.append(task)

        def _unregister() -> None:
            with self._lock:
                if task in self._items:
                    self._items.remove(task)

        return _unregister

    def unregister(self, name: str) -> bool:
        """Unregister all cleanup tasks with the given name."""
        with self._lock:
            before = len(self._items)
            self._items = [t for t in self._items if t.name != name]
            return len(self._items) < before

    def cleanup(self) -> None:
        """Execute all registered cleanup functions in order of priority.

        Each task is executed with a hard timeout using a thread pool.
        Tasks that exceed their timeout are logged and skipped.
        """
        import logging
        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import TimeoutError as FutureTimeoutError

        from .errors import get_error_detail

        logger = logging.getLogger(__name__)

        with self._lock:
            if self._cleaned:
                return
            self._cleaned = True
            # Stable sort by priority. For same priority, follow reverse registration order (LIFO).
            items = sorted(reversed(self._items), key=lambda x: x.priority)
            self._items = []

        if not items:
            return

        # Use a single-thread executor for timeout enforcement
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"cleanup-{self.name}") as executor:
            for task in items:
                try:
                    future = executor.submit(task.fn)
                    future.result(timeout=task.timeout)
                except FutureTimeoutError:
                    logger.warning(
                        "CleanupRegistry[%s] %s timed out after %ss (hard timeout)",
                        self.name, task.name, task.timeout,
                    )
                    future.cancel()
                except Exception as e:
                    logger.error(
                        "CleanupRegistry[%s] failed to cleanup %s: %s",
                        self.name, task.name, get_error_detail(e),
                    )
