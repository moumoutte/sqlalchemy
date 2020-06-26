# engine/result.py
# Copyright (C) 2005-2020 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php

"""Define generic result set constructs."""


import functools
import itertools
import operator

from .row import _baserow_usecext
from .row import Row
from .. import exc
from .. import util
from ..sql.base import _generative
from ..sql.base import HasMemoized
from ..sql.base import InPlaceGenerative
from ..util import collections_abc

if _baserow_usecext:
    from sqlalchemy.cresultproxy import tuplegetter

    _row_as_tuple = tuplegetter
else:

    def tuplegetter(*indexes):
        it = operator.itemgetter(*indexes)

        if len(indexes) > 1:
            return it
        else:
            return lambda row: (it(row),)

    def _row_as_tuple(*indexes):
        # circumvent LegacyRow.__getitem__ pointing to
        # _get_by_key_impl_mapping for now.  otherwise we could
        # use itemgetter
        getters = [
            operator.methodcaller("_get_by_int_impl", index)
            for index in indexes
        ]
        return lambda rec: tuple([getter(rec) for getter in getters])


class ResultMetaData(object):
    """Base for metadata about result rows."""

    __slots__ = ()

    _tuplefilter = None
    _translated_indexes = None
    _unique_filters = None

    @property
    def keys(self):
        return RMKeyView(self)

    def _has_key(self, key):
        raise NotImplementedError()

    def _for_freeze(self):
        raise NotImplementedError()

    def _key_fallback(self, key, err, raiseerr=True):
        assert raiseerr
        util.raise_(KeyError(key), replace_context=err)

    def _warn_for_nonint(self, key):
        raise TypeError(
            "TypeError: tuple indices must be integers or slices, not %s"
            % type(key).__name__
        )

    def _index_for_key(self, keys, raiseerr):
        raise NotImplementedError()

    def _metadata_for_keys(self, key):
        raise NotImplementedError()

    def _reduce(self, keys):
        raise NotImplementedError()

    def _getter(self, key, raiseerr=True):

        index = self._index_for_key(key, raiseerr)

        if index is not None:
            return operator.itemgetter(index)
        else:
            return None

    def _row_as_tuple_getter(self, keys):
        indexes = self._indexes_for_keys(keys)
        return _row_as_tuple(*indexes)


class RMKeyView(collections_abc.KeysView):
    __slots__ = ("_parent", "_keys")

    def __init__(self, parent):
        self._parent = parent
        self._keys = [k for k in parent._keys if k is not None]

    def __len__(self):
        return len(self._keys)

    def __repr__(self):
        return "{0.__class__.__name__}({0._keys!r})".format(self)

    def __iter__(self):
        return iter(self._keys)

    def __contains__(self, item):
        if not _baserow_usecext and isinstance(item, int):
            return False

        # note this also includes special key fallback behaviors
        # which also don't seem to be tested in test_resultset right now
        return self._parent._has_key(item)

    def __eq__(self, other):
        return list(other) == list(self)

    def __ne__(self, other):
        return list(other) != list(self)


