from pathlib import Path
import re
from caflib.Logging import error, info


class Template:
    _cache = {}

    def __init__(self, path):
        self.path = Path(path)
        if self.path not in Template._cache:
            try:
                Template._cache[self.path] = self.path.open().read()
                info('Loading template "{}"'.format(self.path))
            except FileNotFoundError:
                error('Template "{}" does not exist'.format(path))

    def substitute(self, mapping):
        used = set()

        def replacer(m):
            key = m.group(1)
            if key not in mapping:
                raise RuntimeError('"{}" not defined'.format(key))
            else:
                used.add(key)
                return str(mapping[key])

        replaced = re.sub(r'\{\{\s*(\w+)\s*\}\}',
                          replacer,
                          Template._cache[self.path])
        return replaced, used
