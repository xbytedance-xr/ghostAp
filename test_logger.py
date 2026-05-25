from src.utils.errors import safe_error_message


class MyError(Exception):
    pass

try:
    safe_error_message(MyError('test'))
    print("Success")
except Exception as e:
    print(f"Failed: {type(e).__name__}: {e}")
