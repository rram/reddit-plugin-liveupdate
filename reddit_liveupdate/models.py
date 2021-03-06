import base64
import datetime
import json
import uuid

import pytz

from pylons import g
from pycassa.util import convert_uuid_to_time
from pycassa.system_manager import TIME_UUID_TYPE, UTF8_TYPE

from r2.lib.db import tdb_cassandra
from r2.lib import utils
from r2.lib.memoize import memoize


class LiveUpdateEvent(tdb_cassandra.Thing):
    _editor_prefix = "editor_"

    _use_db = True
    _read_consistency_level = tdb_cassandra.CL.ONE
    _write_consistency_level = tdb_cassandra.CL.QUORUM
    _defaults = {
        "description": "",
        "timezone": "UTC",
        # one of "live", "complete"
        "state": "live",
    }

    @classmethod
    def _editor_key(cls, user):
        return "%s%s" % (cls._editor_prefix, user._id36)

    def add_editor(self, user):
        self[self._editor_key(user)] = ""
        self._commit()

    def remove_editor(self, user):
        del self[self._editor_key(user)]
        self._commit()

    def is_editor(self, user):
        return self._editor_key(user) in self._t

    @property
    def _fullname(self):
        return self._id

    @property
    def editor_ids(self):
        return [int(k[len(self._editor_prefix):], 36)
                for k in self._t.iterkeys()
                if k.startswith(self._editor_prefix)]

    @classmethod
    def new(cls, id, title, **properties):
        if not id:
            id = base64.b32encode(uuid.uuid1().bytes).rstrip("=").lower()
        event = cls(id, title=title, **properties)
        event._commit()
        return event


class LiveUpdateStream(tdb_cassandra.View):
    _use_db = True
    _connection_pool = "main"
    _compare_with = TIME_UUID_TYPE
    _read_consistency_level = tdb_cassandra.CL.ONE
    _write_consistency_level = tdb_cassandra.CL.QUORUM
    _extra_schema_creation_args = {
        "default_validation_class": UTF8_TYPE,
    }

    @classmethod
    def add_update(cls, event, update):
        columns = cls._obj_to_column(update)
        cls._set_values(event._id, columns)

    @classmethod
    def get_update(cls, event, id):
        thing = cls._byID(event._id, properties=[id])

        try:
            data = thing._t[id]
        except KeyError:
            raise tdb_cassandra.NotFound, "<LiveUpdate %s>" % id
        else:
            return LiveUpdate.from_json(id, data)

    @classmethod
    def _obj_to_column(cls, entries):
        entries, is_single = utils.tup(entries, ret_is_single=True)
        columns = [{entry._id: entry.to_json()} for entry in entries]
        return columns[0] if is_single else columns

    @classmethod
    def _column_to_obj(cls, columns):
        # columns = [{colname: colvalue}]
        return [LiveUpdate.from_json(*column.popitem())
                for column in utils.tup(columns)]


class LiveUpdate(object):
    __slots__ = ("_id", "_data")
    defaults = {
        "deleted": False,
        "stricken": False,
    }

    def __init__(self, id=None, data=None):
        if not id:
            id = uuid.uuid1()
        self._id = id
        self._data = data or {}

    def __getattr__(self, name):
        try:
            return self._data[name]
        except KeyError:
            try:
                return LiveUpdate.defaults[name]
            except KeyError:
                raise AttributeError, name

    def __setattr__(self, name, value):
        if name in self.__slots__:
            object.__setattr__(self, name, value)
        else:
            self._data[name] = value

    def to_json(self):
        return json.dumps(self._data)

    @classmethod
    def from_json(cls, id, value):
        return cls(id, json.loads(value))

    @property
    def _date(self):
        timestamp = convert_uuid_to_time(self._id)
        return datetime.datetime.fromtimestamp(timestamp, pytz.UTC)

    @property
    def _fullname(self):
        return "%s_%s" % (self.__class__.__name__, self._id)


class ActiveVisitorsByLiveUpdateEvent(tdb_cassandra.View):
    _use_db = True
    _connection_pool = 'main'
    _ttl = datetime.timedelta(minutes=15)

    _extra_schema_creation_args = dict(
        key_validation_class=tdb_cassandra.ASCII_TYPE,
    )

    _read_consistency_level  = tdb_cassandra.CL.ONE
    _write_consistency_level = tdb_cassandra.CL.ANY

    @classmethod
    def touch(cls, event_id, hash):
        cls._set_values(event_id, {hash: ''})

    @classmethod
    def get_count(cls, event_id, cached=True, fuzz=True):
        count = cls._get_count_cached(event_id, _update=not cached)

        if fuzz and count < 100:
            cache_key = "liveupdate_visitors-" + str(event_id)
            fuzzed_count = g.cache.get(cache_key)
            if not fuzzed_count:
                fuzzed_count = utils.fuzz_activity(count)
                g.cache.set(cache_key, fuzzed_count, time=5 * 60)
            return fuzzed_count, True
        return count, False

    @classmethod
    @memoize('lu-active', time=60)
    def _get_count_cached(cls, event_id):
        return cls._cf.get_count(event_id)
