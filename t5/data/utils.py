# Copyright 2020 The T5 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""Utilities for data loading and processing."""

import contextlib
import functools
import inspect
import os

from absl import logging
import gin
import numpy as np
from t5.data import sentencepiece_vocabulary
import tensorflow.compat.v2 as tf
import tensorflow_datasets as tfds

_INFO_FILENAME = "info.{split}.json"
_STATS_FILENAME = "stats.{split}.json"
_TFRECORD_PREFIX = "{split}.tfrecord"

_TFDS_DATA_DIR_OVERRIDE = None
_GLOBAL_CACHE_DIRECTORIES = []

DEFAULT_SPM_PATH = "gs://t5-data/vocabs/cc_all.32000/sentencepiece.model"  # GCS
DEFAULT_EXTRA_IDS = 100


def get_default_vocabulary():
  return sentencepiece_vocabulary.SentencePieceVocabulary(
      DEFAULT_SPM_PATH, DEFAULT_EXTRA_IDS)




def set_tfds_data_dir_override(tfds_data_dir):
  global _TFDS_DATA_DIR_OVERRIDE
  _TFDS_DATA_DIR_OVERRIDE = tfds_data_dir


def get_global_cache_dirs():
  return _GLOBAL_CACHE_DIRECTORIES


def set_global_cache_dirs(global_cache_dirs):
  global _GLOBAL_CACHE_DIRECTORIES
  _GLOBAL_CACHE_DIRECTORIES = global_cache_dirs


def add_global_cache_dirs(global_cache_dirs):
  global _GLOBAL_CACHE_DIRECTORIES
  _GLOBAL_CACHE_DIRECTORIES += global_cache_dirs


class LazyTfdsLoader(object):
  """Wrapper for TFDS datasets with memoization and additional functionality.

  Lazily loads info from TFDS and provides memoization to avoid expensive hidden
  file operations. Also provides additional utility methods.
  """

  _MEMOIZED_BUILDERS = {}

  def __init__(self, name, data_dir=None, split_map=None):
    """LazyTfdsLoader constructor.

    Args:
      name: str, the name of the TFDS dataset.
      data_dir: str (optional), directory to read/write TFDS data.
      split_map: dict (optional), mapping from canonical splits
        (e.g., 'validation') to TFDS splits or slices
        (e.g., 'train[':1%']).
    """
    self._name = name
    self._data_dir = data_dir
    self._split_map = split_map

  @property
  def name(self):
    return self._name

  @property
  def data_dir(self):
    if _TFDS_DATA_DIR_OVERRIDE:
      if self._data_dir:
        logging.warning(
            "Overriding TFDS data directory '%s' with '%s' for dataset '%s'.",
            self._data_dir, _TFDS_DATA_DIR_OVERRIDE, self.name)
      return _TFDS_DATA_DIR_OVERRIDE
    return self._data_dir

  @property
  def builder(self):
    builder_key = (self.name, self.data_dir)
    if builder_key not in LazyTfdsLoader._MEMOIZED_BUILDERS:
      LazyTfdsLoader._MEMOIZED_BUILDERS[builder_key] = tfds.builder(
          self.name, data_dir=self.data_dir)
    return LazyTfdsLoader._MEMOIZED_BUILDERS[builder_key]

  @property
  def info(self):
    return self.builder.info

  def _map_split(self, split):
    return self._split_map[split] if self._split_map else split

  def files(self, split):
    """Returns set of instructions for reading TFDS files for the dataset."""
    split = self._map_split(split)

    if "/" not in self.name and self.builder.BUILDER_CONFIGS:
      # If builder has multiple configs, and no particular config was
      # requested, raise an error.
      raise ValueError("Dataset '%s' has multiple configs." % self.name)

    split_info = self.builder.info.splits[split]
    files = split_info.file_instructions

    if not files:
      logging.fatal("No TFRecord files found for dataset: %s", self.name)
    return files

  def load(self, split, shuffle_files, seed=None):
    """Returns a tf.data.Dataset for the given split."""
    split = self._map_split(split)
    return tfds.load(
        self._name,
        split=split,
        data_dir=self.data_dir,
        shuffle_files=shuffle_files,
        download=True,
        try_gcs=True,
        read_config=tfds.ReadConfig(
            shuffle_seed=seed,
            skip_prefetch=True
        )
    )

  def load_shard(self, file_instruction, shuffle_files=False, seed=None):
    """Returns a dataset for a single shard of the TFDS TFRecord files."""
    ds = self.builder._tfrecords_reader.read_files(  # pylint:disable=protected-access
        [file_instruction],
        read_config=tfds.ReadConfig(shuffle_seed=seed),
        shuffle_files=shuffle_files)
    return ds

  def size(self, split):
    """Returns the number of examples in the split."""
    split = self._map_split(split)
    ds_splits = self.info.splits
    dataset_size = ds_splits[split].num_examples
    # Very large datasets have num_examples = 0; default instead to np.inf
    dataset_size = dataset_size if dataset_size > 0 else np.inf
    return dataset_size