class SimpleResultMetaData(ResultMetaData):
    """result metadata for in-memory collections."""

    __slots__ = (
        "_keys",
        "_keymap",
        "_processors",
        "_tuplefilter",
        "_translated_indexes",
        "_unique_filters",
    )

    def __init__(
        self,
        keys,
        extra=None,
        _processors=None,
        _tuplefilter=None,
        _translated_indexes=None,
        _unique_filters=None,
    ):
        self._keys = list(keys)
        self._tuplefilter = _tuplefilter
        self._translated_indexes = _translated_indexes
        self._unique_filters = _unique_filters

        if extra:
            recs_names = [
                ((name,) + extras, (index, name, extras),)
                for index, (name, extras) in enumerate(zip(self._keys, extra))
            ]
        else:
            recs_names = [
                ((name,), (index, name, ()))
                for index, name in enumerate(self._keys)
            ]

        self._keymap = {key: rec for keys, rec in recs_names for key in keys}

        self._processors = _processors

    def _has_key(self, key):
        return key in self._keymap

    def _for_freeze(self):
        unique_filters = self._unique_filters
        if unique_filters and self._tuplefilter:
            unique_filters = self._tuplefilter(unique_filters)

        # TODO: are we freezing the result with or without uniqueness
        # applied?
        return SimpleResultMetaData(
            self._keys,
            extra=[self._keymap[key][2] for key in self._keys],
            _unique_filters=unique_filters,
        )

    def __getstate__(self):
        return {
            "_keys": self._keys,
            "_translated_indexes": self._translated_indexes,
        }

    def __setstate__(self, state):
        if state["_translated_indexes"]:
            _translated_indexes = state["_translated_indexes"]
            _tuplefilter = tuplegetter(*_translated_indexes)
        else:
            _translated_indexes = _tuplefilter = None
        self.__init__(
            state["_keys"],
            _translated_indexes=_translated_indexes,
            _tuplefilter=_tuplefilter,
        )

    def _contains(self, value, row):
        return value in row._data

    def _index_for_key(self, key, raiseerr=True):
        if int in key.__class__.__mro__:
            key = self._keys[key]
        try:
            rec = self._keymap[key]
        except KeyError as ke:
            rec = self._key_fallback(key, ke, raiseerr)

        return rec[0]

    def _indexes_for_keys(self, keys):
        return [self._keymap[key][0] for key in keys]

    def _metadata_for_keys(self, keys):
        for key in keys:
            if int in key.__class__.__mro__:
                key = self._keys[key]

            try:
                rec = self._keymap[key]
            except KeyError as ke:
                rec = self._key_fallback(key, ke, True)

            yield rec

    def _reduce(self, keys):
        try:
            metadata_for_keys = [
                self._keymap[
                    self._keys[key] if int in key.__class__.__mro__ else key
                ]
                for key in keys
            ]
        except KeyError as ke:
            self._key_fallback(ke.args[0], ke, True)

        indexes, new_keys, extra = zip(*metadata_for_keys)

        if self._translated_indexes:
            indexes = [self._translated_indexes[idx] for idx in indexes]

        tup = tuplegetter(*indexes)

        new_metadata = SimpleResultMetaData(
            new_keys,
            extra=extra,
            _tuplefilter=tup,
            _translated_indexes=indexes,
            _processors=self._processors,
            _unique_filters=self._unique_filters,
        )

        return new_metadata


def result_tuple(fields, extra=None):
    parent = SimpleResultMetaData(fields, extra)
    return functools.partial(
        Row, parent, parent._processors, parent._keymap, Row._default_key_style
    )


# a symbol that indicates to internal Result methods that
# "no row is returned".  We can't use None for those cases where a scalar
# filter is applied to rows.
_NO_ROW = util.symbol("NO_ROW")


