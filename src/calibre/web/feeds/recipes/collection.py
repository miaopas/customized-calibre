#!/usr/bin/env python


__license__   = 'GPL v3'
__copyright__ = '2009, Kovid Goyal <kovid@kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

import calendar
import json
import os
import zipfile
from collections.abc import Sequence
from datetime import timedelta
from threading import RLock
from typing import NamedTuple

from lxml import etree
from lxml.builder import ElementMaker

from calibre import force_unicode
from calibre.constants import numeric_version
from calibre.utils.date import EPOCH, UNDEFINED_DATE, isoformat, local_tz, utcnow
from calibre.utils.date import now as nowf
from calibre.utils.iso8601 import parse_iso8601
from calibre.utils.localization import _
from calibre.utils.recycle_bin import delete_file
from calibre.utils.resources import get_path as P
from calibre.utils.xml_parse import safe_xml_fromstring
from polyglot.builtins import iteritems

NS = 'http://calibre-ebook.com/recipe_collection'
E = ElementMaker(namespace=NS, nsmap={None:NS})


def iterate_over_builtin_recipe_files():
    exclude = ['craigslist', 'toronto_sun']
    d = os.path.dirname
    base = os.path.join(d(d(d(d(d(d(os.path.abspath(__file__))))))), 'recipes')
    for f in os.listdir(base):
        fbase, ext = os.path.splitext(f)
        if ext != '.recipe' or fbase in exclude:
            continue
        f = os.path.join(base, f)
        rid = os.path.splitext(os.path.relpath(f, base).replace(os.sep,
            '/'))[0]
        yield rid, f


def normalize_language(x: str) -> str:
    lang, sep, country = x.replace('-', '_').partition('_')
    if sep == '_':
        x = f'{lang.lower()}{sep}{country.upper()}'
    else:
        x = lang.lower()
    return x


def serialize_recipe(urn, recipe_class):
    from xml.sax.saxutils import quoteattr

    def attr(n, d, normalize=lambda x: x):
        ans = getattr(recipe_class, n, d)
        if isinstance(ans, bytes):
            ans = ans.decode('utf-8', 'replace')
        return quoteattr(normalize(ans))

    default_author = _('You') if urn.startswith('custom:') else _('Unknown')
    ns = getattr(recipe_class, 'needs_subscription', False)
    if not ns:
        ns = 'no'
    if ns is True:
        ns = 'yes'
    options = ''
    rso = getattr(recipe_class, 'recipe_specific_options', None)
    if rso:
        options = f' options={quoteattr(json.dumps(rso))}'
    return ('  <recipe id={id} title={title} author={author} language={language}'
            ' needs_subscription={needs_subscription} description={description}{options}/>').format(
                id=quoteattr(str(urn)),
                title=attr('title', _('Unknown')),
                author=attr('__author__', default_author),
                language=attr('language', 'und', normalize_language),
                needs_subscription=quoteattr(ns),
                description=attr('description', ''),
                options=options,
            )


def serialize_collection(mapping_of_recipe_classes):
    collection = []
    for urn in sorted(mapping_of_recipe_classes.keys(),
            key=lambda key: force_unicode(
                getattr(mapping_of_recipe_classes[key], 'title', 'zzz'),
                'utf-8')):
        try:
            recipe = serialize_recipe(urn, mapping_of_recipe_classes[urn])
        except Exception:
            import traceback
            traceback.print_exc()
            continue
        collection.append(recipe)
    items = '\n'.join(collection)
    return f'''<?xml version='1.0' encoding='utf-8'?>
<recipe_collection xmlns="http://calibre-ebook.com/recipe_collection" count="{len(collection)}">
{items}
</recipe_collection>'''.encode()


def serialize_builtin_recipes():
    from calibre.web.feeds.recipes import compile_recipe
    recipe_mapping = {}
    for rid, f in iterate_over_builtin_recipe_files():
        with open(f, 'rb') as stream:
            try:
                recipe_class = compile_recipe(stream.read())
            except Exception:
                print(f'Failed to compile: {f}')
                raise
        if recipe_class is not None:
            recipe_mapping['builtin:'+rid] = recipe_class

    return serialize_collection(recipe_mapping)