def dict_to_tfexample(ex):
  """Convert example dictionary to tf.train.Example proto."""
  feature_dict = {}
  for k, v in ex.items():
    t = tf.constant(v)
    if len(t.shape) == 0:  # pylint:disable=g-explicit-length-test
      v = [v]
    elif len(t.shape) == 1:
      v = list(v)
    else:
      raise ValueError(
          "Unsupported shape (%s) for '%s' value: %s" %
          (t.shape, k, v))

    if t.dtype == tf.string and len(t.shape) <= 1:
      feature_dict[k] = tf.train.Feature(
          bytes_list=tf.train.BytesList(
              value=[tf.compat.as_bytes(t) for t in v]))
    elif t.dtype in (tf.int32, tf.int64) and len(t.shape) <= 1:
      feature_dict[k] = tf.train.Feature(
          int64_list=tf.train.Int64List(value=v))
    elif t.dtype in (tf.float32, tf.float64) and len(t.shape) <= 1:
      feature_dict[k] = tf.train.Feature(
          float_list=tf.train.FloatList(value=v))
    else:
      raise ValueError(
          "Unsupported type (%s) and shape (%s) for '%s' value: %s" %
          (t.dtype, t.shape, k, v))

  return tf.train.Example(features=tf.train.Features(feature=feature_dict))


# ================================ Tasks =======================================
def get_info_path(data_dir, split):
  return os.path.join(data_dir, _INFO_FILENAME.format(split=split))


def get_tfrecord_prefix(data_dir, split):
  return os.path.join(data_dir, _TFRECORD_PREFIX.format(split=split))


def get_stats_path(data_dir, split):
  return os.path.join(data_dir, _STATS_FILENAME.format(split=split))


def print_dataset(dataset):
  """tf.Print dataset fields for debugging purposes."""
  def my_fn(x):
    return {k: tf.Print(v, [v], k + ": ") for k, v in x.items()}
  return dataset.map(my_fn)


# ========================= Mixing Rate Functions ==============================


@gin.configurable
def rate_num_examples(
    task, maximum=None, temperature=1.0, scale=1.0,
    fallback_to_num_input_examples=True):
  """Mixing rate equal to the number of examples for the task."""

  if task.cache_dir or not fallback_to_num_input_examples:
    ret = task.get_cached_stats("train")["examples"]
  else:
    logging.warning(
        "Task '%s' not cached so using number of input examples instead of "
        "preprocessed examples to compute rate.",
        task.name)
    ret = task.num_input_examples("train")

  ret *= scale
  if maximum:
    ret = min(ret, maximum)
  if temperature != 1.0:
    ret = ret ** (1.0 / temperature)
  return ret


@gin.configurable
def rate_unsupervised(task, value=1e6):
  """Gin-configurable mixing rate for the unsupervised co-training task."""
  del task
  return value


def stateless_shuffle(value, seed):
  """Randomly shuffles a tensor, statelessly."""
  flat_value = tf.reshape(value, [-1])
  indices = tf.argsort(
      tf.random.stateless_uniform(tf.shape(flat_value), seed=seed)
  )
  flat_shuffle = tf.gather(flat_value, indices)
  return tf.reshape(flat_shuffle, tf.shape(value))


# ======================== Decorators =========================================


_NEXT_MAP_SEED = None


@contextlib.contextmanager
def map_seed_manager(initial_seed=None):
  """Contextmanager to control the initial seed used by `map_over_dataset`."""
  global _NEXT_MAP_SEED
  old_map_seed = _NEXT_MAP_SEED
  _NEXT_MAP_SEED = initial_seed
  yield
  _NEXT_MAP_SEED = old_map_seed


def map_over_dataset(fn=None, *, num_seeds=None):
  """Decorator to map decorated function over dataset.

  Many preprocessors map a function over a dataset. This decorator helps reduce
  boilerplate for this common pattern.

  If `num_seeds` is set to 1, a unique random seed (pair of int32) will be
  passed to the mapping function with keyword 'seed'.
  If `num_seeds` is greater than 1, unique random seeds (pairs of int32) will be
  passed to the mapping function with keyword 'seeds'.
  These seeds can be generated deterministically by using the `map_seed_manager`
  to set the seed for the process that generates the individual seeds for each
  mapping function. These seeds will be set sequentially from the initial seed
  for each call to `map_over_dataset` where `num_seeds > 0`.

  Args:
    fn: map function
    num_seeds: optional number of random seeds (pairs of int32) to pass to the
      mapping function.

  Returns:
    Function which takes dataset as first argument.
  """

  def map_without_seeds(fn):
    @functools.wraps(fn)
    def wrapped_fn(ds, *args, **kargs):
      return ds.map(
          lambda arg: fn(arg, *args, **kargs),
          num_parallel_calls=tf.data.experimental.AUTOTUNE)

    return wrapped_fn

  def map_with_seeds(fn):
    @functools.wraps(fn)
    def wrapped_fn(ds, *args, **kwargs):
      global _NEXT_MAP_SEED
      if _NEXT_MAP_SEED is None:
        random_ds_seeds = ((None, None),) * num_seeds
      else:
        random_ds_seeds = np.arange(
            _NEXT_MAP_SEED, _NEXT_MAP_SEED + 2 * num_seeds).reshape(-1, 2)
        random_ds_seeds = tuple(tuple(s) for s in random_ds_seeds)
        _NEXT_MAP_SEED += 2 * num_seeds
      seed_datasets = tf.nest.map_structure(
          tf.data.experimental.RandomDataset,
          random_ds_seeds)
      if num_seeds == 1:
        map_fn = lambda x, s: fn(x, seed=s[0], *args, **kwargs)
      else:
        map_fn = lambda x, s: fn(x, seeds=s, *args, **kwargs)
      return tf.data.Dataset.zip((ds, seed_datasets)).map(
          map_fn, num_parallel_calls=tf.data.experimental.AUTOTUNE)

    # Remove seeds from function signature.
    sig = inspect.signature(wrapped_fn)
    wrapped_fn.__signature__ = sig.replace(
        parameters=tuple(
            p for p in sig.parameters.values() if p.name not in("seed", "seeds")
        )
    )
    return wrapped_fn

  if fn is None:
    return map_with_seeds
  else:
    return map_without_seeds(fn)