class Result(InPlaceGenerative):
    """Represent a set of database results.

    .. versionadded:: 1.4  The :class:`.Result` object provides a completely
       updated usage model and calling facade for SQLAlchemy Core and
       SQLAlchemy ORM.   In Core, it forms the basis of the
       :class:`.CursorResult` object which replaces the previous
       :class:`.ResultProxy` interface.

    """

    _process_row = Row

    _row_logging_fn = None

    _source_supports_scalars = False
    _generate_rows = True
    _column_slice_filter = None
    _post_creational_filter = None
    _unique_filter_state = None
    _no_scalar_onerow = False
    _yield_per = None

    _attributes = util.immutabledict()

    def __init__(self, cursor_metadata):
        self._metadata = cursor_metadata

    def _soft_close(self, hard=False):
        raise NotImplementedError()

    def keys(self):
        """Return an iterable view which yields the string keys that would
        be represented by each :class:`.Row`.

        The view also can be tested for key containment using the Python
        ``in`` operator, which will test both for the string keys represented
        in the view, as well as for alternate keys such as column objects.

        .. versionchanged:: 1.4 a key view object is returned rather than a
           plain list.


        """
        return self._metadata.keys

    @_generative
    def yield_per(self, num):
        """Configure the row-fetching strategy to fetch num rows at a time.

        This impacts the underlying behavior of the result when iterating over
        the result object, or otherwise making use of  methods such as
        :meth:`_engine.Result.fetchone` that return one row at a time.   Data
        from the underlying cursor or other data source will be buffered up to
        this many rows in memory, and the buffered collection will then be
        yielded out one row at at time or as many rows are requested. Each time
        the buffer clears, it will be refreshed to this many rows or as many
        rows remain if fewer remain.

        The :meth:`_engine.Result.yield_per` method is generally used in
        conjunction with the
        :paramref:`_engine.Connection.execution_options.stream_results`
        execution option, which will allow the database dialect in use to make
        use of a server side cursor, if the DBAPI supports it.

        Most DBAPIs do not use server side cursors by default, which means  all
        rows will be fetched upfront from the database regardless of  the
        :meth:`_engine.Result.yield_per` setting.  However,
        :meth:`_engine.Result.yield_per` may still be useful in that it batches
        the SQLAlchemy-side processing of the raw data from the database, and
        additionally when used for ORM scenarios will batch the conversion of
        database rows into  ORM entity rows.


        .. versionadded:: 1.4

        :param num: number of rows to fetch each time the buffer is refilled.
         If set to a value below 1, fetches all rows for the next buffer.

        """
        self._yield_per = num

    @_generative
    def unique(self, strategy=None):
        """Apply unique filtering to the objects returned by this
        :class:`_engine.Result`.

        When this filter is applied with no arguments, the rows or objects
        returned will filtered such that each row is returned uniquely. The
        algorithm used to determine this uniqueness is by default the Python
        hashing identity of the whole tuple.   In some cases a specialized
        per-entity hashing scheme may be used, such as when using the ORM, a
        scheme is applied which  works against the primary key identity of
        returned objects.

        The unique filter is applied **after all other filters**, which means
        if the columns returned have been refined using a method such as the
        :meth:`_engine.Result.columns` or :meth:`_engine.Result.scalars`
        method, the uniquing is applied to **only the column or columns
        returned**.   This occurs regardless of the order in which these
        methods have been called upon the :class:`_engine.Result` object.

        The unique filter also changes the calculus used for methods like
        :meth:`_engine.Result.fetchmany` and :meth:`_engine.Result.partitions`.
        When using :meth:`_engine.Result.unique`, these methods will continue
        to yield the number of rows or objects requested, after uniquing
        has been applied.  However, this necessarily impacts the buffering
        behavior of the underlying cursor or datasource, such that multiple
        underlying calls to ``cursor.fetchmany()`` may be necessary in order
        to accumulate enough objects in order to provide a unique collection
        of the requested size.

        :param strategy: a callable that will be applied to rows or objects
         being iterated, which should return an object that represents the
         unique value of the row.   A Python ``set()`` is used to store
         these identities.   If not passed, a default uniqueness strategy
         is used which may have been assembled by the source of this
         :class:`_engine.Result` object.

        """
        self._unique_filter_state = (set(), strategy)

    @HasMemoized.memoized_attribute
    def _unique_strategy(self):
        uniques, strategy = self._unique_filter_state

        if not strategy and self._metadata._unique_filters:
            if self._source_supports_scalars:
                strategy = self._metadata._unique_filters[0]
            else:
                filters = self._metadata._unique_filters
                if self._metadata._tuplefilter:
                    filters = self._metadata._tuplefilter(filters)

                strategy = operator.methodcaller("_filter_on_values", filters)
        return uniques, strategy

    def columns(self, *col_expressions):
        r"""Establish the columns that should be returned in each row.

        This method may be used to limit the columns returned as well
        as to reorder them.   The given list of expressions are normally
        a series of integers or string key names.   They may also be
        appropriate :class:`.ColumnElement` objects which correspond to
        a given statement construct.

        E.g.::

            statement = select(table.c.x, table.c.y, table.c.z)
            result = connection.execute(statement)

            for z, y in result.columns('z', 'y'):
                # ...


        Example of using the column objects from the statement itself::

            for z, y in result.columns(
                    statement.selected_columns.c.z,
                    statement.selected_columns.c.y
            ):
                # ...

        .. versionadded:: 1.4

        :param \*col_expressions: indicates columns to be returned.  Elements
         may be integer row indexes, string column names, or appropriate
         :class:`.ColumnElement` objects corresponding to a select construct.

        :return: this :class:`_engine.Result` object with the modifications
         given.

        """
        return self._column_slices(col_expressions)

    def partitions(self, size=None):
        """Iterate through sub-lists of rows of the size given.

        Each list will be of the size given, excluding the last list to
        be yielded, which may have a small number of rows.  No empty
        lists will be yielded.

        The result object is automatically closed when the iterator
        is fully consumed.

        Note that the backend driver will usually buffer the entire result
        ahead of time unless the
        :paramref:`.Connection.execution_options.stream_results` execution
        option is used indicating that the driver should not pre-buffer
        results, if possible.   Not all drivers support this option and
        the option is silently ignored for those who do.

        .. versionadded:: 1.4

        :param size: indicate the maximum number of rows to be present
         in each list yielded.  If None, makes use of the value set by
         :meth:`_engine.Result.yield_per`, if present, otherwise uses the
         :meth:`_engine.Result.fetchmany` default which may be backend
         specific.

        :return: iterator of lists

        """
        getter = self._manyrow_getter

        while True:
            partition = getter(self, size)
            if partition:
                yield partition
            else:
                break

    def scalars(self, index=0):
        """Apply a scalars filter to returned rows.

        When this filter is applied, fetching results will return Python scalar
        objects from exactly one column of each row, rather than  :class:`.Row`
        objects or mappings.

        This filter cancels out other filters that may be established such
        as that of :meth:`_engine.Result.mappings`.

        .. versionadded:: 1.4

        :param index: integer or row key indicating the column to be fetched
         from each row, defaults to ``0`` indicating the first column.

        :return: this :class:`_engine.Result` object with modifications.

        """
        result = self._column_slices([index])
        if self._generate_rows:
            result._post_creational_filter = operator.itemgetter(0)
        result._no_scalar_onerow = True
        return result

    @_generative
    def _column_slices(self, indexes):
        if self._source_supports_scalars and len(indexes) == 1:
            self._generate_rows = False
        else:
            self._generate_rows = True
            self._metadata = self._metadata._reduce(indexes)

    def _getter(self, key, raiseerr=True):
        """return a callable that will retrieve the given key from a
        :class:`.Row`.

        """
        if self._source_supports_scalars:
            raise NotImplementedError(
                "can't use this function in 'only scalars' mode"
            )
        return self._metadata._getter(key, raiseerr)

    def _tuple_getter(self, keys):
        """return a callable that will retrieve the given keys from a
        :class:`.Row`.

        """
        if self._source_supports_scalars:
            raise NotImplementedError(
                "can't use this function in 'only scalars' mode"
            )
        return self._metadata._row_as_tuple_getter(keys)

    @_generative
    def mappings(self):
        """Apply a mappings filter to returned rows.

        When this filter is applied, fetching rows will return
        :class:`.RowMapping` objects instead of :class:`.Row` objects.

        This filter cancels out other filters that may be established such
        as that of :meth:`_engine.Result.scalars`.

        .. versionadded:: 1.4

        :return: this :class:`._engine.Result` object with modifications.
        """

        if self._source_supports_scalars:
            self._metadata = self._metadata._reduce([0])

        self._post_creational_filter = operator.attrgetter("_mapping")
        self._no_scalar_onerow = False
        self._generate_rows = True

    @HasMemoized.memoized_attribute
    def _row_getter(self):
        if self._source_supports_scalars:
            if not self._generate_rows:
                return None
            else:
                _proc = self._process_row

                def process_row(
                    metadata, processors, keymap, key_style, scalar_obj
                ):
                    return _proc(
                        metadata, processors, keymap, key_style, (scalar_obj,)
                    )

        else:
            process_row = self._process_row

        key_style = self._process_row._default_key_style
        metadata = self._metadata

        keymap = metadata._keymap
        processors = metadata._processors
        tf = metadata._tuplefilter

        if tf and not self._source_supports_scalars:
            if processors:
                processors = tf(processors)

            _make_row_orig = functools.partial(
                process_row, metadata, processors, keymap, key_style
            )

            def make_row(row):
                return _make_row_orig(tf(row))

        else:
            make_row = functools.partial(
                process_row, metadata, processors, keymap, key_style
            )

        fns = ()

        if self._row_logging_fn:
            fns = (self._row_logging_fn,)
        else:
            fns = ()

        if self._column_slice_filter:
            fns += (self._column_slice_filter,)

        if fns:
            _make_row = make_row

            def make_row(row):
                row = _make_row(row)
                for fn in fns:
                    row = fn(row)
                return row

        return make_row

    def _raw_row_iterator(self):
        """Return a safe iterator that yields raw row data.

        This is used by the :meth:`._engine.Result.merge` method
        to merge multiple compatible results together.

        """
        raise NotImplementedError()

    def freeze(self):
        """Return a callable object that will produce copies of this
        :class:`.Result` when invoked.

        The callable object returned is an instance of
        :class:`_engine.FrozenResult`.

        This is used for result set caching.  The method must be called
        on the result when it has been unconsumed, and calling the method
        will consume the result fully.   When the :class:`_engine.FrozenResult`
        is retrieved from a cache, it can be called any number of times where
        it will produce a new :class:`_engine.Result` object each time
        against its stored set of rows.

        """
        return FrozenResult(self)

    def merge(self, *others):
        """Merge this :class:`.Result` with other compatible result
        objects.

        The object returned is an instance of :class:`_engine.MergedResult`,
        which will be composed of iterators from the given result
        objects.

        The new result will use the metadata from this result object.
        The subsequent result objects must be against an identical
        set of result / cursor metadata, otherwise the behavior is
        undefined.

        """
        return MergedResult(self._metadata, (self,) + others)

    @HasMemoized.memoized_attribute
    def _iterator_getter(self):

        make_row = self._row_getter

        post_creational_filter = self._post_creational_filter

        if self._unique_filter_state:
            uniques, strategy = self._unique_strategy

            def iterrows(self):
                for row in self._fetchiter_impl():
                    obj = make_row(row) if make_row else row
                    hashed = strategy(obj) if strategy else obj
                    if hashed in uniques:
                        continue
                    uniques.add(hashed)
                    if post_creational_filter:
                        obj = post_creational_filter(obj)
                    yield obj

        else:

            def iterrows(self):
                for row in self._fetchiter_impl():
                    row = make_row(row) if make_row else row
                    if post_creational_filter:
                        row = post_creational_filter(row)
                    yield row

        return iterrows

    def _raw_all_rows(self):
        make_row = self._row_getter
        rows = self._fetchall_impl()
        return [make_row(row) for row in rows]

    def _allrows(self):

        make_row = self._row_getter

        rows = self._fetchall_impl()
        if make_row:
            made_rows = [make_row(row) for row in rows]
        else:
            made_rows = rows

        post_creational_filter = self._post_creational_filter

        if self._unique_filter_state:
            uniques, strategy = self._unique_strategy

            rows = [
                made_row
                for made_row, sig_row in [
                    (made_row, strategy(made_row) if strategy else made_row,)
                    for made_row in made_rows
                ]
                if sig_row not in uniques and not uniques.add(sig_row)
            ]
        else:
            rows = made_rows

        if post_creational_filter:
            rows = [post_creational_filter(row) for row in rows]
        return rows

    @HasMemoized.memoized_attribute
    def _onerow_getter(self):
        make_row = self._row_getter

        post_creational_filter = self._post_creational_filter

        if self._unique_filter_state:
            uniques, strategy = self._unique_strategy

            def onerow(self):
                _onerow = self._fetchone_impl
                while True:
                    row = _onerow()
                    if row is None:
                        return _NO_ROW
                    else:
                        obj = make_row(row) if make_row else row
                        hashed = strategy(obj) if strategy else obj
                        if hashed in uniques:
                            continue
                        else:
                            uniques.add(hashed)
                        if post_creational_filter:
                            obj = post_creational_filter(obj)
                        return obj

        else:

            def onerow(self):
                row = self._fetchone_impl()
                if row is None:
                    return _NO_ROW
                else:
                    row = make_row(row) if make_row else row
                    if post_creational_filter:
                        row = post_creational_filter(row)
                    return row

        return onerow

    @HasMemoized.memoized_attribute
    def _manyrow_getter(self):
        make_row = self._row_getter

        post_creational_filter = self._post_creational_filter

        if self._unique_filter_state:
            uniques, strategy = self._unique_strategy

            def filterrows(make_row, rows, strategy, uniques):
                if make_row:
                    rows = [make_row(row) for row in rows]

                if strategy:
                    made_rows = (
                        (made_row, strategy(made_row)) for made_row in rows
                    )
                else:
                    made_rows = ((made_row, made_row) for made_row in rows)
                return [
                    made_row
                    for made_row, sig_row in made_rows
                    if sig_row not in uniques and not uniques.add(sig_row)
                ]

            def manyrows(self, num):
                collect = []

                _manyrows = self._fetchmany_impl

                if num is None:
                    # if None is passed, we don't know the default
                    # manyrows number, DBAPI has this as cursor.arraysize
                    # different DBAPIs / fetch strategies may be different.
                    # do a fetch to find what the number is.  if there are
                    # only fewer rows left, then it doesn't matter.
                    if self._yield_per:
                        num_required = num = self._yield_per
                    else:
                        rows = _manyrows(num)
                        num = len(rows)
                        collect.extend(
                            filterrows(make_row, rows, strategy, uniques)
                        )
                        num_required = num - len(collect)
                else:
                    num_required = num

                while num_required:
                    rows = _manyrows(num_required)
                    if not rows:
                        break

                    collect.extend(
                        filterrows(make_row, rows, strategy, uniques)
                    )
                    num_required = num - len(collect)

                if post_creational_filter:
                    collect = [post_creational_filter(row) for row in collect]
                return collect

        else:

            def manyrows(self, num):
                if num is None:
                    num = self._yield_per

                rows = self._fetchmany_impl(num)
                if make_row:
                    rows = [make_row(row) for row in rows]
                if post_creational_filter:
                    rows = [post_creational_filter(row) for row in rows]
                return rows

        return manyrows

    def _fetchiter_impl(self):
        raise NotImplementedError()

    def _fetchone_impl(self, hard_close=False):
        raise NotImplementedError()

    def _fetchall_impl(self):
        raise NotImplementedError()

    def _fetchmany_impl(self, size=None):
        raise NotImplementedError()

    def __iter__(self):
        return self._iterator_getter(self)

    def __next__(self):
        row = self._onerow_getter(self)
        if row is _NO_ROW:
            raise StopIteration()
        else:
            return row

    next = __next__

    def fetchall(self):
        """A synonym for the :meth:`_engine.Result.all` method."""

        return self._allrows()

    def fetchone(self):
        """Fetch one row.

        When all rows are exhausted, returns None.

        .. note:: This method is not compatible with the
           :meth:`_result.Result.scalars`
           filter, as there is no way to distinguish between a data value of
           None and the ending value.   Prefer to use iterative / collection
           methods which support scalar None values.

        This method is provided for backwards compatibility with
        SQLAlchemy 1.x.x.

        To fetch the first row of a result only, use the
        :meth:`_engine.Result.first` method.  To iterate through all
        rows, iterate the :class:`_engine.Result` object directly.

        :return: a :class:`.Row` object if no filters are applied, or None
         if no rows remain.
         When filters are applied, such as :meth:`_engine.Result.mappings`
         or :meth:`._engine.Result.scalar`, different kinds of objects
         may be returned.

        """
        if self._no_scalar_onerow:
            raise exc.InvalidRequestError(
                "Can't use fetchone() when returning scalar values; there's "
                "no way to distinguish between end of results and None"
            )
        row = self._onerow_getter(self)
        if row is _NO_ROW:
            return None
        else:
            return row

    def fetchmany(self, size=None):
        """Fetch many rows.

        When all rows are exhausted, returns an empty list.

        This method is provided for backwards compatibility with
        SQLAlchemy 1.x.x.

        To fetch rows in groups, use the :meth:`._result.Result.partitions`
        method.

        :return: a list of :class:`.Row` objects if no filters are applied.
         When filters are applied, such as :meth:`_engine.Result.mappings`
         or :meth:`._engine.Result.scalar`, different kinds of objects
         may be returned.

        """
        return self._manyrow_getter(self, size)

    def all(self):
        """Return all rows in a list.

        Closes the result set after invocation.   Subsequent invocations
        will return an empty list.

        .. versionadded:: 1.4

        :return: a list of :class:`.Row` objects if no filters are applied.
         When filters are applied, such as :meth:`_engine.Result.mappings`
         or :meth:`._engine.Result.scalar`, different kinds of objects
         may be returned.

        """
        return self._allrows()

    def _only_one_row(self, raise_for_second_row, raise_for_none, scalar):
        onerow = self._fetchone_impl

        row = onerow(hard_close=True)
        if row is None:
            if raise_for_none:
                raise exc.NoResultFound(
                    "No row was found when one was required"
                )
            else:
                return None

        make_row = self._row_getter

        row = make_row(row) if make_row else row

        if raise_for_second_row:
            if self._unique_filter_state:
                # for no second row but uniqueness, need to essentially
                # consume the entire result :(
                uniques, strategy = self._unique_strategy

                existing_row_hash = strategy(row) if strategy else row

                while True:
                    next_row = onerow(hard_close=True)
                    if next_row is None:
                        next_row = _NO_ROW
                        break

                    next_row = make_row(next_row) if make_row else next_row

                    if strategy:
                        if existing_row_hash == strategy(next_row):
                            continue
                    elif row == next_row:
                        continue
                    # here, we have a row and it's different
                    break
            else:
                next_row = onerow(hard_close=True)
                if next_row is None:
                    next_row = _NO_ROW

            if next_row is not _NO_ROW:
                self._soft_close(hard=True)
                raise exc.MultipleResultsFound(
                    "Multiple rows were found when exactly one was required"
                    if raise_for_none
                    else "Multiple rows were found when one or none "
                    "was required"
                )
        else:
            next_row = _NO_ROW

        if not raise_for_second_row:
            # if we checked for second row then that would have
            # closed us :)
            self._soft_close(hard=True)

        if not scalar:
            post_creational_filter = self._post_creational_filter
            if post_creational_filter:
                row = post_creational_filter(row)

        if scalar and row:
            return row[0]
        else:
            return row

    def first(self):
        """Fetch the first row or None if no row is present.

        Closes the result set and discards remaining rows.

        .. note::  This method returns one **row**, e.g. tuple, by default.
           To return exactly one single scalar value, that is, the first
           column of the first row, use the :meth:`.Result.scalar` method,
           or combine :meth:`.Result.scalars` and :meth:`.Result.first`.

        .. comment: A warning is emitted if additional rows remain.

        :return: a :class:`.Row` object if no filters are applied, or None
         if no rows remain.
         When filters are applied, such as :meth:`_engine.Result.mappings`
         or :meth:`._engine.Result.scalars`, different kinds of objects
         may be returned.

         .. seealso::

            :meth:`_result.Result.scalar`

            :meth:`_result.Result.one`

        """
        return self._only_one_row(False, False, False)

    def one_or_none(self):
        """Return at most one result or raise an exception.

        Returns ``None`` if the result has no rows.
        Raises :class:`.MultipleResultsFound`
        if multiple rows are returned.

        .. versionadded:: 1.4

        :return: The first :class:`.Row` or None if no row is available.
         When filters are applied, such as :meth:`_engine.Result.mappings`
         or :meth:`._engine.Result.scalar`, different kinds of objects
         may be returned.

        :raises: :class:`.MultipleResultsFound`

        .. seealso::

            :meth:`_result.Result.first`

            :meth:`_result.Result.one`

        """
        return self._only_one_row(True, False, False)

    def scalar_one(self):
        """Return exactly one scalar result or raise an exception.

        This is equvalent to calling :meth:`.Result.scalars` and then
        :meth:`.Result.one`.

        .. seealso::

            :meth:`.Result.one`

            :meth:`.Result.scalars`

        """
        return self._only_one_row(True, True, True)

    def scalar_one_or_none(self):
        """Return exactly one or no scalar result.

        This is equvalent to calling :meth:`.Result.scalars` and then
        :meth:`.Result.one_or_none`.

        .. seealso::

            :meth:`.Result.one_or_none`

            :meth:`.Result.scalars`

        """
        return self._only_one_row(True, False, True)

    def one(self):
        """Return exactly one row or raise an exception.

        Raises :class:`.NoResultFound` if the result returns no
        rows, or :class:`.MultipleResultsFound` if multiple rows
        would be returned.

        .. note::  This method returns one **row**, e.g. tuple, by default.
           To return exactly one single scalar value, that is, the first
           column of the first row, use the :meth:`.Result.scalar_one` method,
           or combine :meth:`.Result.scalars` and :meth:`.Result.one`.

        .. versionadded:: 1.4

        :return: The first :class:`.Row`.
         When filters are applied, such as :meth:`_engine.Result.mappings`
         or :meth:`._engine.Result.scalar`, different kinds of objects
         may be returned.

        :raises: :class:`.MultipleResultsFound`, :class:`.NoResultFound`

        .. seealso::

            :meth:`_result.Result.first`

            :meth:`_result.Result.one_or_none`

            :meth:`_result.Result.scalar_one`

        """
        return self._only_one_row(True, True, False)

    def scalar(self):
        """Fetch the first column of the first row, and close the result set.

        Returns None if there are no rows to fetch.

        No validation is performed to test if additional rows remain.

        After calling this method, the object is fully closed,
        e.g. the :meth:`_engine.CursorResult.close`
        method will have been called.

        :return: a Python scalar value , or None if no rows remain.

        """
        return self._only_one_row(False, False, True)


