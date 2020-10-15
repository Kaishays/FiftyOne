"""
Video frames.

| Copyright 2017-2020, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""
from collections import defaultdict
from copy import deepcopy
import json
import weakref
import six

from bson import ObjectId, json_util
from pymongo import ReplaceOne

import fiftyone as fo
from fiftyone.core.document import Document
import fiftyone.core.frame_utils as fofu
from fiftyone.core.labels import _FrameLabels
from fiftyone.core.odm.frame import (
    NoDatasetFrameSampleDocument,
    DatasetFrameSampleDocument,
)
import fiftyone.core.utils as fou

#
# This class is instantiated automatically and depends on an owning
# :class:`fiftyone.core.sample.Sample`. Not for independent use or direct
# assignment to :class:`fiftyone.core.sample.Sample`s.
#
class Frames(object):
    """An ordered dictionary of labels for the frames of a
    :class:`fiftyone.core.sample.Sample`.

    The frame labels are stored in :class:`Frame` instances.

    :class:`Frames` instances behave like ``defaultdict(Frame)`` instances; an
    empty :class:`Frame` instance is returned when accessing a new frame
    number.
    """

    def __init__(self):
        self._sample = None
        self._iter = None
        self._iter_doc = None
        self._replacements = {}

    def __str__(self):
        return "<%s: %s>" % (self.__class__.__name__, fou.pformat(dict(self)))

    def __repr__(self):
        num_frames = len(self)
        plural = "s" if num_frames != 1 else ""
        return "{ <%d frame%s> }" % (num_frames, plural)

    def __len__(self):
        return self._sample._doc.frames["frame_count"]

    def _set_replacement(self, doc):
        self._replacements[doc.get_field("frame_number")] = doc

    def _save_replacements(self):
        if not self._replacements:
            return

        self._frame_collection.bulk_write(
            [
                ReplaceOne(
                    self._make_filter(frame_number, doc),
                    self._make_dict(doc),
                    upsert=True,
                )
                for frame_number, doc in self._replacements.items()
            ],
            ordered=False,
        )
        self._replacements = {}

    def _make_filter(self, frame_number, doc):
        doc._sample_id = self._sample._id
        return {
            "frame_number": frame_number,
            "_sample_id": self._sample._id,
        }

    def _make_dict(self, doc):
        d = doc.to_dict(extended=False)
        d.pop("_id", None)
        d["_sample_id"] = self._sample._id
        return d

    def __iter__(self):
        self._iter = self.keys()
        return self

    def __next__(self):
        try:
            return next(self._iter)
        except StopIteration:
            self._iter = None
            self._iter_doc = None
            raise

    def __contains__(self, frame_number):
        if frame_number in self._replacements:
            return True

        if self._sample._in_db:
            find_d = self._make_filter(frame_number, self._sample._doc)
            return self._frame_collection.find_one(find_d) is not None

        return False

    def __getitem__(self, frame_number):
        fofu.validate_frame_number(frame_number)

        dataset = None
        doc = None
        d = None
        default_d = {
            "_sample_id": self._sample._id,
            "frame_number": frame_number,
        }
        if (
            self._iter is not None
            and frame_number == self._iter_doc.get_field("frame_number")
        ):
            doc = self._iter_doc
        elif frame_number in self._replacements:
            doc = self._replacements[frame_number]
        elif self._sample._in_db:
            d = self._frame_collection.find_one(default_d)
            if d is None:
                d = default_d
                self._sample._doc.frames["frame_count"] += 1
            doc = self._sample._dataset._frame_dict_to_doc(d)
            self._set_replacement(doc)
        else:
            doc = NoDatasetFrameSampleDocument(**default_d)
            self._sample._doc.frames["frame_count"] += 1
            self._set_replacement(doc)

        if self._sample._in_db:
            dataset = self._sample._dataset

        return Frame.from_doc(doc, dataset=dataset)

    def __setitem__(self, frame_number, frame):
        fofu.validate_frame_number(frame_number)

        if not isinstance(frame, Frame):
            raise ValueError("Value must be a %s" % Frame.__name__)

        doc = frame._doc
        doc.set_field("frame_number", frame_number)
        doc._sample_id = self._sample._id

        if not self._sample._in_db and frame_number not in self._replacements:
            self._sample._doc.frames["frame_count"] += 1
        elif self._sample._in_db:
            if (
                self._iter is not None
                or frame_number != self._iter_doc.get_field("frame_number")
            ):
                find_d = {
                    "_sample_id": self._sample._id,
                    "frame_number": frame_number,
                }
                exists = self._frame_collection.find(find_d)
                if exists is None:
                    self._sample._doc.frames["frame_count"] += 1

        self._set_replacement(doc)

    @property
    def field_names(self):
        """An ordered tuple of the names of the fields on the frames."""
        try:
            frame = next(self.values())
            return frame.field_names
        except:
            return ("frame_number",)

    def keys(self):
        """Returns an iterator over the frame numbers with labels in the
        sample.

        The frames are traversed in ascending order.

        Returns:
            a generator that emits frame numbers
        """
        for doc in self._iter_docs():
            yield doc.get_field("frame_number")

    def items(self):
        """Returns an iterator over the frame numberes and :class:`Frame`
        instances for the sample.

        The frames are traversed in ascending order.

        Returns:
            a generator that emits ``(frame_number, Frame)`` tuples
        """
        for doc in self._iter_docs():
            yield doc.frame_number, Frame.from_doc(
                doc, dataset=self._sample._dataset
            )

    def values(self):
        """Returns an iterator over the :class:`Frame` instances for the
        sample.

        The frames are traversed in ascending order.

        Returns:
            a generator that emits :class:`Frame` instances
        """
        dataset = self._sample._dataset if self._sample._in_db else None
        for doc in self._iter_docs():
            yield Frame.from_doc(doc, dataset=dataset)

    def update(self, frames, overwrite=True):
        """Adds the frame labels to this instance.

        Args:
            frames: can be any of the following

                -   a :class:`Frames` instance
                -   a dictionary mapping frame numbers to :class:`Frame`
                    instances
                -   a dictionary mapping frame numbers to dictionaries mapping
                    label fields to :class:`fiftyone.core.labels.Label`
                    instances

            overwrite (True): whether to overwrite existing frames
        """
        for frame_number, frame in frames.items():
            if overwrite or frame_number not in self:
                if isinstance(frame, dict):
                    frame = Frame(frame_number=frame_number, **frame)

                self[frame_number] = frame

    def merge(self, frames, overwrite=True):
        """Merges the frame labels into this instance.

        Args:
            frames: can be any of the following

                -   a :class:`Frames` instance
                -   a dictionary mapping frame numbers to :class:`Frame`
                    instances
                -   a dictionary mapping frame numbers to dictionaries mapping
                    label fields to :class:`fiftyone.core.labels.Label`
                    instances

            overwrite (True): whether to overwrite existing fields
        """
        for frame_number, frame in frames.items():
            if isinstance(frame, dict):
                frame = Frame(frame_number=frame_number, **frame)

            if frame_number in self:
                self[frame_number].merge(frame, overwrite=overwrite)
            else:
                self[frame_number] = frame

    @property
    def _first_frame(self):
        first_frame = self._replacements.get(1, None)
        if first_frame is None and self._sample._in_db:
            first_frame = self._sample._doc.frames["first_frame"]

        return first_frame

    def to_mongo_dict(self):
        first_frame = self._first_frame
        return {
            "frame_count": self._sample._doc.frames["frame_count"],
            "first_frame": self._first_frame,
        }

    @property
    def _frame_collection(self):
        return self._sample._dataset._frame_collection

    def _iter_docs(self):
        if self._sample._in_db:
            repl_fns = sorted(self._replacements.keys())
            repl_fn = repl_fns[0] if len(repl_fns) else None
            find_d = {"_sample_id": self._sample._id}
            for d in self._frame_collection.find(find_d):
                if repl_fn is not None and d["frame_number"] >= repl_fn:
                    self._iter_doc = self._replacements[repl_fn]
                    repl_fn += 1
                else:
                    self._iter_doc = self._sample._dataset._frame_dict_to_doc(
                        d
                    )
                self._set_replacement(self._iter_doc)
                yield self._iter_doc
        else:
            for frame_number in sorted(self._replacements.keys()):
                self._iter_doc = self._replacements[frame_number]
                self._set_replacement(self._iter_doc)
                yield self._iter_doc

    def _get_field_cls(self):
        return self._sample._doc.frames.__class__

    def _get_first_frame(self):
        if 1 in self._replacements:
            d = self._make_dict(self._replacements[1])
            d.pop("_sample_id")
            return d
        return None

    def _save(self):
        if not self._sample._in_db:
            raise fofu.FrameError(
                "Sample does not have a dataset, Frames cannot be saved"
            )

        d = self._get_first_frame()
        if d is not None:
            for k, v in d.items():
                if isinstance(v, dict):
                    if "_cls" in v:
                        # Serialized embedded document
                        _cls = getattr(fo, v["_cls"])
                        d[k] = _cls.from_dict(v)
                    elif "$binary" in v:
                        # Serialized array in extended format
                        binary = json_util.loads(json.dumps(v))
                        d[k] = fou.deserialize_numpy_array(binary)
                    else:
                        d[k] = v
                elif isinstance(v, six.binary_type):
                    # Serialized array in non-extended format
                    d[k] = fou.deserialize_numpy_array(v)
                else:
                    d[k] = v

            self._sample._doc.frames.first_frame = _FrameLabels(**d)

        self._save_replacements()

    def _serve(self, sample):
        self._sample = sample
        return self


class Frame(Document):
    """A frame in a video :class:`fiftyone.core.sample.Sample`.

    :class:`Frame` instances can hold any :class:`fiftyone.core.label.Label`
    or other :class:`fiftyone.core.fields.Field` type that can be assigned
    directly to :class:`fiftyone.core.sample.Sample` instances.
    """

    # Instance references keyed by [collection_name][_sample_id]
    _instances = defaultdict(lambda: defaultdict(weakref.WeakValueDictionary))

    _COLL_CLS = DatasetFrameSampleDocument
    _NO_COLL_CLS = NoDatasetFrameSampleDocument

    def __init__(self, **kwargs):
        self._doc = NoDatasetFrameSampleDocument(**kwargs)
        super().__init__()

    def __str__(self):
        return repr(self)

    def __repr__(self):
        return self._doc.fancy_repr(class_name=self.__class__.__name__)

    def __getitem__(self, field_name):
        try:
            return self.get_field(field_name)
        except AttributeError:
            raise KeyError(
                "%s has no field '%s'" % (self.__class__.__name__, field_name)
            )

    def __setitem__(self, field_name, value):
        self.set_field(field_name, value=value)

    @property
    def _skip_iter_field_names(self):
        return ("frame_number",)

    def copy(self):
        """Returns a deep copy of the frame that has not been added to the
        database.

        Returns:
            a :class:`Frame`
        """
        kwargs = {k: deepcopy(v) for k, v in self.iter_fields()}
        kwargs["frame_number"] = self.frame_number
        return self.__class__(**kwargs)

    @classmethod
    def from_doc(cls, doc, dataset=None):
        """Creates an instance of the :class:`Frame` class backed by the given
        document.

        Args:
            doc: a :class:`fiftyone.core.odm.SampleDocument`
            dataset (None): the :class:`fiftyone.core.dataset.Dataset` that the
                frame belongs to

        Returns:
            a :class:`Frame`
        """
        if isinstance(doc, NoDatasetFrameSampleDocument):
            sample = cls.__new__(cls)
            sample._dataset = None
            sample._doc = doc
            return sample

        try:
            # Get instance if exists
            sample = cls._instances[doc.collection_name][str(doc._sample_id)][
                doc.frame_number
            ]
        except KeyError:
            sample = cls.__new__(cls)
            sample._doc = None  # set to prevent RecursionError
            if dataset is None:
                raise ValueError(
                    "`dataset` arg must be provided if frame is in a dataset"
                )
            sample._set_backing_doc(doc, dataset=dataset)

        return sample

    def _set_backing_doc(self, doc, dataset=None):
        """Sets the backing doc for the sample.

        Args:
            doc: a :class:`fiftyone.core.odm.SampleDocument`
            dataset (None): the :class:`fiftyone.core.dataset.Dataset` to which
                the sample belongs, if any
        """
        self._doc = doc

        # Save weak reference
        dataset_instances = self._instances[doc.collection_name]
        _sample_id = str(self._sample_id)
        if self.frame_number not in dataset_instances[_sample_id]:
            dataset_instances[_sample_id][self.frame_number] = self

        self._dataset = dataset