def get_builtin_recipe_collection():
    return etree.parse(P('builtin_recipes.xml', allow_user_override=False)).getroot()


def get_custom_recipe_collection(*args):
    from calibre.web.feeds.recipes import compile_recipe, custom_recipes
    bdir = os.path.dirname(custom_recipes.file_path)
    rmap = {}
    for id_, x in iteritems(custom_recipes):
        title, fname = x
        recipe = os.path.join(bdir, fname)
        try:
            with open(recipe, 'rb') as f:
                recipe = f.read().decode('utf-8')
            recipe_class = compile_recipe(recipe)
            if recipe_class is not None:
                rmap[f'custom:{id_}'] = recipe_class
        except Exception:
            print(f'Failed to load recipe from: {fname!r}')
            import traceback
            traceback.print_exc()
            continue
    return safe_xml_fromstring(serialize_collection(rmap), recover=False)


def update_custom_recipe(id_, title, script):
    update_custom_recipes([(id_, title, script)])


def update_custom_recipes(script_ids):
    from calibre.web.feeds.recipes import custom_recipe_filename, custom_recipes

    bdir = os.path.dirname(custom_recipes.file_path)
    for id_, title, script in script_ids:

        id_ = str(int(id_))
        existing = custom_recipes.get(id_, None)

        if existing is None:
            fname = custom_recipe_filename(id_, title)
        else:
            fname = existing[1]
        if isinstance(script, str):
            script = script.encode('utf-8')

        custom_recipes[id_] = (title, fname)

        if not os.path.exists(bdir):
            os.makedirs(bdir)

        with open(os.path.join(bdir, fname), 'wb') as f:
            f.write(script)


def add_custom_recipe(title, script):
    add_custom_recipes({title:script})


def add_custom_recipes(script_map):
    from calibre.web.feeds.recipes import custom_recipe_filename, custom_recipes
    id_ = 1000
    keys = tuple(map(int, custom_recipes))
    if keys:
        id_ = max(keys)+1
    bdir = os.path.dirname(custom_recipes.file_path)
    with custom_recipes:
        for title, script in iteritems(script_map):
            fid = str(id_)

            fname = custom_recipe_filename(fid, title)
            if isinstance(script, str):
                script = script.encode('utf-8')

            custom_recipes[fid] = (title, fname)

            if not os.path.exists(bdir):
                os.makedirs(bdir)

            with open(os.path.join(bdir, fname), 'wb') as f:
                f.write(script)
            id_ += 1


def remove_custom_recipe(id_):
    from calibre.web.feeds.recipes import custom_recipes
    id_ = str(int(id_))
    existing = custom_recipes.get(id_, None)
    if existing is not None:
        bdir = os.path.dirname(custom_recipes.file_path)
        fname = existing[1]
        del custom_recipes[id_]
        try:
            delete_file(os.path.join(bdir, fname))
        except Exception:
            pass


def get_custom_recipe(id_):
    from calibre.web.feeds.recipes import custom_recipes
    id_ = str(int(id_))
    existing = custom_recipes.get(id_, None)
    if existing is not None:
        bdir = os.path.dirname(custom_recipes.file_path)
        fname = existing[1]
        with open(os.path.join(bdir, fname), 'rb') as f:
            return f.read().decode('utf-8')


def get_builtin_recipe_titles():
    return [r.get('title') for r in get_builtin_recipe_collection()]


def download_builtin_recipe(urn):
    import bz2

    from calibre.utils.config_base import prefs
    from calibre.utils.https import get_https_resource_securely
    recipe_source = bz2.decompress(get_https_resource_securely(
        'https://code.calibre-ebook.com/recipe-compressed/'+urn, headers={'CALIBRE-INSTALL-UUID':prefs['installation_uuid']}))
    recipe_source = recipe_source.decode('utf-8')
    from calibre.web.feeds.recipes import compile_recipe
    recipe = compile_recipe(recipe_source)  # ensure the downloaded recipe is at least compile-able
    if recipe is None:
        raise ValueError('Failed to find recipe object in downloaded recipe: ' + urn)
    if recipe.requires_version > numeric_version:
        raise ValueError(f'Downloaded recipe for {urn} requires calibre >= {recipe.requires_version}')
    return recipe_source