class FrozenResult(object):
    """Represents a :class:`.Result` object in a "frozen" state suitable
    for caching.

    The :class:`_engine.FrozenResult` object is returned from the
    :meth:`_engine.Result.freeze` method of any :class:`_engine.Result`
    object.

    A new iterable :class:`.Result` object is generatged from a fixed
    set of data each time the :class:`.FrozenResult` is invoked as
    a callable::


        result = connection.execute(query)

        frozen = result.freeze()

        r1 = frozen()
        r2 = frozen()
        # ... etc

    .. versionadded:: 1.4

    """

    def __init__(self, result):
        self.metadata = result._metadata._for_freeze()
        self._post_creational_filter = result._post_creational_filter
        self._generate_rows = result._generate_rows
        self._source_supports_scalars = result._source_supports_scalars
        self._attributes = result._attributes
        result._post_creational_filter = None

        if self._source_supports_scalars:
            self.data = list(result._raw_row_iterator())
        else:
            self.data = result.fetchall()

    def rewrite_rows(self):
        if self._source_supports_scalars:
            return [[elem] for elem in self.data]
        else:
            return [list(row) for row in self.data]

    def with_new_rows(self, tuple_data):
        fr = FrozenResult.__new__(FrozenResult)
        fr.metadata = self.metadata
        fr._post_creational_filter = self._post_creational_filter
        fr._generate_rows = self._generate_rows
        fr._attributes = self._attributes
        fr._source_supports_scalars = self._source_supports_scalars

        if self._source_supports_scalars:
            fr.data = [d[0] for d in tuple_data]
        else:
            fr.data = tuple_data
        return fr

    def __call__(self):
        result = IteratorResult(self.metadata, iter(self.data))
        result._post_creational_filter = self._post_creational_filter
        result._generate_rows = self._generate_rows
        result._attributes = self._attributes
        result._source_supports_scalars = self._source_supports_scalars
        return result


