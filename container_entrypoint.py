"""На Linux оставлять выходные файлы во владении пользователя bind mount."""
import os
from pathlib import Path
import sys

work = Path('/project/work')
if hasattr(os, 'geteuid') and os.geteuid() == 0 and work.is_dir():
    owner = work.stat()
    if owner.st_uid:
        os.setgroups([])
        os.setgid(owner.st_gid)
        os.setuid(owner.st_uid)
args = sys.argv[1:]
if not args or args[0] in ('review', 'students', 'redistribute', '--root', '--help', '-h'):
    args = ['python', '-m', 'tool', *args]
os.execvp(args[0], args)
