from src.project import ProjectContext
from src.thread import get_thread_manager, set_current_thread_id
from src.worktree_engine.manager import WorktreeManager
from src.worktree_engine.session_store import WorktreeSessionKey, WorktreeSessionStore


def test_worktree_session_store_is_scoped_by_thread_root():
    store = WorktreeSessionStore()
    key_a = WorktreeSessionKey("p1", "chat1", "thread-a")
    key_b = WorktreeSessionKey("p1", "chat1", "thread-b")

    state_a = store.get_or_create(key_a)
    state_b = store.get_or_create(key_b)

    assert state_a is store.get_or_create(key_a)
    assert state_b is store.get_or_create(key_b)
    assert state_a is not state_b


def test_worktree_manager_uses_topic_scoped_state():
    mgr = get_thread_manager()
    mgr.register("thread-a", "chat1", "p1", mode="worktree")
    mgr.register("thread-b", "chat1", "p1", mode="worktree")
    project = ProjectContext("p1", "Project", "/tmp/project")
    manager = WorktreeManager(project_manager=None)
    try:
        set_current_thread_id("thread-a")
        state_a = manager.start_selection(project, goal="A")

        set_current_thread_id("thread-b")
        state_b = manager.start_selection(project, goal="B")

        set_current_thread_id("thread-a")
        assert manager.get_state(project) is state_a
        assert manager.get_state(project).selection.pending_goal == "A"

        set_current_thread_id("thread-b")
        assert manager.get_state(project) is state_b
        assert manager.get_state(project).selection.pending_goal == "B"
    finally:
        set_current_thread_id(None)
        mgr.remove("thread-a")
        mgr.remove("thread-b")