def get_builtin_recipe(urn):
    with zipfile.ZipFile(P('builtin_recipes.zip', allow_user_override=False), 'r') as zf:
        return zf.read(urn+'.recipe').decode('utf-8')


def get_builtin_recipe_by_title(title, log=None, download_recipe=False):
    for x in get_builtin_recipe_collection():
        if x.get('title') == title:
            urn = x.get('id')[8:]
            if download_recipe:
                try:
                    if log is not None:
                        log('Trying to get latest version of recipe:', urn)
                    return download_builtin_recipe(urn)
                except Exception:
                    if log is None:
                        import traceback
                        traceback.print_exc()
                    else:
                        log.exception(
                        'Failed to download recipe, using builtin version')
            return get_builtin_recipe(urn)


def get_builtin_recipe_by_id(id_, log=None, download_recipe=False):
    for x in get_builtin_recipe_collection():
        if x.get('id') == id_:
            urn = x.get('id')[8:]
            if download_recipe:
                try:
                    if log is not None:
                        log('Trying to get latest version of recipe:', urn)
                    return download_builtin_recipe(urn)
                except Exception:
                    if log is None:
                        import traceback
                        traceback.print_exc()
                    else:
                        log.exception(
                        'Failed to download recipe, using builtin version')
            return get_builtin_recipe(urn)


class RecipeCustomization(NamedTuple):
    add_title_tag: bool = False
    custom_tags: Sequence[str] = ()
    keep_issues: int = 0
    recipe_specific_options: dict[str, str] | None = None


