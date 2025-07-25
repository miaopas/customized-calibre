#!/usr/bin/env python


__license__   = 'GPL v3'
__copyright__ = '2011, Kovid Goyal <kovid@kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

import importlib
import os
import posixpath
import re
import sys
import threading
import zipfile
from collections import OrderedDict
from functools import partial
from importlib.machinery import ModuleSpec
from importlib.util import decode_source

from calibre import as_unicode
from calibre.customize import InvalidPlugin, Plugin, PluginNotFound, numeric_version, platform
from polyglot.builtins import itervalues, reload, string_or_bytes


def get_resources(zfp, name_or_list_of_names, print_tracebacks_for_missing_resources=True):
    '''
    Load resources from the plugin zip file

    :param name_or_list_of_names: List of paths to resources in the zip file using / as
                separator, or a single path

    :param print_tracebacks_for_missing_resources: When True missing resources are reported to STDERR

    :return: A dictionary of the form ``{name : file_contents}``. Any names
                that were not found in the zip file will not be present in the
                dictionary. If a single path is passed in the return value will
                be just the bytes of the resource or None if it wasn't found.
    '''
    names = name_or_list_of_names
    if isinstance(names, string_or_bytes):
        names = [names]
    ans = {}
    with zipfile.ZipFile(zfp) as zf:
        for name in names:
            try:
                ans[name] = zf.read(name)
            except Exception:
                if print_tracebacks_for_missing_resources:
                    print('Failed to load resource:', repr(name), 'from the plugin zip file:', zfp, file=sys.stderr)
                    import traceback
                    traceback.print_exc()
    if len(names) == 1:
        ans = ans.pop(names[0], None)

    return ans


def get_icons(zfp, name_or_list_of_names, plugin_name='', print_tracebacks_for_missing_resources=True):
    '''
    Load icons from the plugin zip file

    :param name_or_list_of_names: List of paths to resources in the zip file using / as
                separator, or a single path

    :param plugin_name: The human friendly name of the plugin, used to load icons from
                the current theme, if present.

    :param print_tracebacks_for_missing_resources: When True missing resources are reported to STDERR

    :return: A dictionary of the form ``{name : QIcon}``. Any names
                that were not found in the zip file will be null QIcons.
                If a single path is passed in the return value will
                be A QIcon.
    '''
    from qt.core import QIcon, QPixmap
    ans = {}
    namelist = [name_or_list_of_names] if isinstance(name_or_list_of_names, string_or_bytes) else name_or_list_of_names
    failed = set()
    if plugin_name:
        for name in namelist:
            q = QIcon.ic(f'{plugin_name}/{name}')
            if q.is_ok():
                ans[name] = q
            else:
                failed.add(name)
    else:
        failed = set(namelist)
    if failed:
        from_zfp = get_resources(zfp, list(failed), print_tracebacks_for_missing_resources=print_tracebacks_for_missing_resources)
        if from_zfp is None:
            from_zfp = {}
        elif isinstance(from_zfp, string_or_bytes):
            from_zfp = {namelist[0]: from_zfp}

        for name in failed:
            p = QPixmap()
            raw = from_zfp.get(name)
            if raw:
                p.loadFromData(raw)
            ans[name] = QIcon(p)
    if len(namelist) == 1 and ans:
        ans = ans.pop(namelist[0])
    return ans


_translations_cache = {}


def load_translations(namespace, zfp):
    null = object()
    trans = _translations_cache.get(zfp, null)
    if trans is None:
        return
    if trans is null:
        from calibre.utils.localization import get_lang
        lang = get_lang()
        if not lang or lang == 'en':  # performance optimization
            _translations_cache[zfp] = None
            return
        with zipfile.ZipFile(zfp) as zf:
            mo_path = zipfile.Path(zf, f'translations/{lang}.mo')
            if not mo_path.exists() and '_' in lang:
                mo_path = zipfile.Path(zf, f"translations/{lang.split('_')[0]}.mo")
            if mo_path.exists():
                mo = mo_path.read_bytes()
            else:
                _translations_cache[zfp] = None
                return

        from gettext import GNUTranslations
        from io import BytesIO
        trans = _translations_cache[zfp] = GNUTranslations(BytesIO(mo))

    namespace['_'] = trans.gettext
    namespace['ngettext'] = trans.ngettext


