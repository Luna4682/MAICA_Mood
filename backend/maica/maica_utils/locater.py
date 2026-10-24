"""Import layer 0"""
import sys
import os
import inspect

def locater():
    """
    Gets the project/container path.
    """
    try:
        is_frozen = sys.frozen
    except Exception:
        try:
            is_frozen = locater.__compiled__
        except Exception:
            is_frozen = None

    if is_frozen:
        # Nuitka packaged: try multiple possible base paths
        absolute_path = os.path.abspath(sys.executable)
        dirname = os.path.dirname(absolute_path)

        # Try multiple possible paths to find keys directory
        possible_paths = [dirname, os.path.dirname(dirname)]

        found = False
        for path in possible_paths:
            if os.path.isdir(os.path.join(path, 'keys')):
                dirname = path
                found = True
                break

        # If keys directory not found, use executable directory
        # (for first-time key generation)
        if not found:
            dirname = possible_paths[0]
    else:
        absolute_path = os.path.abspath(inspect.getfile(locater))
        dirname = os.path.dirname(absolute_path)
        for i in range(2):
            dirname = os.path.dirname(dirname)

    return is_frozen, dirname

def get_inner_path(filename):
    frozen, base_path = locater()
    if frozen:
        filepath = os.path.join(base_path, filename)
    else:
        filepath = os.path.join(base_path, 'maica', filename)
    return filepath

def get_outer_path(filename):
    frozen, base_path = locater()
    return os.path.join(base_path, filename)

if __name__ == "__main__":
    is_frozen, self_path = locater()
    print(f"是否已被打包: {is_frozen}")
    print(f"函数所在文件绝对路径: {self_path}")