class SchedulerConfig:

    def __init__(self):
        from calibre.utils.config import config_dir
        from calibre.utils.lock import ExclusiveFile
        self.conf_path = os.path.join(config_dir, 'scheduler.xml')
        old_conf_path  = os.path.join(config_dir, 'scheduler.pickle')
        self.root = E.recipe_collection()
        self.lock = RLock()
        if os.access(self.conf_path, os.R_OK):
            with ExclusiveFile(self.conf_path) as f:
                try:
                    self.root = safe_xml_fromstring(f.read(), recover=False)
                except Exception:
                    print('Failed to read recipe scheduler config')
                    import traceback
                    traceback.print_exc()
        elif os.path.exists(old_conf_path):
            self.migrate_old_conf(old_conf_path)

    def iter_recipes(self):
        for x in self.root:
            if x.tag == f'{{{NS}}}scheduled_recipe':
                yield x

    def iter_accounts(self):
        for x in self.root:
            if x.tag == f'{{{NS}}}account_info':
                yield x

    def iter_customization(self):
        for x in self.root:
            if x.tag == f'{{{NS}}}recipe_customization':
                yield x

    def schedule_recipe(self, recipe, schedule_type, schedule, last_downloaded=None):
        with self.lock:
            for x in list(self.iter_recipes()):
                if x.get('id', False) == recipe.get('id'):
                    ld = x.get('last_downloaded', None)
                    if ld and last_downloaded is None:
                        try:
                            last_downloaded = parse_iso8601(ld)
                        except Exception:
                            pass
                    self.root.remove(x)
                    break
            if last_downloaded is None:
                last_downloaded = EPOCH
            sr = E.scheduled_recipe({
                'id': recipe.get('id'),
                'title': recipe.get('title'),
                'last_downloaded':isoformat(last_downloaded),
                }, self.serialize_schedule(schedule_type, schedule))
            self.root.append(sr)
            self.write_scheduler_file()

    # 'keep_issues' argument for recipe-specific number of copies to keep
    def customize_recipe(self, urn, val: RecipeCustomization):
        with self.lock:
            for x in list(self.iter_customization()):
                if x.get('id') == urn:
                    self.root.remove(x)
            cs = E.recipe_customization({
                'keep_issues': str(val.keep_issues),
                'id': urn,
                'add_title_tag': 'yes' if val.add_title_tag else 'no',
                'custom_tags': ','.join(val.custom_tags),
                'recipe_specific_options': json.dumps(val.recipe_specific_options or {}),
                })
            self.root.append(cs)
            self.write_scheduler_file()

    def un_schedule_recipe(self, recipe_id):
        with self.lock:
            for x in list(self.iter_recipes()):
                if x.get('id', False) == recipe_id:
                    self.root.remove(x)
                    break
            self.write_scheduler_file()

    def update_last_downloaded(self, recipe_id):
        with self.lock:
            now = utcnow()
            for x in self.iter_recipes():
                if x.get('id', False) == recipe_id:
                    typ, sch, last_downloaded = self.un_serialize_schedule(x)
                    if typ == 'interval':
                        # Prevent downloads more frequent than once an hour
                        actual_interval = now - last_downloaded
                        nominal_interval = timedelta(days=sch)
                        if abs(actual_interval - nominal_interval) < \
                                timedelta(hours=1):
                            now = last_downloaded + nominal_interval
                    x.set('last_downloaded', isoformat(now))
                    break
            self.write_scheduler_file()

    def get_to_be_downloaded_recipes(self):
        ans = []
        with self.lock:
            for recipe in self.iter_recipes():
                if self.recipe_needs_to_be_downloaded(recipe):
                    ans.append(recipe.get('id'))
        return ans

    def write_scheduler_file(self):
        from calibre.utils.lock import ExclusiveFile
        self.root.text = '\n\n\t'
        for x in self.root:
            x.tail = '\n\n\t'
        if len(self.root) > 0:
            self.root[-1].tail = '\n\n'
        with ExclusiveFile(self.conf_path) as f:
            f.seek(0)
            f.truncate()
            f.write(etree.tostring(self.root, encoding='utf-8',
                xml_declaration=True, pretty_print=False))

    def serialize_schedule(self, typ, schedule):
        s = E.schedule({'type':typ})
        if typ == 'interval':
            if schedule < 0.04:
                schedule = 0.04
            text = f'{schedule:f}'
        elif typ == 'day/time':
            text = f'{int(schedule[0])}:{int(schedule[1])}:{int(schedule[2])}'
        elif typ in ('days_of_week', 'days_of_month'):
            dw = ','.join(map(str, map(int, schedule[0])))
            text = f'{dw}:{int(schedule[1])}:{int(schedule[2])}'
        else:
            raise ValueError(f'Unknown schedule type: {typ!r}')
        s.text = text
        return s

    def un_serialize_schedule(self, recipe):
        for x in recipe.iterdescendants():
            if 'schedule' in x.tag:
                sch, typ = x.text, x.get('type')
                if typ == 'interval':
                    sch = float(sch)
                elif typ == 'day/time':
                    sch = list(map(int, sch.split(':')))
                elif typ in ('days_of_week', 'days_of_month'):
                    parts = sch.split(':')
                    days = list(map(int, [x.strip() for x in
                        parts[0].split(',')]))
                    sch = [days, int(parts[1]), int(parts[2])]
                try:
                    ld = parse_iso8601(recipe.get('last_downloaded'))
                except Exception:
                    ld = UNDEFINED_DATE
                return typ, sch, ld

    def recipe_needs_to_be_downloaded(self, recipe):
        try:
            typ, sch, ld = self.un_serialize_schedule(recipe)
        except Exception:
            return False

        def is_time(now, hour, minute):
            return now.hour > hour or \
                    (now.hour == hour and now.minute >= minute)

        def is_weekday(day, now):
            return day < 0 or day > 6 or \
                    day == calendar.weekday(now.year, now.month, now.day)

        def was_downloaded_already_today(ld_local, now):
            return ld_local.date() == now.date()

        if typ == 'interval':
            return utcnow() - ld > timedelta(sch)
        elif typ == 'day/time':
            now = nowf()
            try:
                ld_local = ld.astimezone(local_tz)
            except Exception:
                return False
            day, hour, minute = sch
            return is_weekday(day, now) and \
                    not was_downloaded_already_today(ld_local, now) and \
                    is_time(now, hour, minute)
        elif typ == 'days_of_week':
            now = nowf()
            try:
                ld_local = ld.astimezone(local_tz)
            except Exception:
                return False
            days, hour, minute = sch
            have_day = False
            for day in days:
                if is_weekday(day, now):
                    have_day = True
                    break
            return have_day and \
                    not was_downloaded_already_today(ld_local, now) and \
                    is_time(now, hour, minute)
        elif typ == 'days_of_month':
            now = nowf()
            try:
                ld_local = ld.astimezone(local_tz)
            except Exception:
                return False
            days, hour, minute = sch
            have_day = now.day in days
            return have_day and \
                    not was_downloaded_already_today(ld_local, now) and \
                    is_time(now, hour, minute)

        return False

    def set_account_info(self, urn, un, pw):
        with self.lock:
            for x in list(self.iter_accounts()):
                if x.get('id', False) == urn:
                    self.root.remove(x)
                    break
            ac = E.account_info({'id':urn, 'username':un, 'password':pw})
            self.root.append(ac)
            self.write_scheduler_file()

    def get_account_info(self, urn):
        with self.lock:
            for x in self.iter_accounts():
                if x.get('id', False) == urn:
                    return x.get('username', ''), x.get('password', '')

    def clear_account_info(self, urn):
        with self.lock:
            for x in self.iter_accounts():
                if x.get('id', False) == urn:
                    x.getparent().remove(x)
                    self.write_scheduler_file()
                    break

    def get_customize_info(self, urn):
        keep_issues = 0
        add_title_tag = True
        custom_tags = ()
        recipe_specific_options = {}
        with self.lock:
            for x in self.iter_customization():
                if x.get('id', False) == urn:
                    keep_issues = int(x.get('keep_issues', '0'))
                    add_title_tag = x.get('add_title_tag', 'yes') == 'yes'
                    custom_tags = tuple(i.strip() for i in x.get('custom_tags', '').split(','))
                    recipe_specific_options = json.loads(x.get('recipe_specific_options', '{}'))
                    break
        return RecipeCustomization(add_title_tag, custom_tags, keep_issues, recipe_specific_options)

    def get_schedule_info(self, urn):
        with self.lock:
            for x in self.iter_recipes():
                if x.get('id', False) == urn:
                    ans = list(self.un_serialize_schedule(x))
                    return ans

    def migrate_old_conf(self, old_conf_path):
        from calibre.utils.config import DynamicConfig
        c = DynamicConfig('scheduler')
        for r in c.get('scheduled_recipes', []):
            try:
                self.add_old_recipe(r)
            except Exception:
                continue
        for k in c.keys():
            if k.startswith('recipe_account_info'):
                try:
                    urn = k.replace('recipe_account_info_', '')
                    if urn.startswith('recipe_'):
                        urn = 'builtin:'+urn[7:]
                    else:
                        urn = f'custom:{int(urn)}'
                    try:
                        username, password = c[k]
                    except Exception:
                        username = password = ''
                    self.set_account_info(urn, str(username),
                            str(password))
                except Exception:
                    continue
        del c
        self.write_scheduler_file()
        try:
            os.remove(old_conf_path)
        except Exception:
            pass

    def add_old_recipe(self, r):
        urn = None
        if r['builtin'] and r['id'].startswith('recipe_'):
            urn = 'builtin:'+r['id'][7:]
        elif not r['builtin']:
            try:
                urn = 'custom:{}'.format(int(r['id']))
            except Exception:
                return
        schedule = r['schedule']
        typ = 'interval'
        if schedule > 1e5:
            typ = 'day/time'
            raw = str(int(schedule))
            day = int(raw[0]) - 1
            hour = int(raw[2:4]) - 1
            minute = int(raw[-2:]) - 1
            if day >= 7:
                day = -1
            schedule = [day, hour, minute]
        recipe = {'id':urn, 'title':r['title']}
        self.schedule_recipe(recipe, typ, schedule,
        last_downloaded=r['last_downloaded'])