class IteratorResult(Result):
    """A :class:`.Result` that gets data from a Python iterator of
    :class:`.Row` objects.

    .. versionadded:: 1.4

    """

    def __init__(self, cursor_metadata, iterator, raw=None):
        self._metadata = cursor_metadata
        self.iterator = iterator
        self.raw = raw

    def _soft_close(self, **kw):
        self.iterator = iter([])

    def _raw_row_iterator(self):
        return self.iterator

    def _fetchiter_impl(self):
        return self.iterator

    def _fetchone_impl(self, hard_close=False):
        row = next(self.iterator, _NO_ROW)
        if row is _NO_ROW:
            self._soft_close(hard=hard_close)
            return None
        else:
            return row

    def _fetchall_impl(self):
        try:
            return list(self.iterator)
        finally:
            self._soft_close()

    def _fetchmany_impl(self, size=None):
        return list(itertools.islice(self.iterator, 0, size))


def null_result():
    return IteratorResult(SimpleResultMetaData([]), iter([]))


class ChunkedIteratorResult(IteratorResult):
    """An :class:`.IteratorResult` that works from an iterator-producing callable.

    The given ``chunks`` argument is a function that is given a number of rows
    to return in each chunk, or ``None`` for all rows.  The function should
    then return an un-consumed iterator of lists, each list of the requested
    size.

    The function can be called at any time again, in which case it should
    continue from the same result set but adjust the chunk size as given.

    .. versionadded:: 1.4

    """

    def __init__(
        self, cursor_metadata, chunks, source_supports_scalars=False, raw=None
    ):
        self._metadata = cursor_metadata
        self.chunks = chunks
        self._source_supports_scalars = source_supports_scalars
        self.raw = raw
        self.iterator = itertools.chain.from_iterable(self.chunks(None))

    @_generative
    def yield_per(self, num):
        self._yield_per = num
        # TODO: this should raise if the iterator has already been started.
        # we can't change the yield mid-stream like this
        self.iterator = itertools.chain.from_iterable(self.chunks(num))


class MergedResult(IteratorResult):
    """A :class:`_engine.Result` that is merged from any number of
    :class:`_engine.Result` objects.

    Returned by the :meth:`_engine.Result.merge` method.

    .. versionadded:: 1.4

    """

    closed = False

    def __init__(self, cursor_metadata, results):
        self._results = results
        super(MergedResult, self).__init__(
            cursor_metadata,
            itertools.chain.from_iterable(
                r._raw_row_iterator() for r in results
            ),
        )

        self._unique_filter_state = results[0]._unique_filter_state
        self._post_creational_filter = results[0]._post_creational_filter
        self._no_scalar_onerow = results[0]._no_scalar_onerow
        self._yield_per = results[0]._yield_per

        # going to try someting w/ this in next rev
        self._source_supports_scalars = results[0]._source_supports_scalars

        self._generate_rows = results[0]._generate_rows
        self._attributes = self._attributes.merge_with(
            *[r._attributes for r in results]
        )

    def close(self):
        self._soft_close(hard=True)

    def _soft_close(self, hard=False):
        for r in self._results:
            r._soft_close(hard=hard)

        if hard:
            self.closed = True