class CalibrePluginLoader:

    __slots__ = (
        '_is_package',
        'all_names',
        'filename',
        'fullname_in_plugin',
        'names',
        'plugin_name',
        'zip_file_path',
    )

    def __init__(self, plugin_name, fullname_in_plugin, zip_file_path, names, filename, is_package, all_names):
        self.plugin_name = plugin_name
        self.fullname_in_plugin = fullname_in_plugin
        self.zip_file_path = zip_file_path
        self.names = names
        self.filename = filename
        self._is_package = is_package
        self.all_names = all_names

    def __eq__(self, other):
        return (
            self.__class__ == other.__class__ and
            self.plugin_name == other.plugin_name and
            self.fullname_in_plugin == other.fullname_in_plugin
        )

    def get_resource_reader(self, fullname=None):
        return self

    def __hash__(self):
        return hash(self.name) ^ hash(self.plugin_name) ^ hash(self.fullname_in_plugin)

    def create_module(self, spec):
        pass

    def is_package(self, fullname):
        return self._is_package

    def get_source_as_bytes(self, fullname=None):
        src = b''
        if self.plugin_name and self.fullname_in_plugin and self.zip_file_path:
            zinfo = self.names.get(self.fullname_in_plugin)
            if zinfo is not None:
                with zipfile.ZipFile(self.zip_file_path) as zf:
                    try:
                        src = zf.read(zinfo)
                    except Exception:
                        # Maybe the zip file changed from under us
                        src = zf.read(zinfo.filename)
        return src

    def get_source(self, fullname=None):
        raw = self.get_source_as_bytes(fullname)
        return decode_source(raw)

    def get_filename(self, fullname):
        return self.filename

    def get_code(self, fullname=None):
        return compile(self.get_source_as_bytes(fullname), f'calibre_plugins.{self.plugin_name}.{self.fullname_in_plugin}',
            'exec', dont_inherit=True)

    def exec_module(self, module):
        compiled = self.get_code()
        module.__file__ = self.filename
        if self.zip_file_path:
            zfp = self.zip_file_path
            module.__dict__['get_resources'] = partial(get_resources, zfp)
            module.__dict__['get_icons'] = partial(get_icons, zfp)
            module.__dict__['load_translations'] = partial(load_translations, module.__dict__, zfp)
        exec(compiled, module.__dict__)

    def resource_path(self, name):
        raise FileNotFoundError(
            f'{name} is not available as a filesystem path in calibre plugins')

    def contents(self):
        if not self._is_package:
            return ()
        zinfo = self.names.get(self.fullname_in_plugin)
        if zinfo is None:
            return ()
        base = posixpath.dirname(zinfo.filename)
        if base:
            base += '/'

        def is_ok(x):
            if not base or x.startswith(base):
                rest = x[len(base):]
                return '/' not in rest
            return False

        return tuple(filter(is_ok, self.all_names))

    def is_resource(self, name):
        zinfo = self.names.get(self.fullname_in_plugin)
        if zinfo is None:
            return False
        base = posixpath.dirname(zinfo.filename)
        q = posixpath.join(base, name)
        return q in self.all_names

    def open_resource(self, name):
        zinfo = self.names.get(self.fullname_in_plugin)
        if zinfo is None:
            raise FileNotFoundError(f'{self.fullname_in_plugin} not in plugin zip file')
        base = posixpath.dirname(zinfo.filename)
        q = posixpath.join(base, name)
        with zipfile.ZipFile(self.zip_file_path) as zf:
            return zf.open(q)


class CalibrePluginFinder:

    def __init__(self):
        self.loaded_plugins = {}
        self._lock = threading.RLock()
        self._identifier_pat = re.compile(r'[a-zA-Z][_0-9a-zA-Z]*')

    def find_spec(self, fullname, path, target=None):
        if not fullname.startswith('calibre_plugins'):
            return
        parts = fullname.split('.')
        if parts[0] != 'calibre_plugins':
            return
        plugin_name = fullname_in_plugin = zip_file_path = filename = None
        all_names = frozenset()
        names = OrderedDict()

        if len(parts) > 1:
            plugin_name = parts[1]
            with self._lock:
                zip_file_path, names, all_names = self.loaded_plugins.get(plugin_name, (None, None, None))
            if zip_file_path is None:
                return
            fullname_in_plugin = '.'.join(parts[2:])
            if not fullname_in_plugin:
                fullname_in_plugin = '__init__'
            if fullname_in_plugin not in names:
                if fullname_in_plugin + '.__init__' in names:
                    fullname_in_plugin += '.__init__'
                else:
                    return
        is_package = bool(
            fullname.count('.') < 2 or
            fullname_in_plugin == '__init__' or
            (fullname_in_plugin and fullname_in_plugin.endswith('.__init__'))
        )
        if zip_file_path:
            filename = posixpath.join(zip_file_path, *fullname_in_plugin.split('.')) + '.py'

        return ModuleSpec(
            fullname,
            CalibrePluginLoader(plugin_name, fullname_in_plugin, zip_file_path, names, filename, is_package, all_names),
            is_package=is_package, origin=filename
        )

    def load(self, path_to_zip_file):
        if not os.access(path_to_zip_file, os.R_OK):
            raise PluginNotFound(f'Cannot access {path_to_zip_file!r}')

        with zipfile.ZipFile(path_to_zip_file) as zf:
            plugin_name = self._locate_code(zf, path_to_zip_file)

        try:
            ans = None
            plugin_module = f'calibre_plugins.{plugin_name}'
            m = sys.modules.get(plugin_module, None)
            if m is not None:
                reload(m)
            else:
                m = importlib.import_module(plugin_module)
            plugin_classes = []
            for obj in itervalues(m.__dict__):
                if isinstance(obj, type) and issubclass(obj, Plugin) and \
                        obj.name != 'Trivial Plugin':
                    plugin_classes.append(obj)
            if not plugin_classes:
                raise InvalidPlugin(f'No plugin class found in {as_unicode(path_to_zip_file)}:{plugin_name}')
            if len(plugin_classes) > 1:
                plugin_classes.sort(key=lambda c:(getattr(c, '__module__', None) or '').count('.'))

            ans = plugin_classes[0]

            if ans.minimum_calibre_version > numeric_version:
                raise InvalidPlugin(
                    'The plugin at {} needs a version of calibre >= {}'.format(as_unicode(path_to_zip_file), '.'.join(map(str,
                        ans.minimum_calibre_version))))

            if platform not in ans.supported_platforms:
                raise InvalidPlugin(
                    f'The plugin at {as_unicode(path_to_zip_file)} cannot be used on {platform}')

            return ans
        except Exception:
            with self._lock:
                del self.loaded_plugins[plugin_name]
            raise

    def _locate_code(self, zf, path_to_zip_file):
        all_names = frozenset(zf.namelist())
        names = [x[1:] if x[0] == '/' else x for x in all_names]

        plugin_name = None
        for name in names:
            name, ext = posixpath.splitext(name)
            if name.startswith('plugin-import-name-') and ext == '.txt':
                plugin_name = name.rpartition('-')[-1]

        if plugin_name is None:
            c = 0
            while True:
                c += 1
                plugin_name = f'dummy{c}'
                if plugin_name not in self.loaded_plugins:
                    break
        else:
            if self._identifier_pat.match(plugin_name) is None:
                raise InvalidPlugin(
                    f'The plugin at {path_to_zip_file!r} uses an invalid import name: {plugin_name!r}')

        pynames = [x for x in names if x.endswith('.py')]

        candidates = [posixpath.dirname(x) for x in pynames if
                x.endswith('/__init__.py')]
        candidates.sort(key=lambda x: x.count('/'))
        valid_packages = set()

        for candidate in candidates:
            parts = candidate.split('/')
            parent = '.'.join(parts[:-1])
            if parent and parent not in valid_packages:
                continue
            valid_packages.add('.'.join(parts))

        names = OrderedDict()

        for candidate in pynames:
            parts = posixpath.splitext(candidate)[0].split('/')
            package = '.'.join(parts[:-1])
            if package and package not in valid_packages:
                continue
            name = '.'.join(parts)
            names[name] = zf.getinfo(candidate)

        # Legacy plugins
        if '__init__' not in names:
            for name in tuple(names):
                if '.' not in name and name.endswith('plugin'):
                    names['__init__'] = names[name]
                    break

        if '__init__' not in names:
            raise InvalidPlugin(f'The plugin in {path_to_zip_file!r} is invalid. It does not '
                    'contain a top-level __init__.py file')

        with self._lock:
            self.loaded_plugins[plugin_name] = path_to_zip_file, names, tuple(all_names)

        return plugin_name


loader = CalibrePluginFinder()
sys.meta_path.append(loader)


if __name__ == '__main__':
    from tempfile import NamedTemporaryFile

    from calibre import CurrentDir
    from calibre.customize.ui import add_plugin
    path = sys.argv[-1]
    with NamedTemporaryFile(suffix='.zip') as f:
        with zipfile.ZipFile(f, 'w') as zf:
            with CurrentDir(path):
                for x in os.listdir('.'):
                    if x[0] != '.':
                        print('Adding', x)
                    zf.write(x)
                    if os.path.isdir(x):
                        for y in os.listdir(x):
                            zf.write(os.path.join(x, y))
        add_plugin(f.name)
        print('Added plugin from', sys.argv[-1])
